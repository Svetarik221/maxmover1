"""Webhook-сервер для MAX API и ЮKassa + API комментариев + MAX long polling."""

import asyncio
import json

import structlog
from aiohttp import web
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from core.tariff import get_channel_tariff, can_use

log = structlog.get_logger()



# ─── API комментариев для мини-аппа ───


async def handle_get_comments(request: web.Request) -> web.Response:
    """GET /api/comments?post_id=xxx — список комментариев к посту."""
    post_id = request.query.get("post_id", "")
    if not post_id:
        return web.json_response({"error": "post_id required"}, status=400)

    from db.database import async_session
    from sqlalchemy import select
    from db.models import Comment

    async with async_session() as session:
        result = await session.execute(
            select(Comment)
            .where(Comment.max_message_id == post_id)
            .order_by(Comment.created_at.asc())
        )
        comments = result.scalars().all()

    return web.json_response({
        "comments": [
            {
                "id": c.id,
                "user_name": c.user_name,
                "text": c.text,
                "created_at": c.created_at.isoformat(),
            }
            for c in comments
        ]
    })


async def handle_post_comment(request: web.Request) -> web.Response:
    """POST /api/comments — добавить комментарий."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    post_id = data.get("post_id", "")
    chat_id = data.get("chat_id", "")
    user_id = data.get("user_id", 0)
    user_name = data.get("user_name", "").strip()
    text = data.get("text", "").strip()

    if not all([post_id, chat_id, user_id, user_name, text]):
        return web.json_response({"error": "missing fields"}, status=400)

    if len(text) > 1000:
        return web.json_response({"error": "text too long (max 1000)"}, status=400)

    from db.database import async_session
    from db.models import Comment

    async with async_session() as session:
        comment = Comment(
            max_message_id=str(post_id),
            max_chat_id=str(chat_id),
            max_user_id=int(user_id),
            user_name=user_name,
            text=text,
        )
        session.add(comment)
        await session.commit()
        await session.refresh(comment)

    # Считаем общее количество комментариев к посту
    from sqlalchemy import func as sa_func, select

    async with async_session() as session:
        result = await session.execute(
            select(sa_func.count()).where(Comment.max_message_id == str(post_id))
        )
        count = result.scalar() or 0

    log.info("Комментарий добавлен", post_id=post_id, user_name=user_name, count=count)

    # Обновляем счётчик на кнопке поста в MAX
    asyncio.create_task(
        _update_comment_button(str(post_id), str(chat_id), count)
    )

    return web.json_response({
        "ok": True,
        "comment": {
            "id": comment.id,
            "user_name": comment.user_name,
            "text": comment.text,
            "created_at": comment.created_at.isoformat(),
        }
    })


async def _update_comment_button(post_id: str, chat_id: str, count: int) -> None:
    """Обновляет текст кнопки комментариев на посте в MAX."""
    import aiohttp

    base_url = settings.max_api_base_url
    headers = {"Authorization": settings.max_bot_token}

    safe_mid = post_id.replace(".", "-")
    safe_chat = chat_id.replace("-", "n")
    payload = f"{safe_mid}__{safe_chat}"

    btn_text = f"💬 Комментарии ({count})" if count > 0 else "💬 Комментарии"

    try:
        async with aiohttp.ClientSession() as session:
            # GET текущие attachments чтобы не затереть media
            cur_atts = []
            async with session.get(f"{base_url}/messages?message_ids={post_id}", headers=headers) as gr:
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
                        "text": btn_text,
                        "web_app": settings.max_bot_username,
                        "payload": payload,
                    }]]
                }
            })
            edit_body = {"attachments": new_atts}

            async with session.put(
                f"{base_url}/messages?message_id={post_id}",
                headers=headers,
                json=edit_body,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("Не удалось обновить кнопку", status=resp.status, body=body)
    except Exception:
        log.exception("Ошибка обновления кнопки комментариев")


# ─── MAX webhook (новые посты в каналах) ───


async def handle_max_webhook(request: web.Request) -> web.Response:
    """Обрабатывает webhook от MAX."""
    try:
        data = await request.json()
        update_type = data.get("update_type", "")
        print(f"[MAX WEBHOOK] type={update_type}", flush=True)

        if update_type == "message_created":
            message = data.get("message", {})
            chat_type = message.get("recipient", {}).get("chat_type", "")

            if chat_type in ("channel", "chat"):
                await _handle_channel_post(data)
            elif chat_type == "dialog":
                await _handle_bot_dm(data)

        elif update_type == "bot_started":
            await _handle_bot_started(data)

        elif update_type == "message_callback":
            await _handle_bot_callback(data)

    except Exception as e:
        print(f"[MAX WEBHOOK ERROR] {e}", flush=True)
        log.exception("Ошибка обработки MAX webhook")

    return web.json_response({"ok": True})


def _max_markup_to_html(text, markup):
    """MAX markup -> HTML."""
    if not markup or not text:
        return text
    sorted_m = sorted(markup, key=lambda m: m.get("from", 0), reverse=True)
    r = text
    for m in sorted_m:
        s = m.get("from", 0)
        l = m.get("length", 0)
        e = s + l
        inner = r[s:e]
        mt = m.get("type", "")
        if mt == "strong":
            r = r[:s] + "<b>" + inner + "</b>" + r[e:]
        elif mt == "italic":
            r = r[:s] + "<i>" + inner + "</i>" + r[e:]
        elif mt == "code":
            r = r[:s] + "<code>" + inner + "</code>" + r[e:]
        elif mt == "link":
            url = m.get("url", "")
            if url:
                r = r[:s] + '<a href="' + url + '">' + inner + "</a>" + r[e:]
        elif mt == "strikethrough":
            r = r[:s] + "<s>" + inner + "</s>" + r[e:]
        elif mt == "underline":
            r = r[:s] + "<u>" + inner + "</u>" + r[e:]
    return r


async def _handle_channel_post(update: dict) -> None:
    """Новый пост в MAX-канале: комментарии + кроспостинг в TG."""
    import aiohttp

    message = update.get("message", {})
    recipient = message.get("recipient", {})
    chat_type = recipient.get("chat_type", "")
    chat_id = recipient.get("chat_id")
    sender = message.get("sender", {})
    mid = message.get("body", {}).get("mid", "")
    text = message.get("body", {}).get("text", "")
    markup = message.get("body", {}).get("markup", [])
    attachments = message.get("body", {}).get("attachments", [])

    # Конвертируем MAX markup в HTML для кроспостинга
    if markup and text:
        text = _max_markup_to_html(text, markup)

    print(f"[MAX POST] chat_type={chat_type} chat_id={chat_id} mid={mid}", flush=True)

    if chat_type not in ("channel", "chat") or not mid or not chat_id:
        return

    bot_id = 255015229
    is_own_message = sender.get("user_id") == bot_id

    # Ищем канал в БД
    from db.database import async_session
    from sqlalchemy import select
    from db.models import Channel

    async with async_session() as session:
        result = await session.execute(
            select(Channel).where(Channel.max_channel_id == str(chat_id)).limit(1)
        )
        channel = result.scalar_one_or_none()

    if not channel:
        return

    # 1. Кнопка комментариев — только для чужих постов (свои получают кнопку при публикации)
    if not is_own_message and channel.comments_enabled:
        tariff = await get_channel_tariff(channel.id)
        if can_use(tariff, "comments"):
            await _add_comment_button_webhook(mid, chat_id, attachments)

    # 2. Кроспостинг MAX→TG — только чужие сообщения (иначе петля)
    if not is_own_message and channel.crosspost_enabled and (channel.tg_channel_id or channel.tg_channel_username):
        tariff = await get_channel_tariff(channel.id)
        if can_use(tariff, "crosspost"):
            await _crosspost_to_tg(channel, text, attachments)


async def _add_comment_button_webhook(mid: str, chat_id: int, existing_attachments: list = None) -> None:
    """Добавляет кнопку комментариев к посту в MAX, сохраняя существующие attachments (media)."""
    import aiohttp

    print(f"[MAX POST] Добавляем кнопку комментариев mid={mid}", flush=True)

    safe_mid = mid.replace(".", "-")
    safe_chat = str(chat_id).replace("-", "n")
    payload = f"{safe_mid}__{safe_chat}"

    # Сохраняем существующие медиа (фильтруем старые inline_keyboard если были)
    new_attachments = [
        a for a in (existing_attachments or [])
        if a.get("type") != "inline_keyboard"
    ]
    new_attachments.append({
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

    edit_body = {"attachments": new_attachments}

    base_url = settings.max_api_base_url
    headers = {"Authorization": settings.max_bot_token}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.put(
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


async def _crosspost_to_tg(channel, text: str, max_attachments: list) -> None:
    """Кроспостинг: публикует пост из MAX в TG-канал."""
    import aiohttp
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    # Автозамена MAX-ссылок на TG-ссылки
    from services.link_mapping import build_reverse_link_mapping
    from core.link_replacer import replace_max_links
    try:
        reverse_mapping = await build_reverse_link_mapping(channel.user_id)
        if reverse_mapping and text:
            text = replace_max_links(text, reverse_mapping)
    except Exception:
        pass

    print(f"[CROSSPOST] MAX→TG для канала {channel.tg_channel_username}", flush=True)

    session = AiohttpSession(proxy=settings.tg_api_proxy) if settings.tg_api_proxy else None
    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )

    tg_chat = channel.tg_channel_id or f"@{channel.tg_channel_username}"

    try:
        import os
        from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument
        os.makedirs("media_cache", exist_ok=True)

        # Собираем все медиа из аттачментов MAX
        medias = []  # [(local_path, type)]
        for idx, att in enumerate(max_attachments):
            att_type = att.get("type", "")
            payload = att.get("payload", {})
            url = payload.get("url") or att.get("url")
            if not url or att_type == "inline_keyboard":
                continue
            # Определяем тип
            if att_type in ("image", "photo"):
                kind = "photo"
                ext = ".jpg"
            elif att_type == "video":
                kind = "video"
                ext = ".mp4"
            elif att_type in ("file", "document"):
                kind = "document"
                ext = ""
            else:
                continue
            local_path = f"media_cache/crosspost_{channel.id}_{idx}{ext}"
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url) as resp:
                        if resp.status == 200:
                            with open(local_path, "wb") as f:
                                f.write(await resp.read())
                            medias.append((local_path, kind))
            except Exception as e:
                print(f"[CROSSPOST] download error: {e}", flush=True)

        if len(medias) > 1:
            # Альбом: send_media_group
            mg = []
            for i, (path, kind) in enumerate(medias):
                file = FSInputFile(path)
                # Caption на ПЕРВОМ элементе альбома
                cap = text if i == 0 and text else None
                if kind == "photo":
                    mg.append(InputMediaPhoto(media=file, caption=cap))
                elif kind == "video":
                    mg.append(InputMediaVideo(media=file, caption=cap))
                else:
                    mg.append(InputMediaDocument(media=file, caption=cap))
            await bot.send_media_group(chat_id=tg_chat, media=mg)
        elif len(medias) == 1:
            path, kind = medias[0]
            file = FSInputFile(path)
            if kind == "photo":
                await bot.send_photo(chat_id=tg_chat, photo=file, caption=text or None)
            elif kind == "video":
                await bot.send_video(chat_id=tg_chat, video=file, caption=text or None)
            else:
                await bot.send_document(chat_id=tg_chat, document=file, caption=text or None)
        elif text:
            await bot.send_message(chat_id=tg_chat, text=text)

        # Чистим локальные файлы
        for path, _ in medias:
            try:
                os.remove(path)
            except Exception:
                pass

        print(f"[CROSSPOST] Опубликовано в TG ({len(medias)} медиа)", flush=True)
        log.info("Кроспостинг MAX→TG", channel=channel.tg_channel_username, medias=len(medias))

    except Exception:
        log.exception("Ошибка кроспостинга MAX→TG")
    finally:
        await bot.session.close()


async def _handle_bot_started(data: dict) -> None:
    """Пользователь нажал Старт у MAX-бота."""
    from services.max_cabinet import handle_start
    user = data.get("user", {})
    user_id = user.get("user_id", 0)
    user_name = user.get("first_name") or user.get("name") or "Пользователь"
    if user_id:
        print(f"[MAX BOT] start from user {user_id} ({user_name})", flush=True)
        await handle_start(user_id, user_name)


async def _handle_bot_dm(data: dict) -> None:
    """Личное сообщение MAX-боту."""
    from services.max_cabinet import handle_start, get_schedule_state, handle_schedule_text, handle_schedule_datetime
    message = data.get("message", {})
    sender = message.get("sender", {})
    user_id = sender.get("user_id", 0)
    user_name = sender.get("first_name") or sender.get("name") or "Пользователь"
    text = message.get("body", {}).get("text", "").strip()
    attachments = message.get("body", {}).get("attachments", []) or []

    if not user_id:
        return

    print(f"[MAX DM] user={user_id} text={text[:50]} atts={len(attachments)}", flush=True)

    # Проверяем FSM-состояние отложки
    state = get_schedule_state(user_id)
    if state:
        if state.get("step") == "text":
            body_data = message.get("body", {})
            msg_markup = body_data.get("markup", [])
            # Конвертируем markup в HTML перед сохранением
            if msg_markup and text:
                text = _max_markup_to_html(text, msg_markup)
            await handle_schedule_text(user_id, text, attachments)
            return
        elif state.get("step") == "datetime":
            await handle_schedule_datetime(user_id, text)
            return
        elif state.get("step") == "reschedule_datetime":
            from services.max_cabinet import handle_scheduled_reschedule_input
            await handle_scheduled_reschedule_input(user_id, text)
            return
        elif state.get("step") == "edit_text":
            from services.max_cabinet import handle_scheduled_edit_input
            await handle_scheduled_edit_input(user_id, text)
            return
        elif state.get("step") == "set_link":
            from services.max_cabinet import handle_set_link_input
            await handle_set_link_input(user_id, text)
            return
        elif state.get("step") == "find_channel_name":
            from services.max_cabinet import handle_find_channel_input
            await handle_find_channel_input(user_id, text)
            return

    # Иначе — главное меню
    await handle_start(user_id, user_name)


async def _handle_bot_callback(data: dict) -> None:
    """Нажатие на inline-кнопку в MAX-боте."""
    from services.max_cabinet import (
        answer_callback, handle_my_channels, handle_find_channels,
        handle_connect_channel, handle_toggle,
    )
    callback = data.get("callback", {})
    callback_id = callback.get("callback_id", "")
    payload = callback.get("payload", "")
    user = callback.get("user", {})
    user_id = user.get("user_id", 0)

    if not user_id or not payload:
        return

    print(f"[MAX CALLBACK] user={user_id} payload={payload}", flush=True)

    await answer_callback(callback_id)

    if payload == "my_channels":
        await handle_my_channels(user_id)
    elif payload == "find_channels":
        await handle_find_channels(user_id)
    elif payload == "schedule_start":
        from services.max_cabinet import handle_schedule_start
        await handle_schedule_start(user_id)
    elif payload.startswith("sched_ch_"):
        from services.max_cabinet import handle_schedule_select_channel
        ch_id = int(payload.split("_")[-1])
        await handle_schedule_select_channel(user_id, ch_id)
    elif payload.startswith("sched_target_"):
        from services.max_cabinet import handle_schedule_target
        target = payload.replace("sched_target_", "")
        await handle_schedule_target(user_id, target)
    elif payload.startswith("sched_tz_"):
        from services.max_cabinet import handle_schedule_timezone
        tz = int(payload.replace("sched_tz_", ""))
        await handle_schedule_timezone(user_id, tz)
    elif payload.startswith("connect_channel_"):
        max_chat_id = payload.replace("connect_channel_", "")
        await handle_connect_channel(user_id, max_chat_id)
    elif payload.startswith("mtoggle_comments_"):
        ch_id = int(payload.split("_")[-1])
        await handle_toggle(user_id, "comments_enabled", ch_id)
    elif payload.startswith("mtoggle_autopost_"):
        ch_id = int(payload.split("_")[-1])
        await handle_toggle(user_id, "autopost_enabled", ch_id)
    elif payload.startswith("mtoggle_crosspost_"):
        ch_id = int(payload.split("_")[-1])
        await handle_toggle(user_id, "crosspost_enabled", ch_id)
    elif payload == "buy_tariff":
        from services.max_cabinet import handle_buy_tariff
        await handle_buy_tariff(user_id)
    elif payload == "mbuy_start":
        from services.max_cabinet import handle_buy_payment
        await handle_buy_payment(user_id, "start")
    elif payload == "mbuy_pro":
        from services.max_cabinet import handle_buy_payment
        await handle_buy_payment(user_id, "pro")
    elif payload.startswith("mbuy_start_ch_"):
        from services.max_cabinet import handle_buy_start_for_channel
        ch_id = int(payload.split("_")[-1])
        await handle_buy_start_for_channel(user_id, ch_id)
    elif payload == "scheduled_list":
        from services.max_cabinet import handle_scheduled_list
        await handle_scheduled_list(user_id)
    elif payload.startswith("msched_del_"):
        from services.max_cabinet import handle_scheduled_delete
        await handle_scheduled_delete(user_id, int(payload.split("_")[-1]))
    elif payload.startswith("msched_resch_"):
        from services.max_cabinet import handle_scheduled_reschedule_start
        await handle_scheduled_reschedule_start(user_id, int(payload.split("_")[-1]))
    elif payload.startswith("msched_edit_"):
        from services.max_cabinet import handle_scheduled_edit_start
        await handle_scheduled_edit_start(user_id, int(payload.split("_")[-1]))
    elif payload == "show_help":
        from services.max_cabinet import handle_help
        await handle_help(user_id)
    elif payload == "main_menu":
        from services.max_cabinet import handle_start
        user_name = data.get("callback", {}).get("user", {}).get("first_name", "Пользователь")
        await handle_start(user_id, user_name)
    elif payload == "show_privacy":
        from services.max_cabinet import handle_privacy
        await handle_privacy(user_id)
    elif payload.startswith("mset_link_"):
        from services.max_cabinet import handle_set_link_start
        ch_id = int(payload.split("_")[-1])
        await handle_set_link_start(user_id, ch_id)



TRANSFER_API_SECRET = "MxTransfer2026Key"


async def handle_pending_transfers(request):
    """Отдаёт список ожидающих переносов для DE-воркера."""
    if request.headers.get("Authorization") != f"Bearer {TRANSFER_API_SECRET}":
        return web.json_response({"error": "unauthorized"}, status=401)

    from db.database import async_session
    from db.models import Channel, TransferJob
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(
            select(TransferJob).where(TransferJob.status == "pending").limit(5)
        )
        jobs = result.scalars().all()

        pending = []
        for job in jobs:
            channel = await session.get(Channel, job.channel_id)
            if not channel:
                continue
            pending.append({
                "job_id": job.id,
                "channel_id": job.channel_id,
                "tg_username": channel.tg_channel_username,
                "max_channel_id": channel.max_channel_id,
                "total_posts": job.total_posts,
                "chat_id": job.error_message,  # chat_id сохранён в error_message
            })

    return web.json_response({"jobs": pending})


async def handle_transfer_complete(request):
    """DE-воркер отчитывается о завершении переноса."""
    if request.headers.get("Authorization") != f"Bearer {TRANSFER_API_SECRET}":
        return web.json_response({"error": "unauthorized"}, status=401)

    data = await request.json()
    job_id = data.get("job_id")
    transferred = data.get("transferred", 0)
    error = data.get("error")
    chat_id = data.get("chat_id")

    from db.database import async_session
    from db.models import TransferJob
    from datetime import datetime

    async with async_session() as session:
        job = await session.get(TransferJob, job_id)
        if job:
            job.status = "done" if not error else "failed"
            job.transferred_posts = transferred
            job.error_message = error if error else None
            job.completed_at = datetime.utcnow()
            await session.commit()

    # Уведомляем юзера в TG
    if chat_id:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.enums import ParseMode
        from bot.config import settings

        bot_session = AiohttpSession(proxy=settings.tg_api_proxy) if settings.tg_api_proxy else None
        bot = Bot(token=settings.tg_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=bot_session)
        try:
            if error:
                await bot.send_message(chat_id, f"❌ Ошибка переноса: {error}")
            else:
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить ещё канал", callback_data="add_channel")],
                    [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="open_my_hint")],
                ])
                await bot.send_message(
                    chat_id,
                    f"✅ Готово! Перенесено <b>{transferred}</b> постов.\n"
                    "Можешь убрать код из описания TG-канала.\n\n"
                    "💬 Подключить комментарии — открой кабинет в MAX-боте.",
                    reply_markup=kb,
                )
        except Exception:
            pass
        finally:
            await bot.session.close()

    return web.json_response({"ok": True})


async def handle_transfer_start(request):
    """Помечает задачу как in_progress."""
    if request.headers.get("Authorization") != f"Bearer {TRANSFER_API_SECRET}":
        return web.json_response({"error": "unauthorized"}, status=401)

    data = await request.json()
    job_id = data.get("job_id")

    from db.database import async_session
    from db.models import TransferJob
    from datetime import datetime

    async with async_session() as session:
        job = await session.get(TransferJob, job_id)
        if job:
            job.status = "in_progress"
            job.started_at = datetime.utcnow()
            await session.commit()

    return web.json_response({"ok": True})


def create_app() -> web.Application:
    app = web.Application()

    # Webhooks
    app.router.add_post("/webhook/max", handle_max_webhook)

    # API комментариев
    app.router.add_get("/api/comments", handle_get_comments)
    app.router.add_post("/api/comments", handle_post_comment)

    # Transfer API для DE-воркера
    app.router.add_get("/api/pending_transfers", handle_pending_transfers)
    app.router.add_post("/api/transfer_complete", handle_transfer_complete)
    app.router.add_post("/api/transfer_start", handle_transfer_start)

    app.on_startup.append(_listen_transfer_done)
    return app


async def _listen_transfer_done(app):
    """Опрашивает MAX служебный канал для результатов переноса."""
    import aiohttp
    import json as _json
    from bot.config import settings

    async def listener():
        seen_mids = set()
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://platform-api.max.ru/messages",
                        headers={"Authorization": settings.max_bot_token},
                        params={"chat_id": settings.relay_chat_id, "count": 20},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            await _aio.sleep(15)
                            continue
                        data = await resp.json(content_type=None)

                    for msg in data.get("messages", []):
                        mid = msg.get("body", {}).get("mid", "")
                        text = msg.get("body", {}).get("text", "")
                        if mid in seen_mids:
                            continue
                        seen_mids.add(mid)

                        # _MAP_ сообщения: записываем post_mapping
                        if text.startswith("_MAP_"):
                            try:
                                map_data = _json.loads(text[len("_MAP_"):])
                                from db.database import async_session as db_session
                                from db.models import PostMapping
                                async with db_session() as session:
                                    for item in map_data.get("mappings", []):
                                        existing = await session.execute(
                                            __import__("sqlalchemy").select(PostMapping).where(
                                                PostMapping.channel_id == item["channel_id"],
                                                PostMapping.tg_message_id == item["tg_id"],
                                            )
                                        )
                                        if not existing.scalar_one_or_none():
                                            session.add(PostMapping(
                                                channel_id=item["channel_id"],
                                                tg_message_id=item["tg_id"],
                                                max_message_id=item.get("max_mid", ""),
                                            ))
                                    await session.commit()
                            except Exception as _e:
                                print(f"[MAP ERROR] {_e}", flush=True)
                            continue

                        if not text.startswith("_DONE_"):
                            continue

                        try:
                            item = _json.loads(text[len("_DONE_"):])
                        except _json.JSONDecodeError:
                            continue

                        job_id = item.get("job_id")
                        transferred = item.get("transferred", 0)
                        error = item.get("error")
                        chat_id = item.get("chat_id")

                        from db.database import async_session as db_session
                        from db.models import TransferJob
                        from datetime import datetime
                        async with db_session() as session:
                            job = await session.get(TransferJob, job_id)
                            if job and job.status not in ("done", "failed"):
                                job.status = "done" if not error else "failed"
                                job.transferred_posts = transferred
                                job.error_message = error if error else None
                                job.completed_at = datetime.utcnow()
                                await session.commit()
                            else:
                                continue

                        if chat_id:
                            from aiogram import Bot
                            from aiogram.client.default import DefaultBotProperties
                            from aiogram.client.session.aiohttp import AiohttpSession
                            from aiogram.enums import ParseMode
                            bot_session = AiohttpSession(proxy=settings.tg_api_proxy) if settings.tg_api_proxy else None
                            bot = Bot(token=settings.tg_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=bot_session)
                            try:
                                if error:
                                    await bot.send_message(int(chat_id), f"\u274c Ошибка переноса: {error}")
                                else:
                                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                                    kb = InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="\u2795 Добавить ещё канал", callback_data="add_channel")],
                                        [InlineKeyboardButton(text="\U0001f464 Личный кабинет", callback_data="open_my_hint")],
                                    ])
                                    await bot.send_message(
                                        int(chat_id),
                                        f"\u2705 Готово! Перенесено <b>{transferred}</b> постов.\n"
                                        "Можешь убрать код из описания TG-канала.\n\n"
                                        "\U0001f4ac Подключить комментарии — открой кабинет в MAX-боте.",
                                        reply_markup=kb,
                                    )
                            except Exception:
                                pass
                            finally:
                                await bot.session.close()

                        print(f"[TRANSFER DONE] job={job_id} transferred={transferred}", flush=True)

            except Exception as e:
                print(f"[TRANSFER LISTENER] error: {e}", flush=True)

            await _aio.sleep(15)

    import asyncio as _aio
    _aio.create_task(listener())


if __name__ == "__main__":
    import sys
    print("MAX webhook сервер запускается...", flush=True, file=sys.stderr)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8443)
