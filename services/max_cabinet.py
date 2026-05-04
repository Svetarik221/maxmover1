"""Кабинет MAX-бота: управление каналами, настройками, отложенными постами."""
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from bot.config import settings
from db.database import async_session
from db.models import Channel, ScheduledPost, User
from sqlalchemy import select
from core.tariff import get_channel_tariff, get_user_tariff, can_use, can_add_channel, UPGRADE_HINTS, TARIFF_NAMES

log = structlog.get_logger()

BASE_URL = settings.max_api_base_url
HEADERS = {"Authorization": settings.max_bot_token}


async def send_dm(user_id: int, text: str, keyboard: list | None = None) -> dict | None:
    """Отправляет ЛС пользователю в MAX с опциональной клавиатурой."""
    body: dict = {"text": text, "format": "html"}
    if keyboard:
        body["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {"buttons": keyboard},
        }]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{BASE_URL}/messages",
                headers=HEADERS,
                params={"user_id": user_id},
                json=body,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning("Ошибка отправки ЛС MAX", status=resp.status)
                return None
    except Exception:
        log.exception("Ошибка отправки ЛС MAX")
        return None


async def answer_callback(callback_id: str, text: str | None = None) -> None:
    """Отвечает на callback."""
    body: dict = {"callback_id": callback_id}
    if text:
        body["notification"] = text
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{BASE_URL}/answers", headers=HEADERS, json=body)
    except Exception:
        pass


