import secrets
import string

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from bot.config import settings

log = structlog.get_logger()


def generate_code() -> str:
    """Генерирует уникальный код верификации вида max-XXXXX."""
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(5))
    return f"max-{suffix}"


async def verify_channel_code(tg_username: str, code: str) -> tuple[bool, int | None]:
    """Проверяет наличие кода верификации в описании TG-канала через Bot API."""
    session = AiohttpSession(proxy=settings.tg_api_proxy) if settings.tg_api_proxy else None
    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    try:
        chat = await bot.get_chat(f"@{tg_username}")
        description = chat.description or ""
        return (code in description), chat.id
    except Exception:
        log.exception("Ошибка верификации канала", tg_username=tg_username)
        return False, None
    finally:
        await bot.session.close()
