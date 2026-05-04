import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.config import settings

# В Celery-воркерах используем NullPool — без переиспользования соединений между форками
_pool_class = NullPool if os.getenv("CELERY_WORKER") else None
engine = create_async_engine(
    settings.database_url,
    echo=False,
    poolclass=_pool_class,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Проверяем подключение к БД при старте."""
    async with engine.begin():
        pass


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
