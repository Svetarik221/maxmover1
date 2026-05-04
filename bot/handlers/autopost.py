"""Автопостинг TG→MAX через aiogram channel_post handler."""
import asyncio
import os
from collections import defaultdict

import structlog
from aiogram import F, Router
from aiogram.types import Message

from core.link_replacer import replace_links
from core.max_publisher import MaxPublisher
from core.media_handler import upload_media_to_max
from db.database import async_session
from db.models import Channel, PostMapping
from sqlalchemy import select
from core.tariff import get_channel_tariff, can_use

log = structlog.get_logger()
router = Router()

# Буфер для сбора альбомов (media_group)
_album_buffer: dict[str, list[Message]] = defaultdict(list)
_album_tasks: dict[str, asyncio.Task] = {}
ALBUM_WAIT = 1.5  # секунд ждём остальные фото альбома



async def _add_comment_button(bot_token: str, api_url: str, mid: str, chat_id: str, web_app: str):
    """Добавляет кнопку комментариев. GET текущие atts чтобы не затереть media."""
    import aiohttp, asyncio as _asl
    await _asl.sleep(2)  # ждём обработку поста MAX
    safe_mid = mid.replace(".", "-")
    safe_chat = str(chat_id).replace("-", "n")
    headers = {"Authorization": bot_token}
    try:
        async with aiohttp.ClientSession() as s:
            cur = []
            async with s.get(f"{api_url}/messages?message_ids={mid}", headers=headers) as r:
                if r.status == 200:
                    d = await r.json()
                    ms = d.get("messages", [])
                    if ms:
                        cur = ms[0].get("body", {}).get("attachments", [])
            atts = [a for a in cur if a.get("type") != "inline_keyboard"]
            atts.append({"type": "inline_keyboard", "payload": {"buttons": [[{
                "type": "open_app", "text": "💬 Комментарии",
                "web_app": web_app, "payload": f"{safe_mid}__{safe_chat}",
            }]]}})
            await s.put(f"{api_url}/messages?message_id={mid}",
                       headers=headers, json={"attachments": atts})
    except Exception:
        pass



async def _send_to_de_for_large_media(channel, tg_username: str, message_ids, text: str, add_comments: bool):
    """Отправляет задачу DE-воркеру. message_ids: int (одиночный) или list[int] (альбом)."""
    import aiohttp, json
    from bot.config import settings
    if isinstance(message_ids, int):
        message_ids = [message_ids]
    payload = {
        "type": "single_post",
        "tg_username": tg_username,
        "message_ids": message_ids,
        "message_id": message_ids[0],  # legacy-совместимость
        "max_channel_id": channel.max_channel_id,
        "channel_id": channel.id,
        "text": text,
        "add_comments": add_comments,
    }
    ctrl_msg = json.dumps(payload)
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"{settings.max_api_base_url}/messages",
                headers={"Authorization": settings.max_bot_token},
                params={"chat_id": settings.relay_chat_id},
                json={"text": f"_POST_{ctrl_msg}"},
            )
        log.info("Большое медиа отправлено DE-воркеру", msg_ids=message_ids)
    except Exception:
        log.exception("Ошибка отправки задачи DE")

