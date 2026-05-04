from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import settings
from bot.states.transfer import TransferStates
from db.database import async_session
from db.repositories.user_repo import UserRepo

router = Router()


def _max_bot_link() -> str:
    """Возвращает HTML-ссылку на MAX-бота из настроек или пометку «настройте»."""
    name = settings.max_bot_username or ""
    if not name:
        return "<i>(укажите MAX_BOT_USERNAME в .env)</i>"
    return f'<a href="https://max.ru/{name}">{name}</a>'


def _max_bot_username_html() -> str:
    """Возвращает <code>username</code> бота в MAX или подсказку настроить."""
    name = settings.max_bot_username or ""
    if not name:
        return "<i>(MAX_BOT_USERNAME не задан)</i>"
    return f"<code>{name}</code>"


HELP_TEXT_TEMPLATE = (
    "❓ <b>Как пользоваться ботом</b>\n\n"
    "<b>Перенос постов:</b>\n"
    "1. Нажми /start\n"
    "2. Отправь ссылку на TG-канал (@username)\n"
    "3. Добавь код верификации в описание канала\n"
    "4. Отправь ссылку на MAX-канал\n"
    "5. Добавь бота админом в MAX-канал\n"
    "6. Готово!\n\n"
    "<b>Команды:</b>\n"
    "/start — начать перенос\n"
    "/my — личный кабинет\n"
    "/schedule — запланировать пост\n"
    "/scheduled — мои отложенные посты\n"
    "/help — эта справка\n\n"
    "<b>📝 Боты:</b>\n"
    "• этот TG-бот — перенос постов и автопостинг.\n"
    "• {MAX_BOT_LINK} в MAX — добавь его в свой MAX-канал. "
    "Через него работают комментарии, кроспостинг и автопостинг.\n\n"
    "<b>Как добавить бота в MAX-канал:</b>\n"
    "1. Открой свой канал в MAX → Управление → Подписчики → Добавить\n"
    "2. В поиске введи: {MAX_BOT_CODE} (не ссылку!) и добавь в подписчики\n"
    "3. Затем сделай админом: Управление → Админы → Добавить\n"
    "4. <b>Важно:</b> оставь галочки «Публиковать сообщения», «Редактировать чужие сообщения» и «Удалять чужие сообщения»\n\n"
    "<b>Как добавить TG-бота в TG-канал/группу</b> (для автопостинга и кроспостинга):\n"
    "1. Открой TG-канал → Управление → Администраторы → Добавить\n"
    "2. Найди и добавь этого бота\n"
    "3. <b>Важно:</b> оставь галочку «Публикация сообщений»\n"
)

WELCOME_TEXT = (
    "👋 Привет! Я перенесу посты из твоего TG-канала в MAX.\n\n"
    "Отправь ссылку на свой TG-канал (например, @mychannel или https://t.me/mychannel).\n\n"
    "Нужна помощь? Жми /help"
)


def _help_text() -> str:
    return HELP_TEXT_TEMPLATE.replace("{MAX_BOT_LINK}", _max_bot_link()) \
                              .replace("{MAX_BOT_CODE}", _max_bot_username_html())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_help_text(), disable_web_page_preview=True)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        repo = UserRepo(session)
        await repo.get_or_create(
            tg_user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
    await message.answer(WELCOME_TEXT)
    await state.set_state(TransferStates.waiting_tg_channel)
