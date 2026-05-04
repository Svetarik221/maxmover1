"""Celery-задачи для отложенных постов и автопостинга."""
import asyncio

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from tasks.celery_app import celery_app

log = structlog.get_logger()

async def _add_comments_to_max_post(mid, channel):
    import asyncio as _acsleep
    await _acsleep.sleep(2)  # ждём обработку поста MAX
    """Добавляет комменты к посту в MAX через GET+PUT."""
    if not mid or not channel.comments_enabled:
        return
    from core.tariff import get_channel_tariff, can_use
    tariff = await get_channel_tariff(channel.id)
    if not can_use(tariff, "comments"):
        return
    import aiohttp
    from bot.config import settings
    safe_mid = mid.replace(".", "-")
    safe_chat = str(channel.max_channel_id).replace("-", "n")
    headers = {"Authorization": settings.max_bot_token}
    try:
        async with aiohttp.ClientSession() as s:
            cur = []
            async with s.get(f"{settings.max_api_base_url}/messages?message_ids={mid}", headers=headers) as r:
                if r.status == 200:
                    d = await r.json()
                    if d.get("messages"):
                        cur = d["messages"][0].get("body", {}).get("attachments", [])
            atts = [a for a in cur if a.get("type") != "inline_keyboard"]
            atts.append({"type": "inline_keyboard", "payload": {"buttons": [[{
                "type": "open_app", "text": "💬 Комментарии",
                "web_app": settings.max_bot_username,
                "payload": f"{safe_mid}__{safe_chat}",
            }]]}})
            await s.put(f"{settings.max_api_base_url}/messages?message_id={mid}",
                       headers=headers, json={"attachments": atts})
    except Exception:
        pass




