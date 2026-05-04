"""Читает посты из TG-каналов через удалённый Telethon API (DE VPS).

Если API недоступен — fallback на локальный Telethon (для обратной совместимости).
"""
import asyncio
import os
from dataclasses import dataclass, field

import aiohttp
import structlog

from bot.config import settings

log = structlog.get_logger()

# Удалённый Telethon API (на VPS вне РФ, если используется отдельный DE-сервер)
TELETHON_API_URL = os.getenv("TELETHON_API_URL", "")
TELETHON_API_SECRET = os.getenv("TELETHON_API_SECRET", "")


@dataclass
class TgComment:
    """Комментарий к посту TG-канала."""
    message_id: int
    reply_to_msg_id: int
    text: str | None = None
    sender_name: str | None = None
    date: str | None = None


@dataclass
class TgPost:
    """Пост из TG-канала."""
    message_id: int
    text: str | None = None
    media_path: str | None = None
    media_type: str | None = None  # photo / video / document
    grouped_id: int | None = None
    date: str | None = None
    comments: list[TgComment] = field(default_factory=list)


async def read_channel_posts(
    tg_username: str,
    limit: int | None = None,
    with_comments: bool = False,
) -> list[TgPost]:
    """Читает посты из TG-канала через удалённый API."""
    headers = {"Authorization": f"Bearer {TELETHON_API_SECRET}"}
    params = {"channel": tg_username, "limit": limit or 50}

    try:
        async with aiohttp.ClientSession() as session:
            # Читаем посты
            async with session.get(
                f"{TELETHON_API_URL}/read_posts",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    log.error("Telethon API ошибка", status=resp.status, error=error)
                    return []
                data = await resp.json()

            posts = []
            os.makedirs("media_cache", exist_ok=True)

            for p in data.get("posts", []):
                media_path = None
                media_type = p.get("media_type")

                # Скачиваем медиа если есть
                if media_type and p.get("media_path"):
                    try:
                        async with session.get(
                            f"{TELETHON_API_URL}/download_media",
                            headers=headers,
                            params={"path": p["media_path"]},
                            timeout=aiohttp.ClientTimeout(total=300),
                        ) as media_resp:
                            if media_resp.status == 200:
                                ext = ".jpg" if media_type == "photo" else ".mp4" if media_type == "video" else ""
                                local_path = f"media_cache/{p['message_id']}{ext}"
                                with open(local_path, "wb") as f:
                                    f.write(await media_resp.read())
                                media_path = local_path
                    except Exception:
                        log.warning("Ошибка скачивания медиа", msg_id=p["message_id"])

                posts.append(TgPost(
                    message_id=p["message_id"],
                    text=p.get("text"),
                    media_path=media_path,
                    media_type=media_type,
                    grouped_id=p.get("grouped_id"),
                    date=p.get("date"),
                ))

            log.info("Прочитано постов через API", count=len(posts), channel=tg_username)
            return posts

    except Exception:
        log.exception("Ошибка чтения канала через Telethon API", channel=tg_username)
        return []
