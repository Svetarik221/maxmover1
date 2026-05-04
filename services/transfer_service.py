import asyncio
import os

import structlog

from core.max_publisher import MaxPublisher
from core.telegram_reader import TgPost, read_channel_posts
from db.database import async_session
from db.repositories.transfer_repo import TransferRepo

log = structlog.get_logger()

# Задержка между публикациями для соблюдения rate limit MAX API (30 rps)
PUBLISH_DELAY = 0.5


async def execute_transfer(
    channel_id: int,
    tg_username: str,
    max_channel_id: str,
    post_limit: int | None = None,
    progress_callback=None,
) -> int:
    """Выполняет перенос постов из TG в MAX.

    Args:
        channel_id: ID канала в нашей БД
        tg_username: Username TG-канала
        max_channel_id: ID канала в MAX
        post_limit: Лимит постов (30 для бесплатного тарифа)
        progress_callback: Callback для обновления прогресса (transferred, total)

    Returns:
        Количество перенесённых постов
    """
    posts = await read_channel_posts(tg_username, limit=post_limit)

    # Дедупликация: пропускаем посты, которые уже переносили ранее
    # (например, юзер сделал бесплатные 30 постов, а потом купил тариф)
    async with async_session() as session:
        repo = TransferRepo(session)
        already_transferred = await repo.get_transferred_tg_ids(channel_id)

    if already_transferred:
        before = len(posts)
        posts = [p for p in posts if p.message_id not in already_transferred]
        log.info(
            "Отфильтрованы уже перенесённые посты",
            channel_id=channel_id,
            skipped=before - len(posts),
            remaining=len(posts),
        )

    total = len(posts)

    async with async_session() as session:
        repo = TransferRepo(session)
        job = await repo.create_job(channel_id, total)
        await repo.start_job(job.id)

    # Загружаем справочник публичных MAX-ссылок для замены t.me/* → max.ru/*
    # ТОЛЬКО для платных тарифов (link_replace)
    from services.link_mapping import build_link_mapping
    from core.tariff import get_channel_tariff, can_use
    from db.models import Channel as _Ch
    async with async_session() as session:
        ch = await session.get(_Ch, channel_id)
    link_mapping = {}
    if ch:
        _tariff = await get_channel_tariff(channel_id)
        if can_use(_tariff, "link_replace"):
            link_mapping = await build_link_mapping(ch.user_id)

    publisher = MaxPublisher()
    transferred = 0

    for post in posts:
        try:
            result = await publisher.publish_post(
                chat_id=max_channel_id,
                text=post.text,
                media_path=post.media_path,
                media_type=post.media_type,
                link_mapping=link_mapping,
            )
            # Retry для постов с медиа: MAX иногда не успевает обработать видео,
            # отвечает 400 errors.send-message.empty. Ждём и пробуем ещё.
            if result is None and post.media_path and post.media_type:
                for _retry in range(2):
                    log.warning("Публикация поста с медиа провалилась, ретрай через 12с", post_id=post.message_id, attempt=_retry+1)
                    await asyncio.sleep(12)
                    result = await publisher.publish_post(
                        chat_id=max_channel_id,
                        text=post.text,
                        media_path=post.media_path,
                        media_type=post.media_type,
                        link_mapping=link_mapping,
                    )
                    if result:
                        break

            if result:
                max_msg_id = str(result.get("message", {}).get("body", {}).get("mid", ""))
                async with async_session() as session:
                    repo = TransferRepo(session)
                    await repo.add_post_mapping(
                        channel_id, post.message_id, max_msg_id
                    )

                transferred += 1

                async with async_session() as session:
                    repo = TransferRepo(session)
                    await repo.update_progress(job.id, transferred)

                if progress_callback:
                    await progress_callback(transferred, total)

            # Удаляем медиафайл после публикации — не засоряем диск
            if post.media_path and os.path.exists(post.media_path):
                os.remove(post.media_path)

            await asyncio.sleep(PUBLISH_DELAY)

        except Exception:
            log.exception(
                "Ошибка переноса поста",
                tg_message_id=post.message_id,
            )

    async with async_session() as session:
        repo = TransferRepo(session)
        if transferred > 0:
            await repo.complete_job(job.id)
        else:
            await repo.fail_job(job.id, "Ни один пост не перенесён")

    log.info(
        "Перенос завершён",
        transferred=transferred,
        total=total,
        channel_id=channel_id,
    )
    return transferred