@celery_app.task(bind=True, max_retries=3)
def publish_scheduled_post(self, scheduled_post_id: int) -> None:
    """Публикует отложенный пост в нужные каналы."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_publish_scheduled(scheduled_post_id))
    finally:
        loop.close()


async def _do_publish_scheduled(scheduled_post_id: int) -> None:
    from db.database import async_session
    from db.models import ScheduledPost, Channel
    from core.max_publisher import MaxPublisher
    from core.link_replacer import replace_links

    from aiogram.client.session.aiohttp import AiohttpSession
    session = AiohttpSession(proxy=settings.tg_api_proxy) if settings.tg_api_proxy else None
    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )

    try:
        async with async_session() as session:
            post = await session.get(ScheduledPost, scheduled_post_id)
            if not post or post.status != "pending":
                return

            channel = await session.get(Channel, post.channel_id)
            if not channel:
                return

            post.status = "publishing"
            await session.commit()

        text = post.content_text
        target = post.target  # "tg", "max", "both"
        media = post.media_file_ids  # {"type": "photo"/"video"/"document", "file_id": "..."}
        published_to = []

        # Публикуем в MAX
        if target in ("max", "both") and channel.max_channel_id:
            # Вариант 1: MAX-аттачменты (отложка создана из MAX-кабинета)
            if media and isinstance(media, dict) and media.get("max_attachments") and len(media["max_attachments"]) > 0:
                import aiohttp as _ah
                max_body = {}
                if text:
                    max_body["text"] = text
                    max_body["format"] = "html"
                max_body["attachments"] = list(media["max_attachments"])
                from bot.config import settings as _s
                try:
                    async with _ah.ClientSession() as _session:
                        async with _session.post(
                            f"{_s.max_api_base_url}/messages",
                            headers={"Authorization": _s.max_bot_token},
                            params={"chat_id": channel.max_channel_id},
                            json=max_body,
                        ) as _resp:
                            if _resp.status == 200:
                                published_to.append("MAX")
                                # Добавляем кнопку комментариев
                                _mid = (await _resp.json()).get("message", {}).get("body", {}).get("mid")
                                if _mid and channel.comments_enabled:
                                    from core.tariff import get_channel_tariff as _gct, can_use as _cu
                                    _t = await _gct(channel.id)
                                    if _cu(_t, "comments"):
                                        _safe_mid = _mid.replace(".", "-")
                                        _safe_chat = str(channel.max_channel_id).replace("-", "n")
                                        _cur1 = []
                                        async with _session.get(f"{_s.max_api_base_url}/messages?message_ids={_mid}", headers={"Authorization": _s.max_bot_token}) as _gr1:
                                            if _gr1.status == 200:
                                                _gd1 = await _gr1.json()
                                                if _gd1.get("messages"):
                                                    _cur1 = _gd1["messages"][0].get("body", {}).get("attachments", [])
                                        _pa1 = [a for a in _cur1 if a.get("type") != "inline_keyboard"]
                                        _pa1.append({"type": "inline_keyboard", "payload": {"buttons": [[{"type": "open_app", "text": "💬 Комментарии", "web_app": _s.max_bot_username, "payload": f"{_safe_mid}__{_safe_chat}"}]]}})
                                        await _session.put(
                                            f"{_s.max_api_base_url}/messages?message_id={_mid}",
                                            headers={"Authorization": _s.max_bot_token},
                                            json={"attachments": _pa1 + [{"type": "inline_keyboard", "payload": {"buttons": [[{"type": "open_app", "text": "\U0001f4ac Комментарии", "web_app": _s.max_bot_username, "payload": f"{_safe_mid}__{_safe_chat}"}]]}}]},
                                        )
                                # Комменты через GET+PUT (с правильным mid)
                                try:
                                    _resp_data = await _resp.json()
                                    _mid_ma = _resp_data.get("message", {}).get("body", {}).get("mid")
                                    if _mid_ma:
                                        await _add_comments_to_max_post(_mid_ma, channel)
                                except Exception:
                                    pass
                                log.info("Отложенный пост опубликован в MAX (с max_attachments)", post_id=scheduled_post_id)
                            else:
                                log.warning("Ошибка публикации отложки в MAX", status=_resp.status, body=(await _resp.text())[:200])
                except Exception:
                    log.exception("Ошибка публикации отложки в MAX")
            else:
                # Вариант 2: TG-отложка (media_file_ids имеет file_id)
                publisher = MaxPublisher()
                media_path = None
                media_type = None
                # Альбом: загружаем все файлы
                if media and media.get("album"):
                    import os
                    os.makedirs("media_cache", exist_ok=True)
                    attachments_list = []
                    for fi in media["album"]:
                        try:
                            try:
                                file = await bot.get_file(fi["file_id"])
                            except Exception as _efb:
                                log.warning("File too big, skip in album", error=str(_efb)[:50])
                                continue
                            ext = ".jpg" if fi["type"] == "photo" else ".mp4" if fi["type"] == "video" else ""
                            lp = f"media_cache/sched_album_{scheduled_post_id}_{fi['file_id'][:10]}{ext}"
                            await bot.download_file(file.file_path, lp)
                            from core.media_handler import upload_media_to_max
                            upl = await upload_media_to_max(lp, fi["type"])
                            if upl:
                                attachments_list.append(upl)
                            os.remove(lp)
                        except Exception:
                            log.exception("Ошибка загрузки медиа альбома")
                    if attachments_list:
                        from services.link_mapping import build_link_mapping
                        link_mapping = await build_link_mapping(channel.user_id)
                        # Retry при attachment.not.ready (видео обрабатывается)
                        for _attempt in range(4):
                            result = await publisher.send_message(
                                chat_id=channel.max_channel_id,
                                text=text,
                                attachments=attachments_list,
                                link_mapping=link_mapping,
                                text_format="html",
                            )
                            if result:
                                published_to.append("MAX")
                                _amid = result.get("message", {}).get("body", {}).get("mid")
                                if _amid:
                                    import asyncio as _ac1; await _ac1.sleep(2)
                                    await _add_comments_to_max_post(_amid, channel)
                                break
                            # Ждём обработки видео
                            import asyncio as _aio
                            await _aio.sleep(5 * (_attempt + 1))
                        else:
                            # Fallback: текст без медиа
                            result = await publisher.send_message(
                                chat_id=channel.max_channel_id,
                                text=text,
                                link_mapping=link_mapping,
                                text_format="html",
                            )
                            if result:
                                published_to.append("MAX")
                                _fmid = result.get("message", {}).get("body", {}).get("mid")

                elif media and media.get("file_id"):
                    try:
                        try:
                            file = await bot.get_file(media["file_id"])
                            import os
                            os.makedirs("media_cache", exist_ok=True)
                            local_path = f"media_cache/scheduled_{scheduled_post_id}.tmp"
                            await bot.download_file(file.file_path, local_path)
                        except Exception as _efb2:
                            log.warning("File too big for scheduled", error=str(_efb2)[:50])
                            file = None
                        media_path = local_path
                        media_type_map = {"photo": "photo", "video": "video", "document": "document"}
                        media_type = media_type_map.get(media.get("type"), "document")
                    except Exception:
                        log.exception("Ошибка загрузки медиа для отложенного поста", post_id=scheduled_post_id)

                # Публикуем одиночный пост (только если альбом не был опубликован выше)
                if "MAX" not in published_to:
                    from services.link_mapping import build_link_mapping
                    link_mapping = await build_link_mapping(channel.user_id)
                    _add_cmts = channel.comments_enabled
                    if _add_cmts:
                        from core.tariff import get_channel_tariff as _gctx, can_use as _cux
                        _add_cmts = _cux(await _gctx(channel.id), "comments")
                    result = await publisher.publish_post(
                        chat_id=channel.max_channel_id,
                        text=text,
                        media_path=media_path,
                        media_type=media_type,
                        link_mapping=link_mapping,
                        text_format="html",
                        add_comments=_add_cmts,
                    )
                    if result:
                        published_to.append("MAX")
                        # Кнопка комментариев
                        _mid2 = result.get("message", {}).get("body", {}).get("mid") if result else None

                        log.info("Отложенный пост опубликован в MAX", post_id=scheduled_post_id)

        # Публикуем в TG
        tg_chat = channel.tg_channel_id or f"@{channel.tg_channel_username}"
        if target in ("tg", "both") and tg_chat:
            # Обратная автозамена MAX→TG ссылок
            tg_text = text
            try:
                from services.link_mapping import build_reverse_link_mapping
                from core.link_replacer import replace_max_links
                rev_mapping = await build_reverse_link_mapping(channel.user_id)
                if rev_mapping and tg_text:
                    tg_text = replace_max_links(tg_text, rev_mapping)
            except Exception:
                pass
            try:
                # TG-отложка с альбомом: используем file_id напрямую (TG без лимита Bot API на file_id)
                # Важно: проверяем album ДО max_attachments — иначе TG-origin пост
                # с попутно загруженными в MAX медиа публиковался бы по MAX URL вместо file_id.
                if media and isinstance(media, dict) and media.get("album"):
                    from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
                    mg = []
                    for i, f in enumerate(media["album"]):
                        cap = tg_text if i == 0 and tg_text else None
                        if f["type"] == "photo":
                            mg.append(InputMediaPhoto(media=f["file_id"], caption=cap, parse_mode="HTML" if cap else None))
                        elif f["type"] == "video":
                            mg.append(InputMediaVideo(media=f["file_id"], caption=cap, parse_mode="HTML" if cap else None))
                        else:
                            mg.append(InputMediaDocument(media=f["file_id"], caption=cap, parse_mode="HTML" if cap else None))
                    if mg:
                        await bot.send_media_group(chat_id=tg_chat, media=mg)
                # MAX-отложка: качаем медиа по URL из max_attachments
                elif media and isinstance(media, dict) and media.get("max_attachments"):
                    import aiohttp as _ah
                    import os
                    from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument
                    os.makedirs("media_cache", exist_ok=True)
                    local_medias = []
                    async with _ah.ClientSession() as _sess:
                        for idx, att in enumerate(media["max_attachments"]):
                            att_type = att.get("type", "")
                            url = att.get("payload", {}).get("url") or att.get("url")
                            if not url or att_type == "inline_keyboard":
                                continue
                            if att_type in ("image", "photo"):
                                kind = "photo"; ext = ".jpg"
                            elif att_type == "video":
                                kind = "video"; ext = ".mp4"
                            elif att_type in ("file", "document"):
                                kind = "document"; ext = ""
                            else:
                                continue
                            path = f"media_cache/sched_{scheduled_post_id}_{idx}{ext}"
                            async with _sess.get(url) as _resp:
                                if _resp.status == 200:
                                    with open(path, "wb") as _f:
                                        _f.write(await _resp.read())
                                    local_medias.append((path, kind))

                    if len(local_medias) > 1:
                        mg = []
                        for i, (pth, k) in enumerate(local_medias):
                            cap = tg_text if i == 0 and tg_text else None
                            f = FSInputFile(pth)
                            if k == "photo": mg.append(InputMediaPhoto(media=f, caption=cap))
                            elif k == "video": mg.append(InputMediaVideo(media=f, caption=cap))
                            else: mg.append(InputMediaDocument(media=f, caption=cap))
                        await bot.send_media_group(chat_id=tg_chat, media=mg)
                    elif len(local_medias) == 1:
                        pth, k = local_medias[0]
                        f = FSInputFile(pth)
                        if k == "photo": await bot.send_photo(chat_id=tg_chat, photo=f, caption=tg_text or None)
                        elif k == "video": await bot.send_video(chat_id=tg_chat, video=f, caption=tg_text or None)
                        else: await bot.send_document(chat_id=tg_chat, document=f, caption=tg_text or None)
                    elif text:
                        await bot.send_message(chat_id=tg_chat, text=tg_text)
                    for pth, _ in local_medias:
                        try: os.remove(pth)
                        except Exception: pass
                # TG-отложка одиночная: публикуем через TG file_id
                elif media and media.get("type") == "photo":
                    await bot.send_photo(chat_id=tg_chat, photo=media["file_id"], caption=tg_text or None)
                elif media and media.get("type") == "video":
                    await bot.send_video(chat_id=tg_chat, video=media["file_id"], caption=tg_text or None)
                elif media and media.get("type") == "document":
                    await bot.send_document(chat_id=tg_chat, document=media["file_id"], caption=tg_text or None)
                else:
                    await bot.send_message(chat_id=tg_chat, text=tg_text)
                published_to.append("TG")
                log.info("Отложенный пост опубликован в TG", post_id=scheduled_post_id)
            except Exception:
                log.exception("Ошибка публикации в TG", post_id=scheduled_post_id)

        async with async_session() as session:
            post = await session.get(ScheduledPost, scheduled_post_id)
            post.status = "done" if published_to else "failed"
            await session.commit()

        # Уведомляем пользователя
        if published_to:
            from db.repositories.user_repo import UserRepo
            async with async_session() as session:
                from db.models import User
                from sqlalchemy import select
                result = await session.execute(
                    select(User).where(User.id == post.user_id)
                )
                user = result.scalar_one_or_none()
                if user:
                    await bot.send_message(
                        user.tg_user_id,
                        f"✅ Отложенный пост опубликован в {' и '.join(published_to)}.",
                    )
    finally:
        await bot.session.close()


@celery_app.task
def run_autopost_for_all_channels() -> None:
    """Запускает автопостинг для всех каналов у которых он включён."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_dispatch_autopost())
    finally:
        loop.close()


