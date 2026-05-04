"""MAX long polling — автоматическое добавление кнопки комментариев к новым постам."""
import asyncio

import aiohttp
import structlog

from bot.config import settings

log = structlog.get_logger()

# Множество mid, которые мы уже обработали (чтобы не добавлять кнопку повторно)
_processed_mids: set[str] = set()


async def run_max_polling() -> None:
    """Запускает long polling для MAX бота."""
    base_url = settings.max_api_base_url
    headers = {"Authorization": settings.max_bot_token}
    marker: int | None = None

    log.info("MAX polling запущен")

    while True:
        try:
            params: dict = {
                "timeout": 30,
                "limit": 50,
                "update_types": "message_created",
            }
            if marker:
                params["marker"] = marker

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base_url}/updates", headers=headers, params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        log.warning("Ошибка получения обновлений MAX", status=resp.status)
                        await asyncio.sleep(5)
                        continue

                    data = await resp.json()
                    updates = data.get("updates", [])
                    new_marker = data.get("marker")
                    if new_marker:
                        marker = new_marker

            for update in updates:
                if update.get("update_type") == "message_created":
                    await _handle_new_message(update)

        except asyncio.CancelledError:
            log.info("MAX polling остановлен")
            break
        except Exception:
            log.exception("Ошибка в MAX polling")
            await asyncio.sleep(5)


async def _handle_new_message(update: dict) -> None:
    """Если новый пост в канале — добавляем кнопку комментариев."""
    message = update.get("message", {})
    recipient = message.get("recipient", {})
    chat_type = recipient.get("chat_type", "")
    chat_id = recipient.get("chat_id")
    sender = message.get("sender", {})
    mid = message.get("body", {}).get("mid", "")

    # Только посты в каналах
    if chat_type != "channel" or not mid or not chat_id:
        return

    # Не обрабатываем свои же сообщения (от бота)
    bot_id = int(__import__("os").environ.get("MAX_BOT_USER_ID", 0))  # см. README
    if sender.get("user_id") == bot_id:
        return

    # Не обрабатываем повторно
    if mid in _processed_mids:
        return
    _processed_mids.add(mid)
    # Чистим старые (держим не больше 10000)
    if len(_processed_mids) > 10000:
        to_remove = list(_processed_mids)[:5000]
        for m in to_remove:
            _processed_mids.discard(m)

    # Проверяем что этот канал зарегистрирован у нас
    from db.database import async_session
    from sqlalchemy import select
    from db.models import Channel

    async with async_session() as session:
        result = await session.execute(
            select(Channel).where(Channel.max_channel_id == str(chat_id))
        )
        channel = result.scalar_one_or_none()

    if not channel:
        return

    log.info("Новый пост в канале, добавляем кнопку комментариев", mid=mid, chat_id=chat_id)

    # Добавляем кнопку комментариев
    safe_mid = mid.replace(".", "-")
    safe_chat = str(chat_id).replace("-", "n")
    payload = f"{safe_mid}__{safe_chat}"

    base_url = settings.max_api_base_url
    headers = {"Authorization": settings.max_bot_token}

    try:
        async with aiohttp.ClientSession() as session:
            # GET текущие attachments чтобы не затереть media
            cur_atts = []
            async with session.get(f"{base_url}/messages?message_ids={mid}", headers=headers) as gr:
                if gr.status == 200:
                    gd = await gr.json()
                    if gd.get("messages"):
                        cur_atts = gd["messages"][0].get("body", {}).get("attachments", [])
            new_atts = [a for a in cur_atts if a.get("type") != "inline_keyboard"]
            new_atts.append({
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [[{
                        "type": "open_app",
                        "text": "💬 Комментарии",
                        "web_app": settings.max_bot_username,
                        "payload": payload,
                    }]]
                }
            })
            edit_body = {"attachments": new_atts}

            async with session.put(
                f"{base_url}/messages?message_id={mid}",
                headers=headers,
                json=edit_body,
            ) as resp:
                if resp.status == 200:
                    log.info("Кнопка комментариев добавлена", mid=mid)
                else:
                    body = await resp.text()
                    log.warning("Не удалось добавить кнопку", status=resp.status, body=body)
    except Exception:
        log.exception("Ошибка добавления кнопки комментариев", mid=mid)
