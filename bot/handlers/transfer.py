import re

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.inline import (
    cancel_keyboard,
    verify_keyboard,
)
from bot.states.transfer import TransferStates
from core.max_publisher import MaxPublisher
from core.verification import generate_code, verify_channel_code
from db.database import async_session
from db.repositories.channel_repo import ChannelRepo
from db.repositories.transfer_repo import TransferRepo
from db.repositories.user_repo import UserRepo

log = structlog.get_logger()
router = Router()


async def _has_pending_transfer(channel_id: int) -> bool:
    """Есть ли уже запущенный (pending) перенос для канала."""
    from db.models import TransferJob
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(
            select(TransferJob.id).where(
                TransferJob.channel_id == channel_id,
                TransferJob.status == "pending",
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

# Регулярка для парсинга TG-канала
TG_CHANNEL_RE = re.compile(
    r"(?:https?://t\.me/|@)([a-zA-Z_][a-zA-Z0-9_]{3,})"
)


# --- Шаг 1: получить ссылку на TG-канал ---

@router.message(TransferStates.waiting_tg_channel)
async def handle_tg_channel(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    match = TG_CHANNEL_RE.search(text)
    if not match:
        await message.answer(
            "Не удалось распознать канал. Отправь ссылку вида @mychannel "
            "или https://t.me/mychannel"
        )
        return

    tg_username = match.group(1)
    # TG username не может содержать точку — заменяем на подчёркивание
    tg_username = tg_username.replace(".", "_")
    code = generate_code()

    async with async_session() as session:
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(message.from_user.id)
        channel_repo = ChannelRepo(session)
        channel = await channel_repo.get_by_user_and_tg(user.id, tg_username)

        if channel is None:
            channel = await channel_repo.create(
                user_id=user.id,
                tg_channel_username=tg_username,
                verification_code=code,
            )
        else:
            channel.verification_code = code
            await session.commit()

    await state.update_data(
        tg_username=tg_username,
        channel_id=channel.id,
        verification_code=code,
    )
    await message.answer(
        f"Канал: <b>@{tg_username}</b>\n\n"
        f"Добавь этот код в описание своего TG-канала:\n"
        f"<code>{code}</code>\n\n"
        f"Когда добавишь — нажми кнопку «Проверить».",
        reply_markup=verify_keyboard(),
    )
    await state.set_state(TransferStates.waiting_verification)


# --- Шаг 2: проверка кода в описании ---

@router.callback_query(TransferStates.waiting_verification, F.data == "verify_channel")
async def handle_verify(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Проверяю...")
    data = await state.get_data()
    tg_username = data["tg_username"]
    code = data["verification_code"]

    verified, tg_chat_id = await verify_channel_code(tg_username, code)
    if not verified:
        await callback.message.edit_text(
            f"Код <code>{code}</code> не найден в описании канала @{tg_username}.\n"
            "Убедись, что код добавлен, и попробуй ещё раз.",
            reply_markup=verify_keyboard(),
        )
        return

    async with async_session() as session:
        channel_repo = ChannelRepo(session)
        await channel_repo.verify(data["channel_id"], tg_chat_id=tg_chat_id)

    from core.text_format import t
    await callback.message.edit_text(t(
        "✅ TG-канал подтверждён!\n\n"
        "Теперь нужно подключить твой MAX-канал.\n\n"
        "<b>Что нужно сделать:</b>\n"
        "1. Открой свой канал в MAX → Управление → Подписчики → Добавить\n"
        "2. В поиске введи <code>{MAX_BOT_USERNAME}</code> и добавь бота в подписчики, "
        "затем сделай его администратором (оставь галочки «Публиковать сообщения», «Редактировать чужие сообщения» и «Удалять чужие сообщения»)\n"
        "3. Пришли сюда <b>публичную ссылку на твой MAX-канал</b>\n\n"
        "Например: <code>https://max.ru/myblog</code>"
    ))
    await state.set_state(TransferStates.waiting_max_channel)


@router.message(TransferStates.waiting_max_channel)
async def handle_max_channel_name_input(message: Message, state: FSMContext) -> None:
    """Юзер прислал ссылку на MAX-канал — ищем по username из ссылки.
    Если по ссылке не нашли — просим прислать точное название."""
    import re as _re
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли ссылку на MAX-канал.")
        return

    data = await state.get_data()

    # Если уже ждём название (после неудачного поиска по ссылке)
    if data.get("max_link_pending"):
        return await _resolve_max_channel_by_title(message, state, text)

    # Парсим ссылку https://max.ru/xxxxx или max.ru/xxxxx
    m = _re.search(r"(?:https?://)?max\.ru/([a-zA-Z0-9_.\-]+)", text)
    if not m:
        # Это не ссылка — воспринимаем как название (для групп, у которых нет публичной ссылки)
        await state.update_data(max_link_pending="")
        return await _resolve_max_channel_by_title(message, state, text)

    max_url = text if text.startswith("http") else f"https://{text}"
    username = m.group(1)

    # max.ru/join/XXX — invite-ссылка группы, username="join" не имя канала
    if username == "join":
        await state.update_data(max_link_pending=max_url)
        await message.answer(
            f"Это похоже на ссылку-приглашение в группу.\n"
            "Пришли мне <b>точное название группы/канала в MAX</b> — как оно отображается у тебя."
        )
        return

    publisher = MaxPublisher()
    all_channels = await publisher.get_channels()

    # Ищем канал, у которого title совпадает с username из ссылки (case-insensitive)
    username_lower = username.lower()
    matches = [
        c for c in all_channels
        if (c.get("title") or "").lower() == username_lower
        or (c.get("title") or "").lower().replace(" ", "") == username_lower
    ]

    if not matches:
        # Сохраним ссылку и попросим название отдельно
        await state.update_data(max_link_pending=max_url)
        await message.answer(
            f"Ссылка сохранена: {max_url}\n\n"
            "Не смог автоматически сопоставить её с MAX-каналом.\n"
            "Пришли мне <b>точное название канала</b> — как оно отображается в MAX.\n\n"
            "Например: <code>Мой блог</code>"
        )
        return

    selected = matches[0]
    return await _finalize_max_channel(message, state, selected, max_url)


async def _resolve_max_channel_by_title(message: Message, state: FSMContext, title_input: str) -> None:
    data = await state.get_data()
    publisher = MaxPublisher()
    all_channels = await publisher.get_channels()
    name_lower = title_input.lower()
    matches = [c for c in all_channels if (c.get("title") or "").lower() == name_lower]
    if not matches:
        partial = [c for c in all_channels if name_lower in (c.get("title") or "").lower()]
        if not partial:
            await message.answer(
                f"❌ Не нашёл канал <b>{title_input}</b>. Проверь название и попробуй ещё раз.\n"
                "Или /cancel чтобы начать заново."
            )
            return
        if len(partial) > 1:
            titles = "\n".join(f"• {c.get('title')}" for c in partial[:10])
            await message.answer(f"Нашёл несколько похожих:\n{titles}\n\nПришли точное название.")
            return
        matches = partial

    max_url = data.get("max_link_pending", "")
    await _finalize_max_channel(message, state, matches[0], max_url)


async def _finalize_max_channel(message: Message, state: FSMContext, selected: dict, max_url: str) -> None:
    max_channel_id = str(selected["chat_id"])
    btn_text = selected.get("title", "MAX-канал")

    data = await state.get_data()

    # Запрос админов канала через MAX API → запоминаем владельца
    owner_max_uid = None
    try:
        import aiohttp
        from bot.config import settings as _S
        _hdr = {"Authorization": _S.max_bot_token}
        async with aiohttp.ClientSession() as _hs:
            async with _hs.get(f"{_S.max_api_base_url}/chats/{max_channel_id}/members/admins", headers=_hdr) as _r:
                if _r.status == 200:
                    _d = await _r.json()
                    _admins = _d.get("members") or _d.get("admins") or []
                    # Фильтруем ботов: ищем первого НЕ-бота среди админов
                    for _a in _admins:
                        if _a.get("is_bot") or _a.get("bot"):
                            continue
                        _uid = int(_a.get("user_id") or _a.get("id") or 0)
                        if _uid:
                            owner_max_uid = _uid
                            break
    except Exception as _e:
        log.warning("Не удалось получить админов канала MAX", err=str(_e))

    async with async_session() as session:
        channel_repo = ChannelRepo(session)
        await channel_repo.set_max_channel(data["channel_id"], max_url or "", max_channel_id)

        # Сохраняем владельца канала
        if owner_max_uid:
            from db.models import Channel as _ChU
            _ch_obj = await session.get(_ChU, data["channel_id"])
            if _ch_obj:
                _ch_obj.owner_max_user_id = owner_max_uid
                await session.commit()

        # Автосвязка TG↔MAX через общий канал
        from db.models import Channel as _Ch, User as _User
        from sqlalchemy import select as _sel
        me_res = await session.execute(
            _sel(_User).where(_User.tg_user_id == message.from_user.id)
        )
        me = me_res.scalar_one_or_none()
        if me and not me.max_user_id:
            other_res = await session.execute(
                _sel(_User)
                .join(_Ch, _Ch.user_id == _User.id)
                .where(
                    _Ch.max_channel_id == max_channel_id,
                    _User.max_user_id.is_not(None),
                    _User.id != me.id,
                )
                .limit(1)
            )
            other = other_res.scalar_one_or_none()
            if other:
                # Сливаем: переносим все данные other в me, other удаляем
                # (они — один человек: тот же канал принадлежит обоим аккаунтам)
                from db.models import Subscription as _Sub, ScheduledPost as _Sched, TransferJob as _TJ
                saved_max_user_id = other.max_user_id
                saved_tariff = other.tariff
                saved_tz = other.timezone_offset

                # Перевешиваем каналы other → me
                await session.execute(
                    _sel(_Ch).where(_Ch.user_id == other.id)
                )  # noqa — warm up
                from sqlalchemy import update as _upd
                await session.execute(_upd(_Ch).where(_Ch.user_id == other.id).values(user_id=me.id))
                await session.execute(_upd(_Sub).where(_Sub.user_id == other.id).values(user_id=me.id))
                await session.execute(_upd(_Sched).where(_Sched.user_id == other.id).values(user_id=me.id))

                # Удаляем other (теперь у него нет данных)
                await session.delete(other)
                await session.flush()

                # Переносим max_user_id и тариф на me
                me.max_user_id = saved_max_user_id
                if saved_tariff and saved_tariff != "free":
                    me.tariff = saved_tariff
                if saved_tz is not None and me.timezone_offset is None:
                    me.timezone_offset = saved_tz

                await session.commit()
                log.info(
                    "Слияние TG+MAX аккаунтов через общий канал",
                    tg_user_id=me.tg_user_id,
                    max_user_id=saved_max_user_id,
                    max_channel_id=max_channel_id,
                )

    await state.update_data(
        max_channel_id=max_channel_id,
        max_channel_title=btn_text,
    )

    data = await state.get_data()
    if await _has_pending_transfer(data["channel_id"]):
        await message.answer("⏳ Перенос этого канала уже идёт. Дождись завершения.")
        await state.clear()
        return

    await message.answer(
        f"✅ Канал MAX: <b>{btn_text}</b>\n\n"
        "Начинаю перенос постов..."
    )
    await state.set_state(TransferStates.transfer_in_progress)
    from tasks.transfer_tasks import run_transfer
    run_transfer.delay(
        channel_id=data["channel_id"],
        tg_username=data["tg_username"],
        max_channel_id=max_channel_id,
        post_limit=1000,
        chat_id=message.chat.id,
    )


# --- Отмена ---

@router.callback_query(F.data == "cancel")
async def handle_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_text("Действие отменено. Нажми /start чтобы начать заново.")


# --- Продолжить перенос после оплаты тарифа ---

@router.callback_query(F.data.startswith("continue_transfer:"))
async def handle_continue_transfer(callback: CallbackQuery, state: FSMContext) -> None:
    """Запускает полный перенос всех постов канала после оплаты тарифа.

    Канал уже верифицирован, заново проходить FSM не нужно.
    Дедупликация в transfer_service пропустит уже перенесённые посты.
    """
    await callback.answer()
    try:
        channel_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("Не удалось определить канал. Открой /my")
        return

    async with async_session() as session:
        ch_repo = ChannelRepo(session)
        channel = await ch_repo.get_by_id(channel_id)
        if not channel:
            await callback.message.answer("Канал не найден. Открой /my")
            return
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(callback.from_user.id)
        if not user or channel.user_id != user.id:
            await callback.message.answer("Этот канал принадлежит другому аккаунту.")
            return

    if await _has_pending_transfer(channel.id):
        await callback.message.answer(
            "⏳ Перенос этого канала уже идёт. Дождись завершения — я пришлю результат в этом чате."
        )
        return

    await state.clear()
    await callback.message.edit_text(
        f"▶️ Запускаю полный перенос всех постов канала <b>@{channel.tg_channel_username}</b>.\n"
        "Уже перенесённые посты пропускаются автоматически.\n"
        "Это может занять несколько минут — пришлю результат.",
    )

    from tasks.transfer_tasks import run_transfer
    run_transfer.delay(
        channel_id=channel.id,
        tg_username=channel.tg_channel_username,
        max_channel_id=channel.max_channel_id,
        post_limit=1000,
        chat_id=callback.message.chat.id,
    )



@router.callback_query(F.data.startswith("retransfer:"))
async def handle_retransfer(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаляет уже перенесённые посты из MAX и запускает перенос с нуля."""
    await callback.answer("Запускаю...")
    try:
        channel_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("Не удалось определить канал. Открой /my")
        return

    async with async_session() as session:
        ch_repo = ChannelRepo(session)
        channel = await ch_repo.get_by_id(channel_id)
        if not channel:
            await callback.message.answer("Канал не найден. Открой /my")
            return
        user_repo = UserRepo(session)
        user = await user_repo.get_by_tg_id(callback.from_user.id)
        if not user or channel.user_id != user.id:
            await callback.message.answer("Этот канал принадлежит другому аккаунту.")
            return

    if await _has_pending_transfer(channel.id):
        await callback.message.answer(
            "⏳ Перенос этого канала уже идёт. Дождись завершения и тогда можно будет перенести заново."
        )
        return

    await state.clear()
    await callback.message.edit_text(
        f"🧹 Удаляю старые посты в MAX и запускаю перенос заново для <b>@{channel.tg_channel_username}</b>...\n"
        "Это может занять несколько минут — пришлю результат."
    )

    # Удаляем уже перенесённые посты в MAX
    import aiohttp
    from bot.config import settings
    from db.models import PostMapping
    from sqlalchemy import select, delete
    deleted = 0
    failed = 0
    headers = {"Authorization": settings.max_bot_token}
    async with aiohttp.ClientSession() as http:
        async with async_session() as session:
            result = await session.execute(
                select(PostMapping.max_message_id).where(PostMapping.channel_id == channel.id)
            )
            mids = [r[0] for r in result.fetchall() if r[0]]

        # Если post_mapping пуст — собираем mid из MAX API (все сообщения бота в канале)
        if not mids:
            try:
                async with http.get(
                    f"https://platform-api.max.ru/messages",
                    headers=headers,
                    params={"chat_id": channel.max_channel_id, "count": 100},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bot_id = 255015229
                        for msg in data.get("messages", []):
                            sender = msg.get("sender") or {}
                            mid = msg.get("body", {}).get("mid")
                            # Берём только посты от бота (или без sender — каналы)
                            if mid and (not sender or sender.get("user_id") == bot_id):
                                mids.append(mid)
            except Exception:
                pass

        fail_reason = None
        for mid in mids:
            try:
                async with http.delete(
                    f"https://platform-api.max.ru/messages?message_id={mid}",
                    headers=headers,
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("success") is not False:
                        deleted += 1
                    else:
                        failed += 1
                        if not fail_reason:
                            fail_reason = data.get("message", f"HTTP {resp.status}")
            except Exception as e:
                failed += 1
                if not fail_reason:
                    fail_reason = str(e)

        # Чистим post_mapping
        async with async_session() as session:
            await session.execute(delete(PostMapping).where(PostMapping.channel_id == channel.id))
            await session.commit()

    log.info("Retransfer: удалены старые посты", channel_id=channel.id, deleted=deleted, failed=failed)

    # Если ничего не удалили из-за прав — предупредить юзера
    if deleted == 0 and failed > 0 and fail_reason and "not.admin" in fail_reason:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Перенести заново (после выдачи прав)",
                callback_data=f"retransfer:{channel.id}",
            )],
            [InlineKeyboardButton(
                text="⏭ Только новые посты",
                callback_data=f"continue_transfer:{channel.id}",
            )],
        ])
        from core.text_format import t
        await callback.message.answer(t(
            "⚠️ Не удалось удалить старые посты — у бота нет прав на удаление в MAX-канале.\n\n"
            "<b>Что сделать:</b>\n"
            "1. Открой MAX-канал → Управление → Админы → бот {MAX_BOT_USERNAME}\n"
            "2. Включи галочки: «Публиковать сообщения», «Редактировать чужие сообщения», «Удалять чужие сообщения»\n\n"
            "После этого нажми кнопку ниже:"),
            reply_markup=kb,
        )
        return

    # Запускаем перенос всех постов
    from tasks.transfer_tasks import run_transfer
    run_transfer.delay(
        channel_id=channel.id,
        tg_username=channel.tg_channel_username,
        max_channel_id=channel.max_channel_id,
        post_limit=1000,
        chat_id=callback.message.chat.id,
    )