async def _dispatch_autopost() -> None:
    from db.database import async_session
    from db.models import Channel
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(
            select(Channel).where(Channel.autopost_enabled == True)  # noqa: E712
        )
        channels = result.scalars().all()

    for channel in channels:
        check_new_tg_posts.delay(channel.id)
        log.info("Запущена проверка автопостинга", channel_id=channel.id)


@celery_app.task(bind=True)
def check_new_tg_posts(self, channel_id: int) -> None:
    """Проверяет новые посты в TG-канале и публикует их в MAX (автопостинг)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_check_new_posts(channel_id))
    finally:
        loop.close()


async def _do_check_new_posts(channel_id: int) -> None:
    from db.database import async_session
    from db.models import Channel, PostMapping
    from core.telegram_reader import read_channel_posts
    from core.max_publisher import MaxPublisher
    from core.link_replacer import replace_links
    from sqlalchemy import select

    async with async_session() as session:
        channel = await session.get(Channel, channel_id)
        if not channel or not channel.autopost_enabled:
            return
        if not channel.max_channel_id:
            return

        # Последний перенесённый пост
        result = await session.execute(
            select(PostMapping)
            .where(PostMapping.channel_id == channel_id)
            .order_by(PostMapping.tg_message_id.desc())
            .limit(1)
        )
        last_mapping = result.scalar_one_or_none()
        min_id = last_mapping.tg_message_id if last_mapping else 0

    # Читаем новые посты (только свежие)
    posts = await read_channel_posts(channel.tg_channel_username, limit=50)
    new_posts = [p for p in posts if p.message_id > min_id]

    if not new_posts:
        return

    log.info("Найдены новые посты для автопостинга", count=len(new_posts), channel_id=channel_id)

    from services.link_mapping import build_link_mapping
    link_mapping = await build_link_mapping(channel.user_id)
    publisher = MaxPublisher()
    for post in new_posts:
        result = await publisher.publish_post(
            chat_id=channel.max_channel_id,
            text=post.text,
            media_path=post.media_path,
            media_type=post.media_type,
            link_mapping=link_mapping,
        )
        if result:
            max_msg_id = str(result.get("message", {}).get("id", ""))
            async with async_session() as session:
                mapping = PostMapping(
                    channel_id=channel_id,
                    tg_message_id=post.message_id,
                    max_message_id=max_msg_id,
                )
                session.add(mapping)
                await session.commit()

            log.info("Автопостинг: пост опубликован в MAX", tg_id=post.message_id)


@celery_app.task
def cleanup_old_data() -> None:
    """Чистит старые данные: transfer_jobs, media_cache."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_cleanup())
    finally:
        loop.close()


