"""Хэндлеры для отложенных постов (/schedule)."""
from datetime import datetime, timezone, timedelta

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.states.schedule import ScheduleStates
from db.database import async_session
from db.models import ScheduledPost
from db.repositories.user_repo import UserRepo
from core.tariff import get_user_tariff, can_use, UPGRADE_HINTS
from sqlalchemy import select

log = structlog.get_logger()
router = Router()

TIMEZONES = [
    ("UTC+2 (Калининград)", 2),
    ("UTC+3 (Москва, СПб)", 3),
    ("UTC+4 (Самара)", 4),
    ("UTC+5 (Екатеринбург)", 5),
    ("UTC+6 (Омск)", 6),
    ("UTC+7 (Новосибирск, Красноярск)", 7),
    ("UTC+8 (Иркутск)", 8),
    ("UTC+9 (Якутск)", 9),
    ("UTC+10 (Владивосток)", 10),
    ("UTC+11 (Магадан)", 11),
    ("UTC+12 (Камчатка)", 12),
]


def timezone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"tz_{offset}")]
        for label, offset in TIMEZONES
    ])

def target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Только в TG", callback_data="target_tg")],
        [InlineKeyboardButton(text="💬 Только в MAX", callback_data="target_max")],
        [InlineKeyboardButton(text="🔁 В оба канала", callback_data="target_both")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="schedule_cancel")],
    ])


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    await state.clear()

    # Проверяем что у пользователя есть верифицированный канал
    async with async_session() as session:
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(message.from_user.id)
        if not user:
            await message.answer("Сначала нажми /start и настрой перенос.")
            return

    # Проверяем тариф — отложка только на Про
    tariff = await get_user_tariff(user.id)
    if not can_use(tariff, "schedule"):
        await message.answer(UPGRADE_HINTS["schedule"])
        return

    async with async_session() as session:
        from db.models import Channel
        channels_result = await session.execute(
            select(Channel).where(
                Channel.user_id == user.id,
                Channel.is_verified == True,  # noqa: E712
            )
        )
        channels = channels_result.scalars().all()

    if not channels:
        await message.answer(
            "У тебя нет верифицированных каналов.\n"
            "Сначала пройди настройку через /start."
        )
        return

    # Сохраняем user_id и tz для дальнейших шагов
    await state.update_data(
        user_id=user.id,
        timezone_offset=user.timezone_offset,
    )

    # Если больше одного канала — предлагаем выбор
    if len(channels) > 1:
        buttons = []
        for ch in channels:
            buttons.append([InlineKeyboardButton(
                text=f"📢 @{ch.tg_channel_username}",
                callback_data=f"sched_pick_ch_{ch.id}",
            )])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="schedule_cancel")])
        await message.answer(
            "📝 <b>Отложенный пост</b>\n\nВыбери канал:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await state.set_state(ScheduleStates.waiting_channel)
        return

    # Один канал — берём автоматически
    channel = channels[0]
    await state.update_data(
        channel_id=channel.id,
        tg_username=channel.tg_channel_username,
        max_channel_id=channel.max_channel_id,
    )

    await message.answer(
        "⚠️ <b>Важно для публикации в TG-канал:</b>\n"
        "Бот должен быть добавлен администратором в твой TG-канал "
        "с правом «Публикация сообщений».\n"
        "Для MAX этого не нужно — там уже настроено.",
        disable_notification=True,
    )

    # Если часовой пояс ещё не сохранён — спрашиваем один раз
    if user.timezone_offset is None:
        await message.answer(
            "🌍 <b>Выбери свой часовой пояс</b>\n"
            "Это нужно чтобы публиковать посты в правильное время.\n"
            "Запомним и больше спрашивать не будем.",
            reply_markup=timezone_keyboard(),
        )
        await state.set_state(ScheduleStates.waiting_timezone)
        return

    await message.answer(
        "📝 <b>Отложенный пост</b>\n\n"
        "Отправь текст поста (можно с фото или видео).\n"
        "Для отмены напиши /cancel."
    )
    await state.set_state(ScheduleStates.waiting_content)


@router.callback_query(ScheduleStates.waiting_timezone, F.data.startswith("tz_"))
async def handle_timezone_select(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    offset = int(callback.data.replace("tz_", ""))
    data = await state.get_data()

    async with async_session() as session:
        user_repo = UserRepo(session)
        await user_repo.update_timezone(data["user_id"], offset)

    await state.update_data(timezone_offset=offset)

    tz_label = next(label for label, o in TIMEZONES if o == offset)
    await callback.message.edit_text(
        f"✅ Часовой пояс сохранён: <b>{tz_label}</b>\n\n"
        "📝 <b>Отложенный пост</b>\n\n"
        "Отправь текст поста (можно с фото или видео).\n"
        "Для отмены напиши /cancel."
    )
    await state.set_state(ScheduleStates.waiting_content)


# Буфер для альбомов в отложке
_sched_album_buffer: dict[str, list] = {}
_sched_album_tasks: dict[str, "asyncio.Task"] = {}

@router.message(ScheduleStates.waiting_content)
async def handle_schedule_content(message: Message, state: FSMContext) -> None:
    import asyncio

    text = message.html_text or message.caption or ""

    if not text and not message.photo and not message.video and not message.document:
        await message.answer("Отправь текст или медиа для поста.")
        return

    # Собираем медиа
    file_info = None
    if message.photo:
        file_info = {"type": "photo", "file_id": message.photo[-1].file_id}
    elif message.video:
        file_info = {"type": "video", "file_id": message.video.file_id}
    elif message.document:
        file_info = {"type": "document", "file_id": message.document.file_id}

    # Альбом: буферизуем
    if message.media_group_id:
        gid = message.media_group_id
        if gid not in _sched_album_buffer:
            _sched_album_buffer[gid] = {"text": text, "files": []}
        if not _sched_album_buffer[gid]["text"] and text:
            _sched_album_buffer[gid]["text"] = text
        if file_info:
            _sched_album_buffer[gid]["files"].append(file_info)

        # Перезапускаем таймер
        if gid in _sched_album_tasks:
            _sched_album_tasks[gid].cancel()

        async def _finalize_album(group_id, st, msg):
            await asyncio.sleep(1.5)
            album = _sched_album_buffer.pop(group_id, None)
            _sched_album_tasks.pop(group_id, None)
            if not album:
                return
            media_data = album["files"]  # list of {type, file_id}

            # Сразу тянем в MAX, пока создаём отложку:
            # маленькие → max_attachments (публикация в MAX мгновенная)
            # >20MB → пропускаем, уведомляем пользователя
            max_attachments = []
            skipped = 0
            import os as _os
            _os.makedirs("media_cache", exist_ok=True)
            for fi in media_data:
                try:
                    _file = await msg.bot.get_file(fi["file_id"])
                except Exception as e:
                    skipped += 1
                    log.warning("Scheduled: file too big for Bot API",
                                type=fi.get("type"), err=str(e)[:80])
                    continue
                ext = ".jpg" if fi["type"] == "photo" else ".mp4" if fi["type"] == "video" else ""
                lp = f"media_cache/sched_pre_{fi['file_id'][:10]}{ext}"
                try:
                    await msg.bot.download_file(_file.file_path, lp)
                    from core.media_handler import upload_media_to_max
                    upl = await upload_media_to_max(lp, fi["type"])
                    if upl:
                        max_attachments.append(upl)
                    else:
                        skipped += 1
                finally:
                    if _os.path.exists(lp):
                        try:
                            _os.remove(lp)
                        except Exception:
                            pass

            storage = {"album": media_data}
            if max_attachments:
                storage["max_attachments"] = max_attachments
            await st.update_data(
                content_text=album["text"],
                media_file_ids=storage if len(media_data) > 1 else media_data[0],
            )
            if skipped:
                await msg.answer(
                    f"⚠️ {skipped} видео не поместилось в MAX-пост (>20 МБ — лимит Telegram Bot API для отложки через бота).\n\n"
                    f"• В TG пост выйдет полностью\n"
                    f"• В MAX — без этих видео\n\n"
                    f"Для больших видео используй: отложку через MAX-бот, либо автопост (напиши прямо в канал)."
                )
            await msg.answer("Куда опубликовать пост?", reply_markup=target_keyboard())
            await st.set_state(ScheduleStates.waiting_target)

        _sched_album_tasks[gid] = asyncio.create_task(_finalize_album(gid, state, message))
        return

    # Одиночный пост — также пробуем сразу залить в MAX
    single_max_att = None
    single_skipped = False
    if file_info:
        try:
            _file = await message.bot.get_file(file_info["file_id"])
            import os as _os
            _os.makedirs("media_cache", exist_ok=True)
            ext = ".jpg" if file_info["type"] == "photo" else ".mp4" if file_info["type"] == "video" else ""
            lp = f"media_cache/sched_pre_{file_info['file_id'][:10]}{ext}"
            try:
                await message.bot.download_file(_file.file_path, lp)
                from core.media_handler import upload_media_to_max
                upl = await upload_media_to_max(lp, file_info["type"])
                if upl:
                    single_max_att = upl
            finally:
                if _os.path.exists(lp):
                    try:
                        _os.remove(lp)
                    except Exception:
                        pass
        except Exception as e:
            single_skipped = True
            log.warning("Scheduled single: file too big", err=str(e)[:80])

    storage = file_info
    if single_max_att:
        storage = dict(file_info or {})
        storage["max_attachments"] = [single_max_att]
    await state.update_data(content_text=text, media_file_ids=storage)
    if single_skipped:
        await message.answer(
            "⚠️ Видео >20 МБ — для отложки через бота есть лимит Telegram Bot API. "
            "В TG пост выйдет нормально, в MAX — без видео.\n\n"
            "Для больших видео: отложка через MAX-бот или автопост в канал."
        )
    await message.answer(
        "Куда опубликовать пост?",
        reply_markup=target_keyboard(),
    )
    await state.set_state(ScheduleStates.waiting_target)


@router.callback_query(ScheduleStates.waiting_target, F.data.startswith("target_"))
async def handle_schedule_target(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    target = callback.data.replace("target_", "")  # tg / max / both
    await state.update_data(target=target)

    target_labels = {"tg": "TG", "max": "MAX", "both": "оба канала"}
    await callback.message.edit_text(
        f"Выбрано: <b>{target_labels[target]}</b>\n\n"
        "Когда опубликовать?\n"
        "Отправь дату и время в формате: <code>15.04.2026 14:30</code>"
    )
    await state.set_state(ScheduleStates.waiting_datetime)


@router.callback_query(ScheduleStates.waiting_target, F.data == "schedule_cancel")
async def handle_schedule_cancel_btn(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.")


@router.message(ScheduleStates.waiting_datetime)
async def handle_schedule_datetime(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()

    data = await state.get_data()
    offset = data.get("timezone_offset", 3)
    user_tz = timezone(timedelta(hours=offset))

    try:
        publish_at_naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
        publish_at = publish_at_naive.replace(tzinfo=user_tz).astimezone(timezone.utc)
    except ValueError:
        await message.answer(
            "Не удалось распознать дату. Используй формат:\n"
            "<code>15.04.2026 14:30</code>"
        )
        return

    if publish_at <= datetime.now(timezone.utc):
        await message.answer("Дата должна быть в будущем. Попробуй ещё раз.")
        return

    async with async_session() as session:
        scheduled = ScheduledPost(
            user_id=data["user_id"],
            channel_id=data["channel_id"],
            content_text=data.get("content_text"),
            media_file_ids=data.get("media_file_ids"),
            target=data["target"],
            publish_at=publish_at.replace(tzinfo=None),  # naive UTC в БД
            status="pending",
        )
        session.add(scheduled)
        await session.commit()
        await session.refresh(scheduled)
        post_id = scheduled.id

    # Планируем задачу через Celery с нужным временем
    from tasks.scheduled_tasks import publish_scheduled_post
    publish_scheduled_post.apply_async(
        args=[post_id],
        eta=publish_at,
    )

    target_labels = {"tg": "TG", "max": "MAX", "both": "TG и MAX"}
    tz_label = next((label for label, o in TIMEZONES if o == offset), f"UTC+{offset}")
    local_time = publish_at.astimezone(user_tz).strftime("%d.%m.%Y %H:%M")
    await message.answer(
        f"✅ <b>Пост запланирован!</b>\n\n"
        f"📅 Дата: <b>{local_time} ({tz_label})</b>\n"
        f"📢 Куда: <b>{target_labels[data['target']]}</b>\n\n"
        "В указанное время пост будет опубликован автоматически."
    )
    await state.clear()


@router.message(Command("scheduled"))
async def cmd_scheduled_list(message: Message, state: FSMContext) -> None:
    """Список отложенных постов с кнопками отмены/переноса."""
    await state.clear()
    async with async_session() as session:
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(message.from_user.id)
        if not user:
            await message.answer("Сначала нажми /start.")
            return
        from sqlalchemy import func as _sqlfunc
        total_result = await session.execute(
            select(_sqlfunc.count(ScheduledPost.id)).where(
                ScheduledPost.user_id == user.id,
                ScheduledPost.status == "pending",
            )
        )
        total = total_result.scalar() or 0
        result = await session.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.user_id == user.id,
                ScheduledPost.status == "pending",
            )
            .order_by(ScheduledPost.publish_at.asc())
            .limit(10)
        )
        posts = result.scalars().all()
        tz_offset = user.timezone_offset or 3

    if not posts:
        await message.answer(
            "У тебя нет запланированных постов.\n"
            "Создать новый — /schedule"
        )
        return

    user_tz = timezone(timedelta(hours=tz_offset))
    target_labels = {"tg": "TG", "max": "MAX", "both": "TG+MAX"}

    header = f"📋 <b>Запланированные посты ({total}):</b>"
    if total > 10:
        header += f"\n\n<i>Показаны ближайшие 10. Чтобы управлять остальными — удали/опубликуй ближайшие.</i>"
    await message.answer(header)
    for post in posts:
        local_dt = post.publish_at.replace(tzinfo=timezone.utc).astimezone(user_tz)
        when = local_dt.strftime("%d.%m.%Y %H:%M")
        preview = (post.content_text or "(без текста)")[:100]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"sched_edit_{post.id}"),
                InlineKeyboardButton(text="🕐 Перенести", callback_data=f"sched_resch_{post.id}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sched_del_{post.id}"),
            ],
        ])
        await message.answer(
            f"📅 <b>{when}</b> ({target_labels.get(post.target, post.target)})\n{preview}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("sched_del_"))
async def handle_scheduled_delete(callback: CallbackQuery) -> None:
    post_id = int(callback.data.replace("sched_del_", ""))
    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(callback.from_user.id)
        if not post or not user or post.user_id != user.id:
            await callback.answer("Пост не найден", show_alert=True)
            return
        if post.status != "pending":
            await callback.answer("Пост уже опубликован или отменён", show_alert=True)
            return
        post.status = "cancelled"
        await session.commit()
    await callback.answer("Удалено")
    await callback.message.edit_text("🗑 Отложенный пост отменён.")


@router.callback_query(F.data.startswith("sched_resch_"))
async def handle_scheduled_reschedule(callback: CallbackQuery, state: FSMContext) -> None:
    post_id = int(callback.data.replace("sched_resch_", ""))
    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(callback.from_user.id)
        if not post or not user or post.user_id != user.id:
            await callback.answer("Пост не найден", show_alert=True)
            return
        if post.status != "pending":
            await callback.answer("Пост уже опубликован или отменён", show_alert=True)
            return
        tz_offset = user.timezone_offset or 3
    await state.update_data(reschedule_post_id=post_id, reschedule_tz=tz_offset)
    await state.set_state(ScheduleStates.waiting_reschedule)
    await callback.answer()
    await callback.message.answer(
        "🕐 Введи новое время публикации в формате:\n"
        "<code>15.04.2026 14:30</code>\n\nИли /cancel чтобы отменить."
    )


@router.message(ScheduleStates.waiting_reschedule)
async def handle_reschedule_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    offset = data.get("reschedule_tz", 3)
    user_tz = timezone(timedelta(hours=offset))

    try:
        publish_at_naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
        publish_at = publish_at_naive.replace(tzinfo=user_tz).astimezone(timezone.utc)
    except ValueError:
        await message.answer(
            "Не удалось распознать дату. Используй формат:\n<code>15.04.2026 14:30</code>"
        )
        return

    if publish_at <= datetime.now(timezone.utc):
        await message.answer("Дата должна быть в будущем. Попробуй ещё раз.")
        return

    post_id = data["reschedule_post_id"]
    async with async_session() as session:
        old = await session.get(ScheduledPost, post_id)
        if not old or old.status != "pending":
            await message.answer("Пост уже не существует.")
            await state.clear()
            return
        old.status = "cancelled"
        new_post = ScheduledPost(
            user_id=old.user_id,
            channel_id=old.channel_id,
            content_text=old.content_text,
            media_file_ids=old.media_file_ids,
            target=old.target,
            publish_at=publish_at.replace(tzinfo=None),
            status="pending",
        )
        session.add(new_post)
        await session.commit()
        await session.refresh(new_post)
        new_id = new_post.id

    from tasks.scheduled_tasks import publish_scheduled_post
    publish_scheduled_post.apply_async(args=[new_id], eta=publish_at)

    local_time = publish_at.astimezone(user_tz).strftime("%d.%m.%Y %H:%M")
    await message.answer(f"✅ Время изменено на <b>{local_time}</b>.")
    await state.clear()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current:
        await state.clear()
        await message.answer("Отменено. Нажми /start или /schedule чтобы начать заново.")
    else:
        await message.answer("Нечего отменять.")



# --- Выбор канала для отложки при нескольких каналах ---
@router.callback_query(ScheduleStates.waiting_channel, F.data.startswith("sched_pick_ch_"))
async def handle_pick_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    ch_id = int(callback.data.replace("sched_pick_ch_", ""))
    async with async_session() as session:
        from db.models import Channel
        channel = await session.get(Channel, ch_id)
        if not channel:
            await callback.message.edit_text("Канал не найден. Попробуй снова /schedule")
            await state.clear()
            return
        await state.update_data(
            channel_id=channel.id,
            tg_username=channel.tg_channel_username,
            max_channel_id=channel.max_channel_id,
        )

    data = await state.get_data()
    await callback.message.edit_text(
        f"✅ Канал: <b>@{channel.tg_channel_username}</b>\n\n"
        "⚠️ <b>Важно для публикации в TG:</b> бот должен быть админом в TG-канале."
    )

    # Часовой пояс
    if data.get("timezone_offset") is None:
        await callback.message.answer(
            "🌍 <b>Выбери свой часовой пояс</b>",
            reply_markup=timezone_keyboard(),
        )
        await state.set_state(ScheduleStates.waiting_timezone)
        return

    await callback.message.answer(
        "📝 Отправь текст поста (можно с фото или видео).\nДля отмены /cancel."
    )
    await state.set_state(ScheduleStates.waiting_content)


@router.callback_query(ScheduleStates.waiting_channel, F.data == "schedule_cancel")
async def handle_schedule_cancel_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    await callback.message.edit_text("Отложка отменена.")


# --- Редактирование отложенного поста ---
@router.callback_query(F.data.startswith("sched_edit_"))
async def handle_scheduled_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    post_id = int(callback.data.replace("sched_edit_", ""))
    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(callback.from_user.id)
        if not post or not user or post.user_id != user.id:
            await callback.answer("Пост не найден", show_alert=True)
            return
        if post.status != "pending":
            await callback.answer("Пост уже опубликован", show_alert=True)
            return
    await callback.answer()
    await state.update_data(edit_post_id=post_id)
    await state.set_state(ScheduleStates.waiting_edit_content)
    await callback.message.answer(
        "✏️ Отправь новый текст поста (с медиа или без).\nДля отмены /cancel."
    )


@router.message(ScheduleStates.waiting_edit_content)
async def handle_scheduled_edit_content(message: Message, state: FSMContext) -> None:
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Редактирование отменено.")
        return

    data = await state.get_data()
    post_id = data.get("edit_post_id")
    if not post_id:
        await state.clear()
        return

    new_text = message.text or message.caption or ""
    media_file_id = None
    media_type = None
    if message.photo:
        media_type = "photo"
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_file_id = message.video.file_id
    elif message.document:
        media_type = "document"
        media_file_id = message.document.file_id

    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        if not post:
            await state.clear()
            await message.answer("Пост не найден.")
            return
        post.content_text = new_text
        if media_file_id:
            post.media_file_ids = {"type": media_type, "file_id": media_file_id}
        await session.commit()

    await state.clear()
    await message.answer("✅ Текст обновлён. Список: /scheduled")
