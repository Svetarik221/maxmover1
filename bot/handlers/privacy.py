"""Заглушка для команды /privacy.

В self-hosted версии политику конфиденциальности заполняет владелец бота
(если планирует публиковать его для пользователей).
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


PRIVACY_TEXT = (
    "📄 <b>О данных</b>\n\n"
    "Это самостоятельно развёрнутая копия open-source бота. "
    "Все данные (TG-каналы, MAX-каналы, отложки, посты) хранятся "
    "в базе на сервере владельца этого экземпляра бота. "
    "К разработчикам исходного кода ваши данные не попадают.\n\n"
    "Если у вас вопросы — обращайтесь к владельцу этого бота."
)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(PRIVACY_TEXT, disable_web_page_preview=True)
