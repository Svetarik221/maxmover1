import aiohttp
import structlog

from bot.config import settings
from core.link_replacer import replace_links
from core.media_handler import upload_media_to_max

log = structlog.get_logger()


class MaxPublisher:
    """Клиент для публикации постов в MAX через platform-api.max.ru."""

    def __init__(self) -> None:
        self.base_url = settings.max_api_base_url
        self.headers = {"Authorization": settings.max_bot_token}

    async def get_channels(self) -> list[dict]:
        """Возвращает список каналов, в которых бот является админом."""
        if settings.max_dev_mode:
            log.info("[DEV] Получение каналов MAX пропущено")
            return [{"chat_id": "dev_123", "title": "DEV-канал"}]
        url = f"{self.base_url}/chats"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        chats = data.get("chats", [])
                        # Только каналы
                        return [c for c in chats if c.get("type") in ("channel", "chat")]
                    log.warning("Ошибка получения чатов MAX", status=resp.status)
                    return []
        except Exception:
            log.exception("Ошибка получения каналов MAX")
            return []

    async def check_channel_access(self, channel_id: str) -> bool:
        """Проверяет, есть ли у бота доступ к каналу MAX."""
        if settings.max_dev_mode:
            log.info("[DEV] Проверка MAX-канала пропущена", channel_id=channel_id)
            return True
        url = f"{self.base_url}/chats/{channel_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status == 200:
                        return True
                    log.warning(
                        "Нет доступа к MAX-каналу",
                        channel_id=channel_id,
                        status=resp.status,
                    )
                    return False
        except Exception:
            log.exception("Ошибка проверки доступа к MAX", channel_id=channel_id)
            return False

    async def send_message(
        self,
        chat_id: str,
        text: str | None = None,
        attachments: list[dict] | None = None,
        disable_link_preview: bool = True,
        link_mapping: dict[str, str] | None = None,
        text_format: str | None = None,
    ) -> dict | None:
        """Отправляет сообщение в MAX-канал."""
        url = f"{self.base_url}/messages"
        params = {"chat_id": int(chat_id)}
        body: dict = {}
        if text:
            body["text"] = replace_links(text, link_mapping)
            if text_format:
                body["format"] = text_format
        if attachments:
            body["attachments"] = attachments
        if disable_link_preview:
            body["disable_link_preview"] = True

        import asyncio as _aio
        for _retry in range(4):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, headers=self.headers, params=params, json=body
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        body_text = await resp.text()
                        if "not.ready" in body_text or "not.processed" in body_text:
                            log.warning("Вложение не готово, retry", attempt=_retry+1)
                            await _aio.sleep(15 * (_retry + 1))  # 15/30/45/60с для видео
                            continue
                        log.error("Ошибка отправки в MAX", status=resp.status, body=body_text)
                        return None
            except Exception:
                log.exception("Ошибка отправки сообщения в MAX")
                return None
        log.warning("Все попытки исчерпаны, публикуем без медиа")
        # Fallback: текст без медиа
        if body.get("attachments"):
            body.pop("attachments")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=self.headers, params=params, json=body) as resp:
                        if resp.status == 200:
                            return await resp.json()
            except Exception:
                pass
            return None

    async def publish_post(
        self,
        chat_id: str,
        text: str | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
        link_mapping: dict[str, str] | None = None,
        text_format: str | None = None,
        add_comments: bool = False,
    ) -> dict | None:
        """Публикует пост (текст + медиа) в MAX-канал."""
        if settings.max_dev_mode:
            log.info(
                "[DEV] Публикация в MAX пропущена",
                chat_id=chat_id,
                media_type=media_type,
                has_text=bool(text),
                has_media=bool(media_path),
            )
            return {"message": {"id": f"dev_{chat_id}"}}

        attachments = []
        if media_path and media_type:
            upload_result = await upload_media_to_max(media_path, media_type)
            if upload_result:
                attachments.append(upload_result)

        # Отправляем пост
        # Добавляем кнопку комментов сразу в attachments
        if add_comments:
            _safe_chat = str(chat_id).replace("-", "n")
            attachments.append({
                "type": "inline_keyboard",
                "payload": {"buttons": [[{
                    "type": "open_app",
                    "text": "💬 Комментарии",
                    "web_app": settings.max_bot_username,
                    "payload": f"pending__{_safe_chat}",
                }]]}
            })

        result = await self.send_message(
            chat_id=chat_id,
            text=text,
            attachments=attachments if attachments else None,
            link_mapping=link_mapping,
            text_format=text_format,
        )

        # Обновляем payload кнопки с реальным mid
        if result and add_comments:
            mid = result.get("message", {}).get("body", {}).get("mid", "")
            if mid:
                _safe_mid = mid.replace(".", "-")
                _safe_chat = str(chat_id).replace("-", "n")
                try:
                    import aiohttp as _ah
                    async with _ah.ClientSession() as _s:
                        # GET текущие atts (media + keyboard с pending payload)
                        cur_atts = []
                        async with _s.get(
                            f"{self.base_url}/messages?message_ids={mid}",
                            headers=self.headers
                        ) as _r:
                            if _r.status == 200:
                                _d = await _r.json()
                                if _d.get("messages"):
                                    cur_atts = _d["messages"][0].get("body", {}).get("attachments", [])
                        # Заменяем pending payload на реальный
                        for att in cur_atts:
                            if att.get("type") == "inline_keyboard":
                                for row in att.get("payload", {}).get("buttons", []):
                                    for btn in row:
                                        if btn.get("payload", "").startswith("pending__"):
                                            btn["payload"] = f"{_safe_mid}__{_safe_chat}"
                        await _s.put(
                            f"{self.base_url}/messages?message_id={mid}",
                            headers=self.headers,
                            json={"attachments": cur_atts}
                        )
                except Exception:
                    pass

        # Добавляем кнопку комментариев после отправки — только если у канала включены комменты
        if result and settings.webhook_host:
            mid = result.get("message", {}).get("body", {}).get("mid", "")
            if mid:
                from db.database import async_session as _async_session
                from db.models import Channel as _Ch
                from sqlalchemy import select as _sel
                async with _async_session() as _s:
                    _r = await _s.execute(_sel(_Ch).where(_Ch.max_channel_id == str(chat_id)).limit(1))
                    _channel = _r.scalar_one_or_none()
                if _channel and _channel.comments_enabled:
                    await self._add_comment_button(chat_id, mid, text, attachments, link_mapping)

        return result

    async def _add_comment_button(
        self,
        chat_id: str,
        mid: str,
        text: str | None,
        attachments: list[dict] | None,
        link_mapping: dict[str, str] | None = None,
    ) -> None:
        """Редактирует пост — добавляет кнопку комментариев."""
        # payload: только A-Z, a-z, 0-9, _, -
        # mid формат: mid.xxxxxx → заменяем . на -
        # разделитель полей: __
        safe_mid = mid.replace(".", "-")
        safe_chat = str(chat_id).replace("-", "n")  # минус → n (negative)
        payload = f"{safe_mid}__{safe_chat}"
        comment_btn = {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{
                    "type": "open_app",
                    "text": "💬 Комментарии",
                    "web_app": settings.max_bot_username,
                    "payload": payload,
                }]]
            },
        }
        edit_body: dict = {}
        if text:
            edit_body["text"] = replace_links(text, link_mapping)
        edit_attachments = list(attachments) if attachments else []
        edit_attachments.append(comment_btn)
        edit_body["attachments"] = edit_attachments

        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"{self.base_url}/messages?message_id={mid}",
                    headers=self.headers,
                    json=edit_body,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("Не удалось добавить кнопку комментариев", status=resp.status, body=body)
        except Exception:
            log.exception("Ошибка добавления кнопки комментариев")

    async def send_reply(
        self,
        chat_id: str,
        mid: str,
        text: str,
    ) -> dict | None:
        """Отправляет комментарий (reply) к сообщению в MAX-канале."""
        url = f"{self.base_url}/messages"
        params = {"chat_id": int(chat_id)}
        body: dict = {
            "text": text,
            "link": {"type": "reply", "mid": mid},
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self.headers, params=params, json=body
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    body_text = await resp.text()
                    log.error(
                        "Ошибка отправки комментария в MAX",
                        status=resp.status,
                        body=body_text,
                    )
                    return None
        except Exception:
            log.exception("Ошибка отправки комментария в MAX")
            return None

    async def answer_callback(self, callback_id: str, notification: str | None = None) -> bool:
        """Отвечает на callback от inline-кнопки."""
        url = f"{self.base_url}/answers"
        body: dict = {"callback_id": callback_id}
        if notification:
            body["notification"] = notification
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=body) as resp:
                    return resp.status == 200
        except Exception:
            log.exception("Ошибка ответа на callback")
            return False

    async def send_dm(self, user_id: int, text: str) -> dict | None:
        """Отправляет личное сообщение пользователю в MAX."""
        url = f"{self.base_url}/messages"
        params = {"user_id": user_id}
        body = {"text": text, "format": "html"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self.headers, params=params, json=body
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    body_text = await resp.text()
                    log.error("Ошибка отправки ЛС в MAX", status=resp.status, body=body_text)
                    return None
        except Exception:
            log.exception("Ошибка отправки ЛС в MAX")
            return None
