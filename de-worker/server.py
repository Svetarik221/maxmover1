"""Telethon API — читает TG-каналы и отдаёт посты по HTTP."""
import asyncio
import os
import json
import hashlib
import hmac
from aiohttp import web
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ.get("TG_PHONE", "")
API_SECRET = os.environ["TELETHON_API_SECRET"]  # любой случайный длинный пароль (см. README)
SESSION_PATH = os.environ.get("TELETHON_SESSION_PATH", "./sessions/bot_session")
MEDIA_DIR = os.environ.get("TELETHON_MEDIA_DIR", "./media_cache")

os.makedirs(MEDIA_DIR, exist_ok=True)

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)


def check_auth(request):
    token = request.headers.get("Authorization", "")
    return token == f"Bearer {API_SECRET}"


async def read_posts(request):
    if not check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    channel = request.query.get("channel")
    limit = int(request.query.get("limit", 50))
    if not channel:
        return web.json_response({"error": "channel required"}, status=400)

    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            return web.json_response({"error": "session not authorized"}, status=500)

        entity = await client.get_entity(channel)
        posts = []
        async for msg in client.iter_messages(entity, limit=limit):
            post = {
                "message_id": msg.id,
                "text": msg.text,
                "media_type": None,
                "media_url": None,
                "grouped_id": msg.grouped_id,
                "date": msg.date.isoformat() if msg.date else None,
            }
            if msg.media:
                if isinstance(msg.media, MessageMediaPhoto):
                    post["media_type"] = "photo"
                    path = await client.download_media(msg, file=f"{MEDIA_DIR}/{msg.id}")
                    post["media_path"] = path
                elif isinstance(msg.media, MessageMediaDocument):
                    mime = msg.media.document.mime_type or ""
                    post["media_type"] = "video" if mime.startswith("video/") else "document"
                    try:
                        path = await asyncio.wait_for(
                            client.download_media(msg, file=f"{MEDIA_DIR}/{msg.id}"),
                            timeout=300
                        )
                        post["media_path"] = path
                    except asyncio.TimeoutError:
                        post["media_path"] = None

            posts.append(post)

        posts.reverse()
        return web.json_response({"posts": posts, "count": len(posts)})

    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def download_media(request):
    """Отдаёт скачанный медиа-файл по пути."""
    if not check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    path = request.query.get("path")
    if not path or not os.path.exists(path):
        return web.json_response({"error": "file not found"}, status=404)

    return web.FileResponse(path)


async def health(request):
    connected = client.is_connected()
    return web.json_response({"status": "ok", "connected": connected})


async def on_startup(app):
    await client.connect()
    print(f"Telethon connected: {await client.is_user_authorized()}")


async def on_shutdown(app):
    await client.disconnect()


app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)
app.router.add_get("/read_posts", read_posts)
app.router.add_get("/download_media", download_media)
app.router.add_get("/health", health)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8900)
