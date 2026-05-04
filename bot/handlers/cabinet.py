import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.states.transfer import TransferStates
from db.database import async_session
from db.models import Channel, User
from db.repositories.user_repo import UserRepo
from sqlalchemy import select

log = structlog.get_logger()
router = Router()


def _channel_settings_kb(channel: Channel) -> InlineKeyboardMarkup:
    """Клавиатура настроек канала. В open mode все фичи доступны."""
    buttons = []
    icon = "✅" if channel.autopost_enabled else "❌"
    buttons.append([InlineKeyboardButton(
        text=f"{icon} Автопостинг TG→MAX",
        callback_data=f"toggle_autopost_{channel.id}",
    )])
    icon = "✅" if channel.crosspost_enabled else "❌"
    buttons.append([InlineKeyboardButton(
        text=f"{icon} Кроспостинг MAX→TG",
        callback_data=f"toggle_crosspost_{channel.id}",
    )])
    icon = "✅" if channel.comments_enabled else "❌"
    buttons.append([InlineKeyboardButton(
        text=f"{icon} Комментарии в MAX",
        callback_data=f"toggle_comments_{channel.id}",
    )])
    buttons.append([InlineKeyboardButton(
        text="🔗 Публичная ссылка MAX-канала",
        callback_data=f"set_max_link_{channel.id}",
    )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("my"))
async def cmd_my(message: Message, state: FSMContext) -> None:
    """Личный кабинет пользователя."""
    await state.clear()

    async with async_session() as session:
        user_repo = UserRepo(session)
        user = await user_repo.get_or_create(
            tg_user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )

        channels_result = await session.execute(
            select(Channel).where(Channel.user_id == user.id)
        )
        channels = channels_result.scalars().all()

    lines = ["👤 <b>Личный кабинет</b>\n"]

    if not channels:
        lines.append("У тебя пока нет привязанных каналов.\nНажми /start чтобы начать перенос.")
        await message.answer("\n".join(lines))
        return

    lines.append(f"<b>Каналы ({len(channels)}):</b>")
    await message.answer("\n".join(lines))

    for ch in channels:
        autopost = "✅" if ch.autopost_enabled else "❌"
        crosspost = "✅" if ch.crosspost_enabled else "❌"
        comments = "✅" if ch.comments_enabled else "❌"

        ch_text = f"📢 <b>@{ch.tg_channel_username}</b>\n"
        ch_text += f"💬 Комменты: {comments} | 📤 Автопост: {autopost} | 📥 Кроспост: {crosspost}"

        await message.answer(ch_text, reply_markup=_channel_settings_kb(ch))

    common_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Отложенные посты", callback_data="show_scheduled")],
    ])
    await message.answer("⚙️ <b>Общее:</b>", reply_markup=common_kb)


async def _toggle_field(callback: CallbackQuery, field: str, label: str) -> None:
    """Переключение boolean-поля канала."""
    channel_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        channel = await session.get(Channel, channel_id)
        if not channel:
            await callback.answer("Канал не найден")
            return
        current = getattr(channel, field)
        setattr(channel, field, not current)
        await session.commit()
        status = "включён" if not current else "выключен"
        await callback.answer(f"{label} {status}")
        await callback.message.edit_reply_markup(reply_markup=_channel_settings_kb(channel))


@router.callback_query(F.data.startswith("toggle_autopost_"))
async def toggle_autopost(callback: CallbackQuery) -> None:
    await _toggle_field(callback, "autopost_enabled", "Автопостинг TG→MAX")


@router.callback_query(F.data.startswith("toggle_crosspost_"))
async def toggle_crosspost(callback: CallbackQuery) -> None:
    await _toggle_field(callback, "crosspost_enabled", "Кроспостинг MAX→TG")


@router.callback_query(F.data.startswith("toggle_comments_"))
async def toggle_comments(callback: CallbackQuery) -> None:
    await _toggle_field(callback, "comments_enabled", "Комментарии")


@router.callback_query(F.data == "back_to_my")
async def handle_back_to_my(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.delete()
    await cmd_my(callback.message, state)


@router.callback_query(F.data == "add_channel")
async def handle_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(TransferStates.waiting_tg_channel)
    await callback.message.answer(
        "➕ <b>Добавить новый канал</b>\n\n"
        "Отправь ссылку или @username TG-канала, который хочешь подключить.\n"
        "Например: <code>@mychannel</code> или <code>https://t.me/mychannel</code>"
    )


@router.callback_query(F.data == "open_my_hint")
async def handle_open_my_hint(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Отправь /my чтобы открыть кабинет.")


@router.callback_query(F.data == "show_scheduled")
async def handle_show_scheduled(callback: CallbackQuery) -> None:
    await callback.answer()
    from datetime import timezone, timedelta
    from db.models import ScheduledPost
    async with async_session() as session:
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала нажми /start.")
            return
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
        await callback.message.answer(
            "У тебя нет запланированных постов.\nСоздать новый — /schedule"
        )
        return

    user_tz = timezone(timedelta(hours=tz_offset))
    target_labels = {"tg": "TG", "max": "MAX", "both": "TG+MAX"}
    await callback.message.answer(f"📋 <b>Запланированные посты ({len(posts)}):</b>")
    for post in posts:
        local_dt = post.publish_at.replace(tzinfo=timezone.utc).astimezone(user_tz)
        when = local_dt.strftime("%d.%m.%Y %H:%M")
        preview = (post.content_text or "(без текста)")[:100]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🕐 Перенести", callback_data=f"sched_resch_{post.id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sched_del_{post.id}"),
            ],
        ])
        await callback.message.answer(
            f"📅 <b>{when}</b> ({target_labels.get(post.target, post.target)})\n{preview}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("set_max_link_"))
async def handle_set_max_link(callback: CallbackQuery, state: FSMContext) -> None:
    channel_id = int(callback.data.replace("set_max_link_", ""))
    async with async_session() as session:
        channel = await session.get(Channel, channel_id)
        if not channel:
            await callback.answer("Канал не найден", show_alert=True)
            return
        current = channel.max_channel_url or "—"
    await state.update_data(max_link_channel_id=channel_id)
    await state.set_state(TransferStates.waiting_max_link)
    await callback.answer()
    await callback.message.answer(
        "🔗 <b>Публичная ссылка MAX-канала</b>\n\n"
        f"Текущая: <code>{current}</code>\n\n"
        "Пришли публичную ссылку на твой MAX-канал в формате:\n"
        "<code>https://max.ru/yourchannel</code>\n\n"
        "Эта ссылка будет подставляться в постах вместо t.me/yourchannel "
        "при автозамене ссылок.\n\n"
        "Или /cancel для отмены."
    )


@router.message(TransferStates.waiting_max_link)
async def handle_max_link_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not (text.startswith("http://") or text.startswith("https://")):
        await message.answer("Нужна полная ссылка с https://. Попробуй ещё раз или /cancel.")
        return
    data = await state.get_data()
    channel_id = data.get("max_link_channel_id")
    if not channel_id:
        await state.clear()
        return
    async with async_session() as session:
        channel = await session.get(Channel, channel_id)
        if not channel:
            await state.clear()
            await message.answer("Канал не найден.")
            return
        channel.max_channel_url = text
        await session.commit()
        username = channel.tg_channel_username
    await state.clear()
    await message.answer(
        f"✅ Ссылка сохранена: {text}\n\n"
        f"Теперь t.me/{username} будет заменяться на эту ссылку при автозамене."
    )
