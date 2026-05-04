"""Заглушка тарифов для self-hosted (open mode).

Все фичи доступны без ограничений. Сохранены сигнатуры функций
для совместимости с остальным кодом.
"""
from bot.config import settings


# Стабы для совместимости (нигде в self-hosted не используются строго)
POST_LIMITS = {"open": 1000}
MAX_CHANNELS_PRO = 999
FEATURE_ACCESS = {
    "comments":     {"open"},
    "autopost":     {"open"},
    "crosspost":    {"open"},
    "schedule":     {"open"},
    "link_replace": {"open"},
}
TARIFF_NAMES = {"open": "Self-hosted"}
UPGRADE_HINTS = {
    "comments":  "",
    "autopost":  "",
    "crosspost": "",
    "schedule":  "",
    "channels":  "",
}


async def get_channel_tariff(channel_id: int) -> str:
    return "open" if settings.open_mode else "free"


async def get_user_tariff(user_id: int) -> str:
    return "open" if settings.open_mode else "free"


async def get_user_tariff_by_tg_id(tg_user_id: int) -> str:
    return "open" if settings.open_mode else "free"


async def can_add_channel(user_id: int) -> tuple[bool, str]:
    return True, ""


def can_use(tariff: str, feature: str) -> bool:
    """В open mode все фичи доступны."""
    if settings.open_mode:
        return True
    return tariff in FEATURE_ACCESS.get(feature, set())


def get_post_limit(tariff: str) -> int:
    return 1000