async def _publish_album(messages: list[Message], channel: "Channel") -> None:
    """Публикует альбом (несколько медиа) как один пост в MAX."""
    # Текст берём из первого сообщения (caption)
    text = ""
    for m in messages:
        t = m.html_text or m.caption or ""
        if t:
            text = t
            break

    # Скачиваем все медиа
    os.makedirs("media_cache", exist_ok=True)
    media_paths = []
    for m in messages:
        local_path = None
        media_type = None
        if m.photo:
            media_type = "photo"
            local_path = f"media_cache/auto_{m.message_id}.jpg"
            await m.bot.download(m.photo[-1].file_id, destination=local_path)
        elif m.video:
            media_type = "video"
            local_path = f"media_cache/auto_{m.message_id}.mp4"
            try:
                await m.bot.download(m.video.file_id, destination=local_path)
            except Exception:
                log.warning("Video too big, sending album to DE",
                            msg_id=m.message_id, album_size=len(messages))
                # Удаляем уже скачанные мелкие медиа альбома — DE перекачает всё сам
                for p, _mt in media_paths:
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                _add_cmts = False
                if channel.comments_enabled:
                    from core.tariff import get_channel_tariff, can_use
                    _t = await get_channel_tariff(channel.id)
                    _add_cmts = can_use(_t, "comments")
                await _send_to_de_for_large_media(
                    channel, channel.tg_channel_username,
                    [mm.message_id for mm in messages], text,
                    _add_cmts,
                )
                return  # DE обработает весь альбом целиком
        elif m.document:
            media_type = "document"
            local_path = f"media_cache/auto_{m.message_id}_{m.document.file_name or 'file'}"
            try:
                await m.bot.download(m.document.file_id, destination=local_path)
            except Exception:
                log.warning("Doc too big, skip", msg_id=m.message_id)
                continue
        if local_path and media_type:
            media_paths.append((local_path, media_type))

    # Загружаем все медиа в MAX
    attachments = []
    for path, mtype in media_paths:
        upload_result = await upload_media_to_max(path, mtype)
        if upload_result:
            attachments.append(upload_result)

    # Автозамена ссылок
    from services.link_mapping import build_link_mapping
    link_mapping = await build_link_mapping(channel.user_id)
    if link_mapping and text:
        text = replace_links(text, link_mapping)

    # Публикуем один пост с несколькими аттачментами
    publisher = MaxPublisher()
    body = {}
    if text:
        body["text"] = text
        body["format"] = "html"
    if attachments:
        body["attachments"] = attachments
    if not body:
        return

    import aiohttp
    from bot.config import settings
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{settings.max_api_base_url}/messages",
                headers={"Authorization": settings.max_bot_token},
                params={"chat_id": int(channel.max_channel_id)},
                json=body,
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    max_msg_id = result.get("message", {}).get("body", {}).get("mid", "")
                    # Сохраняем маппинг для каждого сообщения альбома
                    async with async_session() as db:
                        for m in messages:
                            db.add(PostMapping(
                                channel_id=channel.id,
                                tg_message_id=m.message_id,
                                max_message_id=max_msg_id,
                            ))
                        await db.commit()
                    # Кнопка комментариев
                    print(f"[AUTOPOST COMMENTS] checking channel={channel.id} comments_enabled={channel.comments_enabled}", flush=True)
                    if channel.comments_enabled:
                        from core.tariff import get_channel_tariff, can_use
                        from bot.config import settings
                        _tar = await get_channel_tariff(channel.id)
                        if can_use(_tar, "comments"):
                            await _add_comment_button(settings.max_bot_token, settings.max_api_base_url,
                                                     max_msg_id, channel.max_channel_id, settings.max_bot_username)
                    log.info("Автопостинг: альбом опубликован", count=len(messages), max_mid=max_msg_id)
    except Exception:
        log.exception("Ошибка автопостинга альбома")

    # Чистим медиа
    for path, _ in media_paths:
        try:
            os.remove(path)
        except Exception:
            pass


