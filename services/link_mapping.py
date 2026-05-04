"""Построение справочника замены TG-ссылок на публичные MAX-ссылки.

Берётся из таблицы channels: для каждого канала, у которого
указан max_channel_url (полный URL), пара tg_channel_username → max_channel_url.
"""
from sqlalchemy import select

from db.database import async_session
from db.models import Channel


async def build_link_mapping(user_id: int) -> dict[str, str]:
    """Возвращает {tg_username → max_channel_url} для всех каналов юзера,
    у которых заполнена публичная MAX-ссылка."""
    async with async_session() as session:
        result = await session.execute(
            select(Channel.tg_channel_username, Channel.max_channel_url)
            .where(
                Channel.user_id == user_id,
                Channel.max_channel_url.is_not(None),
                Channel.max_channel_url != "",
            )
        )
        rows = result.all()

    mapping: dict[str, str] = {}
    for tg_username, max_url in rows:
        if not tg_username or not max_url:
            continue
        if max_url.startswith("http://") or max_url.startswith("https://"):
            mapping[tg_username] = max_url
    return mapping


async def build_reverse_link_mapping(user_id: int) -> dict[str, str]:
    """Возвращает {max_channel_url → https://t.me/tg_username} для кроспостинга MAX→TG."""
    async with async_session() as session:
        result = await session.execute(
            select(Channel.tg_channel_username, Channel.max_channel_url)
            .where(
                Channel.user_id == user_id,
                Channel.max_channel_url.is_not(None),
                Channel.max_channel_url != "",
                Channel.tg_channel_username.is_not(None),
                Channel.tg_channel_username != "",
            )
        )
        rows = result.all()

    mapping: dict[str, str] = {}
    for tg_username, max_url in rows:
        if not tg_username or not max_url:
            continue
        tg_link = f"https://t.me/{tg_username}"
        # Маппим max_url → tg_link
        mapping[max_url] = tg_link
        # Также маппим домен без https
        if max_url.startswith("https://"):
            mapping[max_url[8:]] = tg_link
        elif max_url.startswith("http://"):
            mapping[max_url[7:]] = tg_link
    return mapping
