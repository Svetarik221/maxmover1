import asyncio

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from services.transfer_service import execute_transfer
from tasks.celery_app import celery_app

log = structlog.get_logger()


def _progress_bar(current: int, total: int, width: int = 10) -> str:
    """Генерирует текстовый прогресс-бар."""
    filled = int(width * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (width - filled)
    percent = int(100 * current / total) if total > 0 else 0
    return f"{bar} {percent}%"


@celery_app.task(bind=True, rate_limit="20/m", max_retries=3)
def run_transfer(
    self,
    channel_id: int,
    tg_username: str,
    max_channel_id: str,
    post_limit: int | None,
    chat_id: int,
) -> dict:
    """Создаёт задачу переноса в БД. Выполняется DE-воркером."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _create_transfer_job(channel_id, tg_username, max_channel_id, post_limit, chat_id)
        )
    finally:
        loop.close()


async def _create_transfer_job(channel_id, tg_username, max_channel_id, post_limit, chat_id):
    import aiohttp
    from db.database import async_session
    from db.models import TransferJob
    from sqlalchemy import select

    # Собираем уже перенесённые post IDs + проверяем тариф для фич
    existing_ids = []
    link_mapping = {}
    add_comments = False
    try:
        from db.models import PostMapping, Channel
        from core.tariff import get_channel_tariff, can_use
        async with async_session() as session:
            result = await session.execute(
                select(PostMapping.tg_message_id).where(PostMapping.channel_id == channel_id)
            )
            existing_ids = [r[0] for r in result.fetchall()]

        # Проверяем тариф канала
        tariff = await get_channel_tariff(channel_id)
        add_comments = can_use(tariff, "comments")
        if can_use(tariff, "link_replace"):
            async with async_session() as session:
                ch = await session.get(Channel, channel_id)
                if ch:
                    from services.link_mapping import build_link_mapping
                    link_mapping = await build_link_mapping(ch.user_id)
    except Exception:
        pass

    async with async_session() as session:
        job = TransferJob(
            channel_id=channel_id,
            total_posts=post_limit,
            transferred_posts=0,
            status="pending",
            error_message=str(chat_id),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # Пушим задачу DE-воркеру через MAX API (служебный канал)
    import json as _json
    ctrl_msg = "_XFER_" + _json.dumps({
        "job_id": job_id,
        "channel_id": channel_id,
        "tg_username": tg_username,
        "max_channel_id": max_channel_id,
        "total_posts": post_limit,
        "chat_id": chat_id,
        "existing_ids": existing_ids,
        "link_mapping": link_mapping,
        "add_comments": add_comments,
    })
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                "https://platform-api.max.ru/messages",
                headers={"Authorization": settings.max_bot_token},
                params={"chat_id": settings.relay_chat_id},
                json={"text": ctrl_msg},
            )
        log.info("Transfer job отправлен DE-воркеру через MAX", job_id=job_id)
    except Exception:
        log.exception("Не удалось отправить задачу")

    return {"status": "queued"}