async def _process_single(message: Message, channel: "Channel") -> None:
    """Публикует одиночный пост в MAX."""
    media_path = None
    media_type = None
    os.makedirs("media_cache", exist_ok=True)

    if message.photo:
        media_type = "photo"
        local_path = f"media_cache/auto_{message.message_id}.jpg"
        await message.bot.download(message.photo[-1].file_id, destination=local_path)
        media_path = local_path
    elif message.video:
        media_type = "video"
        local_path = f"media_cache/auto_{message.message_id}.mp4"
        try:
            await message.bot.download(message.video.file_id, destination=local_path)
            media_path = local_path
        except Exception:
            log.warning("Video too big, sending to DE", msg_id=message.message_id)
            _add_cmts = False
            if channel.comments_enabled:
                from core.tariff import get_channel_tariff, can_use
                _t = await get_channel_tariff(channel.id)
                _add_cmts = can_use(_t, "comments")
            await _send_to_de_for_large_media(
                channel, channel.tg_channel_username,
                message.message_id, text, _add_cmts,
            )
            if media_path and os.path.exists(media_path):
                os.remove(media_path)
            return
    elif message.document:
        media_type = "document"
        ext = message.document.file_name or "file"
        local_path = f"media_cache/auto_{message.message_id}_{ext}"
        await message.bot.download(message.document.file_id, destination=local_path)
        media_path = local_path

    text = message.html_text or message.caption or ""

    from services.link_mapping import build_link_mapping
    link_mapping = await build_link_mapping(channel.user_id)
    publisher = MaxPublisher()
    _add_cmts = False
    if channel.comments_enabled:
        from core.tariff import get_channel_tariff, can_use
        _t = await get_channel_tariff(channel.id)
        _add_cmts = can_use(_t, "comments")
    result = await publisher.publish_post(
        chat_id=channel.max_channel_id,
        text=text or None,
        media_path=media_path,
        media_type=media_type,
        link_mapping=link_mapping,
        text_format="html",
        add_comments=_add_cmts,
    )

    if result:
        max_msg_id = result.get("message", {}).get("body", {}).get("mid", "")
        async with async_session() as session:
            session.add(PostMapping(
                channel_id=channel.id,
                tg_message_id=message.message_id,
                max_message_id=max_msg_id,
            ))
            await session.commit()
        # Кнопка комментариев
        print(f"[AUTOPOST COMMENTS SINGLE] channel={channel.id} comments_enabled={channel.comments_enabled} mid={max_msg_id}", flush=True)
        if channel.comments_enabled:
            from core.tariff import get_channel_tariff, can_use
            from bot.config import settings
            _tar = await get_channel_tariff(channel.id)
            if can_use(_tar, "comments"):
                await _add_comment_button(settings.max_bot_token, settings.max_api_base_url,
                                         max_msg_id, channel.max_channel_id, settings.max_bot_username)
        log.info("Автопостинг: опубликовано", tg_msg=message.message_id, max_mid=max_msg_id)

    if media_path and os.path.exists(media_path):
        os.remove(media_path)


@router.channel_post()
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_channel_post(message: Message) -> None:
    """Ловит новые посты в TG-каналах и публикует в MAX."""
    chat_id = message.chat.id

    async with async_session() as session:
        result = await session.execute(
            select(Channel).where(
                Channel.tg_channel_id == chat_id,
                Channel.autopost_enabled == True,
            )
        )
        channel = result.scalar_one_or_none()

    if not channel or not channel.max_channel_id:
        return

    tariff = await get_channel_tariff(channel.id)
    if not can_use(tariff, "autopost"):
        return

    # Дедуп
    async with async_session() as session:
        result = await session.execute(
            select(PostMapping).where(
                PostMapping.channel_id == channel.id,
                PostMapping.tg_message_id == message.message_id,
            )
        )
        if result.scalar_one_or_none():
            return

    # Альбом: буферизуем и ждём остальные фото
    if message.media_group_id:
        group_id = message.media_group_id
        _album_buffer[group_id].append(message)

        # Отменяем старый таймер и создаём новый
        if group_id in _album_tasks:
            _album_tasks[group_id].cancel()

        async def _delayed_publish(gid: str, ch: "Channel"):
            await asyncio.sleep(ALBUM_WAIT)
            msgs = _album_buffer.pop(gid, [])
            _album_tasks.pop(gid, None)
            if msgs:
                await _publish_album(msgs, ch)

        _album_tasks[group_id] = asyncio.create_task(_delayed_publish(group_id, channel))
        return

    # Одиночный пост
    await _process_single(message, channel)
