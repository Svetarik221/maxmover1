import asyncio
import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand

from bot.config import settings
from bot.handlers import start, transfer, cabinet, privacy, schedule, autopost
from db.database import init_db

log = structlog.get_logger()


async def main() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    await init_db()

    storage = RedisStorage.from_url(settings.redis_url)

    # Прокси для Telegram API (если задан)
    session = None
    if settings.tg_api_proxy:
        session = AiohttpSession(proxy=settings.tg_api_proxy)
        log.info("Используем прокси для TG API", proxy=settings.tg_api_proxy)

    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=storage)

    dp.include_router(start.router)
    dp.include_router(cabinet.router)
    dp.include_router(privacy.router)
    dp.include_router(schedule.router)
    dp.include_router(transfer.router)
    dp.include_router(autopost.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="🔄 Перенести посты"),
        BotCommand(command="my", description="👤 Личный кабинет"),
        BotCommand(command="schedule", description="🕐 Запланировать пост"),
        BotCommand(command="scheduled", description="📋 Мои отложенные посты"),
        BotCommand(command="help", description="❓ Помощь"),
        BotCommand(command="privacy", description="📄 Политика конфиденциальности"),
    ])

    log.info("Бот запущен")

    async def watchdog():
        """Если связь с TG API молчит > 6 мин — force-exit, контейнер поднимет docker."""
        consecutive_fails = 0
        while True:
            await asyncio.sleep(120)
            try:
                await asyncio.wait_for(bot.get_me(), timeout=20)
                if consecutive_fails:
                    log.info("Watchdog: связь с TG восстановлена")
                consecutive_fails = 0
            except Exception as e:
                consecutive_fails += 1
                log.warning("Watchdog: TG API не отвечает", attempt=consecutive_fails, error=str(e))
                if consecutive_fails >= 3:
                    log.error("Watchdog: TG недоступен 6+ мин, force-exit для рестарта docker")
                    import os
                    os._exit(1)

    asyncio.create_task(watchdog())

    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post"],
    )


if __name__ == "__main__":
    asyncio.run(main())
