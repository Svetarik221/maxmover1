import os

import aiohttp
import structlog

from bot.config import settings

log = structlog.get_logger()

# Маппинг наших типов медиа на типы MAX API
MEDIA_TYPE_MAP = {
    "photo": "image",
    "video": "video",
    "audio": "audio",
    "document": "file",
}


async def upload_media_to_max(file_path: str, media_type: str) -> dict | None:
    """Загружает медиафайл в MAX (двухшаговый процесс) и возвращает attachment.

    Шаг 1: POST /uploads?type=<type> → получаем upload URL
    Шаг 2: POST на upload URL с файлом → получаем token
    Возвращает dict вида {"type": "image", "payload": {"token": "..."}}
    """
    if not file_path or not os.path.exists(file_path):
        return None

    max_type = MEDIA_TYPE_MAP.get(media_type, "file")
    base_url = settings.max_api_base_url
    headers = {"Authorization": settings.max_bot_token}

    try:
        async with aiohttp.ClientSession() as session:
            import json as _json
            # Шаг 1: получаем URL для загрузки
            async with session.post(
                f"{base_url}/uploads",
                headers=headers,
                params={"type": max_type},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Ошибка получения upload URL", status=resp.status, body=body)
                    return None
                data = await resp.json(content_type=None)
                upload_url = data.get("url")
                # Для video MAX отдаёт token уже на шаге 1
                step1_token = data.get("token")
                if not upload_url:
                    log.error("upload URL отсутствует", data=data)
                    return None

            # Шаг 2: загружаем файл
            with open(file_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("file", f, filename=os.path.basename(file_path))
                async with session.post(upload_url, data=form) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("Ошибка загрузки файла в MAX", status=resp.status, body=body)
                        return None
                    # vu.okcdn.ru может вернуть не-JSON. Парсим robustly.
                    raw = await resp.text()
                    upload_result = {}
                    try:
                        upload_result = _json.loads(raw) if raw else {}
                    except Exception:
                        log.warning("upload ответ не JSON, продолжаем", body=raw[:200])

            # Извлекаем token: сначала из upload_result, затем из шага 1 (для video)
            token = _extract_token(upload_result, max_type) or step1_token
            if not token:
                log.error("Не удалось извлечь token из ответа", result=upload_result)
                return None

            log.info("Медиа загружено в MAX", file=file_path, type=max_type)
            return {"type": max_type, "payload": {"token": token}}

    except Exception:
        log.exception("Ошибка загрузки медиа", file_path=file_path)
        return None


def _extract_token(upload_result: dict, max_type: str) -> str | None:
    """Извлекает token из ответа загрузки в зависимости от типа."""
    if max_type == "image":
        photos = upload_result.get("photos", {})
        if photos:
            first = next(iter(photos.values()))
            return first.get("token")
    elif max_type == "video":
        videos = upload_result.get("videos", {})
        if videos:
            first = next(iter(videos.values()))
            return first.get("token")
        # Иногда токен в корне
        return upload_result.get("token")
    elif max_type == "audio":
        audios = upload_result.get("audios", {})
        if audios:
            first = next(iter(audios.values()))
            return first.get("token")
        return upload_result.get("token")
    elif max_type == "file":
        return upload_result.get("token")

    return None