async def _do_cleanup() -> None:
    from db.database import async_session
    from db.models import TransferJob
    from sqlalchemy import delete
    from datetime import datetime, timedelta
    import os

    cutoff = datetime.utcnow() - timedelta(days=30)

    async with async_session() as session:
        # Удаляем transfer_jobs старше 30 дней
        result = await session.execute(
            delete(TransferJob).where(TransferJob.created_at < cutoff)
        )
        deleted = result.rowcount
        await session.commit()

    if deleted:
        log.info("Очистка: удалено transfer_jobs", count=deleted)

    # Чистим media_cache старше 1 часа
    media_dir = "media_cache"
    if os.path.exists(media_dir):
        import time
        now = time.time()
        cleaned = 0
        for f in os.listdir(media_dir):
            path = os.path.join(media_dir, f)
            if os.path.isfile(path) and now - os.path.getmtime(path) > 3600:
                os.remove(path)
                cleaned += 1
        if cleaned:
            log.info("Очистка: удалено файлов media_cache", count=cleaned)


@celery_app.task
def health_check() -> None:
    """Проверяет здоровье сервисов и шлёт оповещение если что-то не так."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_health_check())
    finally:
        loop.close()


async def _do_health_check() -> None:
    import aiohttp
    from bot.config import settings
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.enums import ParseMode

    ADMIN_TG_ID = settings.admin_tg_id
    issues = []
    if not ADMIN_TG_ID:
        return  # некому слать алерты — пропускаем

    # 1. Проверяем MAX API
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{settings.max_api_base_url}/me",
                headers={"Authorization": settings.max_bot_token},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    issues.append(f"MAX API: HTTP {resp.status}")
    except Exception as e:
        issues.append(f"MAX API недоступен: {e}")

    # 2. Проверяем БД
    try:
        from db.database import async_session
        from db.models import User
        from sqlalchemy import select, func
        async with async_session() as session:
            result = await session.execute(select(func.count(User.id)))
            count = result.scalar()
    except Exception as e:
        issues.append(f"БД недоступна: {e}")

    # 3. Проверяем Redis
    try:
        from tasks.celery_app import celery_app as _ca
        _ca.backend.get("health_ping")
    except Exception as e:
        issues.append(f"Redis: {e}")

    # 4. Висячие переносы — DE-воркер не подхватил
    try:
        from db.database import async_session as _ses
        from db.models import TransferJob
        from sqlalchemy import select, and_
        from datetime import datetime, timedelta
        async with _ses() as session:
            cutoff_stuck = datetime.utcnow() - timedelta(minutes=5)
            result = await session.execute(
                select(TransferJob.id, TransferJob.channel_id, TransferJob.created_at)
                .where(and_(TransferJob.status == "pending", TransferJob.created_at < cutoff_stuck))
            )
            stuck = result.all()
            if stuck:
                ids = [row[0] for row in stuck]
                issues.append(f"Перенос встал: pending job {ids} (DE-воркер не взял > 5 мин)")
    except Exception as e:
        issues.append(f"Проверка transfer_jobs: {e}")

    # 5. Оплаты ЮKassa: succeeded-платёж без активированной подписки
    try:
        import os as _os
        shop_id = _os.getenv("YOKASSA_SHOP_ID")
        secret = _os.getenv("YOKASSA_SECRET_KEY")
        if shop_id and secret:
            from datetime import datetime, timedelta, timezone as _tz
            from sqlalchemy import select as _sel
            from db.models import Subscription as _Sub
            from db.database import async_session as _sesp

            auth = aiohttp.BasicAuth(shop_id, secret)
            since = (datetime.now(_tz.utc) - timedelta(hours=2)).isoformat()
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.yookassa.ru/v3/payments",
                    auth=auth,
                    params={"created_at.gte": since, "status": "succeeded", "limit": 50},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        pdata = await resp.json()
                        yk_payments = pdata.get("items", [])
                        if yk_payments:
                            async with _sesp() as _session:
                                _pids = [p.get("id") for p in yk_payments if p.get("id")]
                                _res = await _session.execute(
                                    _sel(_Sub.payment_id).where(_Sub.payment_id.in_(_pids))
                                )
                                _active_pids = {row[0] for row in _res}
                            orphans = []
                            for p in yk_payments:
                                pid = p.get("id")
                                if pid and pid not in _active_pids:
                                    created = p.get("created_at", "")
                                    amt = p.get("amount", {}).get("value", "?")
                                    orphans.append(f"{pid} ({amt}₽, {created[:16]})")
                            if orphans:
                                issues.append(
                                    f"ЮKassa: {len(orphans)} оплат без активации — "
                                    + "; ".join(orphans[:3])
                                    + ("..." if len(orphans) > 3 else "")
                                )
                    else:
                        issues.append(f"ЮKassa API: HTTP {resp.status}")
    except Exception as e:
        issues.append(f"Проверка ЮKassa: {str(e)[:100]}")

    # Если проблемы — шлём в TG
    if issues:
        bot_session = AiohttpSession(proxy=settings.tg_api_proxy) if settings.tg_api_proxy else None
        bot = Bot(token=settings.tg_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=bot_session)
        try:
            text = "\u26a0\ufe0f <b>Бот: проблемы!</b>\n\n" + "\n".join(f"\u274c {i}" for i in issues)
            await bot.send_message(ADMIN_TG_ID, text)
        except Exception:
            pass
        finally:
            await bot.session.close()
        log.warning("Health check: проблемы", issues=issues)