async def _get_or_create_user(max_user_id: int, user_name: str) -> User:
    """Находит пользователя по max_user_id или создаёт нового.

    Автосвязка: перед созданием виртуального MAX-юзера проверяем,
    нет ли TG-юзера которому принадлежит канал с owner_max_user_id == max_user_id
    и у которого ещё не привязан max_user_id. Если есть — связываем,
    возвращаем его (без создания дубля).
    """
    async with async_session() as session:
        # 1. По max_user_id
        result = await session.execute(
            select(User).where(User.max_user_id == max_user_id)
        )
        user = result.scalar_one_or_none()
        if user:
            return user

        # 2. Автосвязка через Channel.owner_max_user_id
        ch_res = await session.execute(
            select(Channel)
            .where(Channel.owner_max_user_id == max_user_id)
            .limit(1)
        )
        ch = ch_res.scalar_one_or_none()
        if ch:
            owner_res = await session.execute(
                select(User).where(User.id == ch.user_id)
            )
            owner = owner_res.scalar_one_or_none()
            if owner and not owner.max_user_id:
                owner.max_user_id = max_user_id
                if not owner.first_name:
                    owner.first_name = user_name
                await session.commit()
                await session.refresh(owner)
                log.info("Автосвязка MAX→TG через owner_max_user_id",
                         max_user_id=max_user_id, tg_user_id=owner.tg_user_id)
                return owner

        # 3. Новый MAX-only юзер
        user = User(
            tg_user_id=-max_user_id,
            max_user_id=max_user_id,
            username=None,
            first_name=user_name,
            tariff="free",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def handle_start(user_id: int, user_name: str) -> None:
    """Пользователь нажал Старт / написал боту."""
    await _get_or_create_user(user_id, user_name)

    await send_dm(
        user_id,
        f"Привет, {user_name}! 👋\n\n"
        "Я помогу подключить комментарии к постам в MAX-каналах "
        "и настроить автопостинг.\n\n"
        "Что хочешь сделать?",
        keyboard=[
            [
                {"type": "callback", "text": "📢 Мои каналы", "payload": "my_channels"},
                {"type": "callback", "text": "🕐 Отложенный пост", "payload": "schedule_start"},
            ],
            [
                {"type": "callback", "text": "📋 Мои отложенные", "payload": "scheduled_list"},
            ],
            [
                {"type": "callback", "text": "❓ Как подключить", "payload": "show_help"},
                {"type": "callback", "text": "📄 Политика", "payload": "show_privacy"},
            ],
            [
                {"type": "link", "text": "💬 Поддержка", "url": ""},
            ],
        ],
    )


async def handle_my_channels(user_id: int) -> None:
    """Показывает каналы пользователя с настройками."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.max_user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await send_dm(user_id, "Напиши мне что-нибудь чтобы начать.")
            return

        result = await session.execute(
            select(Channel).where(Channel.user_id == user.id)
        )
        channels = result.scalars().all()

    if not channels:
        from core.text_format import t
        await send_dm(
            user_id,
            t("У тебя пока нет подключённых каналов.\n\n"
              "Добавь бот {MAX_BOT_USERNAME} в свой MAX-канал (как — см. «Как подключить»), "
              "затем нажми кнопку ниже."),
            keyboard=[[
                {"type": "callback", "text": "🔍 Найти мои каналы", "payload": "find_channels"},
            ]],
        )
        return

    for ch in channels:
        autopost = "✅" if ch.autopost_enabled else "❌"
        crosspost = "✅" if ch.crosspost_enabled else "❌"
        comments = "✅" if ch.comments_enabled else "❌"
        link_btn = "🔗 Ссылка: ✅" if ch.max_channel_url else "🔗 Задать публичную ссылку"

        text = f"📢 <b>{ch.tg_channel_username or 'MAX-канал'}</b>"

        await send_dm(user_id, text, keyboard=[
            [{"type": "callback", "text": f"{comments} Комментарии", "payload": f"mtoggle_comments_{ch.id}"}],
            [{"type": "callback", "text": f"{autopost} Автопост TG→MAX", "payload": f"mtoggle_autopost_{ch.id}"}],
            [{"type": "callback", "text": f"{crosspost} Кроспост MAX→TG", "payload": f"mtoggle_crosspost_{ch.id}"}],
            [{"type": "callback", "text": link_btn, "payload": f"mset_link_{ch.id}"}],
        ])

    # Кнопка добавить ещё канал
    await send_dm(
        user_id,
        "Хочешь подключить ещё один канал?",
        keyboard=[[{"type": "callback", "text": "➕ Добавить канал", "payload": "find_channels"}]],
    )


async def handle_find_channels(user_id: int) -> None:
    """Просим юзера прислать название его MAX-канала (вместо показа общего списка
    всех каналов где бот когда-либо был добавлен — это утечка чужих каналов)."""
    from core.text_format import t
    _schedule_state[user_id] = {"step": "find_channel_name"}
    await send_dm(
        user_id,
        t("🔍 <b>Подключение MAX-канала</b>\n\n"
          "Как добавить бота в MAX-канал:\n1. Открой свой канал в MAX → Управление → Подписчики → Добавить\n2. В поиске введи: <code>{MAX_BOT_USERNAME}</code> (не ссылку!) и добавь в подписчики\n3. Затем сделай админом: Управление → Админы → Добавить → найди {MAX_BOT_USERNAME}\n4. <b>Важно:</b> при назначении админом оставь галочки «Публиковать сообщения», «Редактировать чужие сообщения» и «Удалять чужие сообщения»\n\n"
          "Как добавить бота в TG-канал/группу (нужно для автопостинга TG→MAX и кроспостинга MAX→TG):\n1. Открой TG-канал → Управление → Администраторы → Добавить\n2. Найди и добавь этого бота\n3. <b>Важно:</b> оставь галочку «Публикация сообщений»\n\n"
          "5. Пришли мне сюда <b>точное название канала</b> — как оно отображается в MAX\n\n"
          "Например: <code>Мой блог</code>"),
    )


async def handle_find_channel_input(user_id: int, name: str) -> None:
    """Юзер прислал название MAX-канала — ищем среди каналов где бот админ."""
    name = (name or "").strip()
    if not name:
        await send_dm(user_id, "Пришли название канала текстом.")
        return

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/chats", headers=HEADERS) as resp:
                if resp.status != 200:
                    await send_dm(user_id, "Ошибка получения каналов. Попробуй позже.")
                    return
                data = await resp.json()
                chats = data.get("chats", [])
    except Exception:
        await send_dm(user_id, "Ошибка сети. Попробуй позже.")
        return

    channels = [c for c in chats if c.get("type") in ("channel", "chat")]
    name_lower = name.lower()
    matches = [c for c in channels if (c.get("title") or "").lower() == name_lower]

    if not matches:
        partial = [c for c in channels if name_lower in (c.get("title") or "").lower()]
        if not partial:
            from core.text_format import t
            await send_dm(
                user_id,
                f"❌ Не нашёл MAX-канал с названием <b>{name}</b> среди тех, где я админ.\n\n"
                + t("Проверь:\n"
                    "• Бот <b>{MAX_BOT_USERNAME}</b> добавлен в канал админом?\n"
                    "• Название написано точно как в MAX?\n\n"
                    "Попробуй ещё раз или нажми «Мои каналы»."),
            )
            return
        if len(partial) > 1:
            titles = "\n".join(f"• {c.get('title')}" for c in partial[:10])
            await send_dm(user_id, f"Нашёл несколько похожих:\n{titles}\n\nПришли точное название.")
            return
        matches = partial

    selected = matches[0]
    chat_id = str(selected["chat_id"])
    if user_id in _schedule_state:
        del _schedule_state[user_id]
    await handle_connect_channel(user_id, chat_id)


# Старый список (НЕ используется — оставлен на случай если где-то ещё вызовется)
async def _legacy_handle_find_channels(user_id: int) -> None:
    buttons = []

    await send_dm(user_id, "Выбери канал для подключения:", keyboard=buttons)


async def handle_connect_channel(user_id: int, max_chat_id: str) -> None:
    """Подключает MAX-канал к пользователю."""
    async with async_session() as session:
        # Находим пользователя
        result = await session.execute(
            select(User).where(User.max_user_id == user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await send_dm(user_id, "Нажми /start чтобы начать.")
            return

        # Проверяем лимит каналов
        allowed, reason = await can_add_channel(user.id)
        if not allowed:
            await send_dm(user_id, reason)
            return

        # Проверяем что канал не подключён
        result = await session.execute(
            select(Channel).where(Channel.max_channel_id == max_chat_id).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            if existing.user_id == user.id:
                # Канал уже у этого пользователя — показываем настройки
                await handle_my_channels(user_id)
                return
            await send_dm(user_id, "Этот канал уже подключён другим пользователем.")
            return

        # Получаем инфо о канале
        title = "MAX-канал"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{BASE_URL}/chats/{max_chat_id}", headers=HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", title)
        except Exception:
            pass

        channel = Channel(
            user_id=user.id,
            tg_channel_username=title,
            max_channel_id=max_chat_id,
            is_verified=True,
            autopost_enabled=False,
            crosspost_enabled=False,
            comments_enabled=False,
        )
        session.add(channel)
        await session.commit()

        # Автосвязка MAX→TG: если этот же max_chat_id уже подключён через
        # TG-бота другим юзером — копируем в его профиль наш max_user_id.
        if user.tg_user_id < 0:  # это чистый MAX-юзер
            other_res = await session.execute(
                select(User)
                .join(Channel, Channel.user_id == User.id)
                .where(
                    Channel.max_channel_id == max_chat_id,
                    User.tg_user_id > 0,
                )
                .limit(1)
            )
            other = other_res.scalar_one_or_none()
            if other and not other.max_user_id:
                other.max_user_id = user.max_user_id
                await session.commit()
                log.info(
                    "Автосвязка MAX→TG через общий канал",
                    max_user_id=user.max_user_id,
                    tg_user_id=other.tg_user_id,
                    max_chat_id=max_chat_id,
                )

    await send_dm(
        user_id,
        f"✅ Канал <b>{title}</b> подключён!\n\n"
        "💬 Чтобы включить комментарии под постами и другие фичи (автопостинг, отложенные посты, замена ссылок) — оформи подписку в кабинете.",
        keyboard=[[
            {"type": "callback", "text": "📢 Мои каналы", "payload": "my_channels"},
        ]],
    )


async def handle_toggle(user_id: int, field: str, channel_id: int) -> None:
    # Проверяем что юзер владеет каналом
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        from db.models import Channel as _Ch
        _ch = await session.get(_Ch, channel_id)
        if not _ch or _ch.user_id != user.id:
            await send_dm(user_id, "Ошибка: канал не найден или принадлежит другому пользователю.")
            return

    """Переключает настройку канала с проверкой тарифа."""
    field_to_feature = {
        "comments_enabled": "comments",
        "autopost_enabled": "autopost",
        "crosspost_enabled": "crosspost",
    }
    feature = field_to_feature.get(field)

    if feature:
        tariff = await get_channel_tariff(channel_id)
        if not can_use(tariff, feature):
            await send_dm(user_id, UPGRADE_HINTS.get(feature, "Эта функция доступна на платном тарифе."))
            return

    async with async_session() as session:
        channel = await session.get(Channel, channel_id)
        if not channel:
            await send_dm(user_id, "Канал не найден.")
            return

        current = getattr(channel, field)
        setattr(channel, field, not current)
        await session.commit()

        labels = {
            "comments_enabled": "Комментарии",
            "autopost_enabled": "Автопостинг TG→MAX",
            "crosspost_enabled": "Кроспостинг MAX→TG",
        }
        status = "включены" if not current else "выключены"
        label = labels.get(field, field)

    await send_dm(user_id, f"{label}: {status}")
    await handle_my_channels(user_id)


# ─── Отложенные посты в MAX ───

# FSM: {max_user_id: {"step": "...", "channel_id": ..., "text": ..., "target": ...}}
_schedule_state: dict[int, dict] = {}


async def handle_schedule_start(user_id: int) -> None:
    """Начинает флоу отложенного поста."""
    user = await _get_or_create_user(user_id, "")

    # Проверяем тариф — отложка только на Про
    tariff = await get_user_tariff(user.id)
    if not can_use(tariff, "schedule"):
        await send_dm(user_id, UPGRADE_HINTS["schedule"])
        return

    async with async_session() as session:
        result = await session.execute(
            select(Channel).where(Channel.user_id == user.id)
        )
        channels = result.scalars().all()

    if not channels:
        await send_dm(user_id, "Сначала подключи канал через «Мои каналы».")
        return

    if len(channels) == 1:
        _schedule_state[user_id] = {"step": "text", "channel_id": channels[0].id}
        await send_dm(user_id, "📝 Напиши текст поста, который хочешь запланировать:")
        return

    # Несколько каналов — выбор
    buttons = []
    for ch in channels:
        name = ch.tg_channel_username or f"Канал {ch.id}"
        buttons.append([{
            "type": "callback",
            "text": f"📢 {name}",
            "payload": f"sched_ch_{ch.id}",
        }])
    _schedule_state[user_id] = {"step": "channel"}
    await send_dm(user_id, "Выбери канал:", keyboard=buttons)


async def handle_schedule_select_channel(user_id: int, channel_id: int) -> None:
    """Пользователь выбрал канал для отложки."""
    _schedule_state[user_id] = {"step": "text", "channel_id": channel_id}
    await send_dm(user_id, "📝 Напиши текст поста:")


async def handle_schedule_text(user_id: int, text: str, attachments: list = None) -> None:
    """Получили текст поста — спрашиваем куда публиковать."""
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "text":
        return

    state["text"] = text
    # Сохраняем MAX-аттачменты (медиа) в state — без inline_keyboard
    state["max_attachments"] = [
        a for a in (attachments or [])
        if a.get("type") != "inline_keyboard"
    ]
    state["step"] = "target"

    # Проверяем есть ли TG-канал
    async with async_session() as session:
        channel = await session.get(Channel, state["channel_id"])
        has_tg = channel and channel.tg_channel_id

    if has_tg:
        await send_dm(user_id, "Куда опубликовать?", keyboard=[
            [{"type": "callback", "text": "💬 Только MAX", "payload": "sched_target_max"}],
            [{"type": "callback", "text": "📢 Только TG", "payload": "sched_target_tg"}],
            [{"type": "callback", "text": "🔁 В оба", "payload": "sched_target_both"}],
        ])
    else:
        # Только MAX — сразу к выбору пояса/времени
        await handle_schedule_target(user_id, "max")


TIMEZONES = [
    ("UTC+2 Калининград", 2),
    ("UTC+3 Москва", 3),
    ("UTC+4 Самара", 4),
    ("UTC+5 Екатеринбург", 5),
    ("UTC+6 Омск", 6),
    ("UTC+7 Новосибирск", 7),
    ("UTC+8 Иркутск", 8),
    ("UTC+9 Якутск", 9),
    ("UTC+10 Владивосток", 10),
    ("UTC+11 Магадан", 11),
    ("UTC+12 Камчатка", 12),
]


async def handle_schedule_target(user_id: int, target: str) -> None:
    """Выбрали куда — спрашиваем часовой пояс."""
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "target":
        return

    state["target"] = target
    state["step"] = "timezone"

    # Проверяем сохранённый пояс
    user = await _get_or_create_user(user_id, "")
    if user.timezone_offset is not None:
        # Уже знаем пояс — пропускаем
        state["tz_offset"] = user.timezone_offset
        state["step"] = "datetime"
        tz_label = next((l for l, o in TIMEZONES if o == user.timezone_offset), f"UTC+{user.timezone_offset}")
        await send_dm(user_id, f"🕐 Когда опубликовать? (время {tz_label})\nФормат: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\nНапример: 05.04.2026 14:30")
        return

    buttons = []
    row = []
    for label, offset in TIMEZONES:
        row.append({"type": "callback", "text": label, "payload": f"sched_tz_{offset}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await send_dm(user_id, "🌍 Выбери свой часовой пояс:", keyboard=buttons)


async def handle_schedule_timezone(user_id: int, tz_offset: int) -> None:
    """Выбрали часовой пояс — сохраняем и спрашиваем время."""
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "timezone":
        return

    state["tz_offset"] = tz_offset
    state["step"] = "datetime"

    # Сохраняем пояс в профиль
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        u = result.scalar_one()
        u.timezone_offset = tz_offset
        await session.commit()

    tz_label = next((l for l, o in TIMEZONES if o == tz_offset), f"UTC+{tz_offset}")
    await send_dm(user_id, f"🕐 Когда опубликовать? (время {tz_label})\nФормат: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\nНапример: 05.04.2026 14:30")


async def handle_schedule_datetime(user_id: int, text: str) -> None:
    """Получили дату — создаём отложенный пост."""
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "datetime":
        return

    # Парсим дату с учётом выбранного пояса
    tz_offset = state.get("tz_offset", 3)
    try:
        dt = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M")
        user_tz = timezone(timedelta(hours=tz_offset))
        dt_local = dt.replace(tzinfo=user_tz)
        publish_at = dt_local.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        await send_dm(user_id, "❌ Неверный формат. Напиши так: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\nНапример: 05.04.2026 14:30")
        return

    if publish_at < datetime.utcnow():
        await send_dm(user_id, "❌ Дата уже прошла. Укажи будущую дату:")
        return

    # Создаём пост
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        # Сохраняем MAX-аттачменты (медиа) для публикации
        max_atts = state.get("max_attachments") or []
        media_data = {"max_attachments": max_atts, "from_max": True} if max_atts else {"from_max": True}
        post = ScheduledPost(
            user_id=user.id,
            channel_id=state["channel_id"],
            content_text=state["text"],
            media_file_ids=media_data,
            target=state["target"],
            publish_at=publish_at,
            status="pending",
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        post_id = post.id

    # Планируем в celery
    from tasks.scheduled_tasks import publish_scheduled_post
    publish_scheduled_post.apply_async(args=[post_id], eta=publish_at)

    target_labels = {"tg": "TG", "max": "MAX", "both": "TG и MAX"}
    local_time = text.strip()

    del _schedule_state[user_id]

    await send_dm(
        user_id,
        f"✅ <b>Пост запланирован!</b>\n\n"
        f"📅 Дата: <b>{local_time} (МСК)</b>\n"
        f"📢 Куда: <b>{target_labels.get(state['target'], state['target'])}</b>\n\n"
        "В указанное время пост будет опубликован автоматически.",
        keyboard=[[
            {"type": "callback", "text": "📢 Мои каналы", "payload": "my_channels"},
            {"type": "callback", "text": "🕐 Ещё пост", "payload": "schedule_start"},
        ]],
    )


def get_schedule_state(user_id: int) -> dict | None:
    """Возвращает текущее состояние FSM для отложки."""
    return _schedule_state.get(user_id)


# ─── Заглушки для совместимости (в self-hosted open mode оплата не нужна) ───

async def handle_buy_tariff(user_id: int) -> None:
    await send_dm(user_id, "В self-hosted версии все функции уже доступны без оплаты.")


async def handle_buy_payment(user_id: int, tariff: str) -> None:
    await send_dm(user_id, "В self-hosted версии оплата не требуется.")


async def handle_buy_start_for_channel(user_id: int, channel_id: int) -> None:
    await send_dm(user_id, "В self-hosted версии оплата не требуется.")


# ─── Список / удаление / перенос отложек в MAX ───

async def handle_scheduled_list(user_id: int) -> None:
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
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
        await send_dm(
            user_id,
            "У тебя нет запланированных постов.",
            keyboard=[[{"type": "callback", "text": "🕐 Создать", "payload": "schedule_start"}]],
        )
        return

    user_tz = timezone(timedelta(hours=tz_offset))
    target_labels = {"tg": "TG", "max": "MAX", "both": "TG+MAX"}
    header = f"📋 <b>Запланированные посты ({total}):</b>"
    if total > 10:
        header += "\n\n<i>Показаны ближайшие 10. Остальные появятся по мере публикации.</i>"
    await send_dm(user_id, header)
    for post in posts:
        local_dt = post.publish_at.replace(tzinfo=timezone.utc).astimezone(user_tz)
        when = local_dt.strftime("%d.%m.%Y %H:%M")
        preview = (post.content_text or "(без текста)")[:100]
        await send_dm(
            user_id,
            f"📅 <b>{when}</b> ({target_labels.get(post.target, post.target)})\n{preview}",
            keyboard=[
                [
                    {"type": "callback", "text": "✏️ Изменить", "payload": f"msched_edit_{post.id}"},
                    {"type": "callback", "text": "🕐 Перенести", "payload": f"msched_resch_{post.id}"},
                ],
                [
                    {"type": "callback", "text": "🗑 Удалить", "payload": f"msched_del_{post.id}"},
                ],
            ],
        )


async def handle_scheduled_delete(user_id: int, post_id: int) -> None:
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        if not post or post.user_id != user.id or post.status != "pending":
            await send_dm(user_id, "Пост не найден или уже опубликован.")
            return
        post.status = "cancelled"
        await session.commit()
    await send_dm(user_id, "🗑 Отложенный пост отменён.")


async def handle_scheduled_reschedule_start(user_id: int, post_id: int) -> None:
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        if not post or post.user_id != user.id or post.status != "pending":
            await send_dm(user_id, "Пост не найден или уже опубликован.")
            return
    _schedule_state[user_id] = {
        "step": "reschedule_datetime",
        "reschedule_post_id": post_id,
        "tz_offset": user.timezone_offset or 3,
    }
    await send_dm(
        user_id,
        "🕐 Введи новое время публикации:\n"
        "<b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\nНапример: 15.04.2026 14:30",
    )


async def handle_scheduled_reschedule_input(user_id: int, text: str) -> None:
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "reschedule_datetime":
        return
    tz_offset = state.get("tz_offset", 3)
    try:
        dt = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M")
        user_tz = timezone(timedelta(hours=tz_offset))
        publish_at = dt.replace(tzinfo=user_tz).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        await send_dm(user_id, "❌ Неверный формат. Пример: 15.04.2026 14:30")
        return
    if publish_at < datetime.utcnow():
        await send_dm(user_id, "❌ Дата уже прошла.")
        return

    post_id = state["reschedule_post_id"]
    async with async_session() as session:
        old = await session.get(ScheduledPost, post_id)
        if not old or old.status != "pending":
            await send_dm(user_id, "Пост уже не существует.")
            del _schedule_state[user_id]
            return
        old.status = "cancelled"
        new_post = ScheduledPost(
            user_id=old.user_id,
            channel_id=old.channel_id,
            content_text=old.content_text,
            media_file_ids=old.media_file_ids,
            target=old.target,
            publish_at=publish_at,
            status="pending",
        )
        session.add(new_post)
        await session.commit()
        await session.refresh(new_post)
        new_id = new_post.id

    from tasks.scheduled_tasks import publish_scheduled_post
    publish_scheduled_post.apply_async(args=[new_id], eta=publish_at)

    del _schedule_state[user_id]
    user_tz = timezone(timedelta(hours=tz_offset))
    local_time = publish_at.replace(tzinfo=timezone.utc).astimezone(user_tz).strftime("%d.%m.%Y %H:%M")
    await send_dm(user_id, f"✅ Время изменено на <b>{local_time}</b>.")


# ─── Установка публичной MAX-ссылки канала ───

async def handle_set_link_start(user_id: int, channel_id: int) -> None:
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        ch = await session.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            await send_dm(user_id, "Канал не найден.")
            return
        current = ch.max_channel_url or "—"
    _schedule_state[user_id] = {"step": "set_link", "channel_id": channel_id}
    await send_dm(
        user_id,
        f"🔗 <b>Публичная ссылка MAX-канала</b>\n\n"
        f"Текущая: <code>{current}</code>\n\n"
        "Пришли ссылку формата <code>https://max.ru/yourchannel</code>.\n"
        "Она будет подставляться в постах вместо t.me/yourchannel при автозамене.",
    )


async def handle_set_link_input(user_id: int, text: str) -> None:
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "set_link":
        return
    text = text.strip()
    if not (text.startswith("http://") or text.startswith("https://")):
        await send_dm(user_id, "Нужна полная ссылка с https://. Попробуй ещё раз.")
        return
    channel_id = state["channel_id"]
    async with async_session() as session:
        ch = await session.get(Channel, channel_id)
        if not ch:
            del _schedule_state[user_id]
            return
        ch.max_channel_url = text
        await session.commit()
        username = ch.tg_channel_username
    del _schedule_state[user_id]
    await send_dm(
        user_id,
        f"✅ Ссылка сохранена: {text}\n\n"
        f"Теперь t.me/{username} в постах будет заменяться на неё.",
    )


# ─── Политика конфиденциальности в MAX ───

async def handle_help(user_id: int) -> None:
    """Показывает инструкцию для MAX."""
    from core.text_format import t
    await send_dm(
        user_id,
        t("❓ <b>Как подключить канал</b>\n\n"
          "Как добавить бота в MAX-канал:\n1. Открой свой канал в MAX → Управление → Подписчики → Добавить\n2. В поиске введи: <code>{MAX_BOT_USERNAME}</code> (не ссылку!) и добавь в подписчики\n3. Затем сделай админом: Управление → Админы → Добавить → найди {MAX_BOT_USERNAME}\n4. <b>Важно:</b> при назначении админом оставь галочки «Публиковать сообщения», «Редактировать чужие сообщения» и «Удалять чужие сообщения»\n\n"
          "Как добавить бота в TG-канал/группу (нужно для автопостинга TG→MAX и кроспостинга MAX→TG):\n1. Открой TG-канал → Управление → Администраторы → Добавить\n2. Найди и добавь этого бота\n3. <b>Важно:</b> оставь галочку «Публикация сообщений»\n\n"
          "<b>Шаг 5.</b> Вернись в этот чат → «📢 Мои каналы» → «🔍 Найти мои каналы»\n"
          "<b>Шаг 6.</b> Пришли название канала — бот найдёт и подключит\n\n"
          "<b>Что умеет бот:</b>\n"
          "💬 Комментарии к постам (мини-приложение)\n"
          "📤 Автопостинг TG→MAX (новые посты из TG летят в MAX)\n"
          "📥 Кроспостинг MAX→TG (новые посты из MAX летят в TG)\n"
          "🕐 Отложенные посты (MAX, TG или оба)\n\n"
          "\U0001f4a1 <b>Подсказка:</b> чтобы вызвать меню — напиши боту любое слово."),
        keyboard=[[
            {"type": "callback", "text": "📢 Мои каналы", "payload": "my_channels"},
            {"type": "callback", "text": "🏠 Меню", "payload": "main_menu"},
        ]],
    )


async def handle_scheduled_edit_start(user_id: int, post_id: int) -> None:
    """Запускает редактирование отложенного поста — ждём новый текст."""
    user = await _get_or_create_user(user_id, "")
    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        if not post or post.user_id != user.id or post.status != "pending":
            await send_dm(user_id, "Пост не найден или уже опубликован.")
            return
    _schedule_state[user_id] = {"step": "edit_text", "edit_post_id": post_id}
    await send_dm(
        user_id,
        "✏️ <b>Редактирование</b>\n\nПришли новый текст поста.",
    )


async def handle_scheduled_edit_input(user_id: int, text: str) -> None:
    """Сохраняет новый текст отложенного поста."""
    state = _schedule_state.get(user_id)
    if not state or state.get("step") != "edit_text":
        return
    post_id = state.get("edit_post_id")
    if not post_id:
        return

    async with async_session() as session:
        post = await session.get(ScheduledPost, post_id)
        if not post:
            await send_dm(user_id, "Пост не найден.")
            del _schedule_state[user_id]
            return
        post.content_text = text
        await session.commit()

    del _schedule_state[user_id]
    await send_dm(user_id, "✅ Текст обновлён. Список: «📋 Мои отложенные».")


async def handle_privacy(user_id: int) -> None:
    from bot.handlers.privacy import PRIVACY_TEXT
    await send_dm(user_id, PRIVACY_TEXT)
