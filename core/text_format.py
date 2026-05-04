"""Утилита: подстановка имени MAX-бота в шаблоны текстов handler'ов.

Используется чтобы не хардкодить конкретный username MAX-бота —
он берётся из .env (MAX_BOT_USERNAME).

Использование:
    from core.text_format import t
    await message.answer(t("Добавь бота {MAX_BOT_USERNAME} в канал"))
"""
from bot.config import settings


def t(text: str) -> str:
    """Подставить плейсхолдеры в тексте."""
    bot_name = settings.max_bot_username or "(MAX_BOT_USERNAME не задан в .env)"
    return text.replace("{MAX_BOT_USERNAME}", bot_name)
