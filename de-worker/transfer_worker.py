"""DE Transfer Worker v5 — MAX API relay (без сторонних сервисов).

Все секреты читаются из переменных окружения (см. .env.example):
  TG_API_ID, TG_API_HASH        — Telegram API из my.telegram.org
  MAX_BOT_TOKEN                  — токен бота в MAX
  MAX_BOT_USERNAME               — username бота в MAX
  RELAY_CHAT_ID                  — id служебного MAX-чата для relay (бот должен быть админом!)
  TELETHON_SESSION_PATH          — путь к Telethon-сессии (default: ./sessions/bot_session)
  TELETHON_MEDIA_DIR             — путь к кэшу медиа (default: ./media_cache)
"""
import asyncio
import os
import json
import time
import aiohttp
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.errors import FloodWaitError

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TELETHON_SESSION_PATH", "./sessions/bot_session")
MAX_API = "https://platform-api.max.ru"
MAX_TOKEN = os.environ["MAX_BOT_TOKEN"]
MAX_HEADERS = {"Authorization": MAX_TOKEN}
CONTROL_CHAT = os.environ["RELAY_CHAT_ID"]
CTRL_PREFIX = "_XFER_"
DONE_PREFIX = "_DONE_"
MEDIA_DIR = os.environ.get("TELETHON_MEDIA_DIR", "./media_cache")
MEDIA_TYPE_MAP = {"photo": "image", "video": "video", "document": "file"}
import re as _re

MAX_BOT_USERNAME = os.environ.get("MAX_BOT_USERNAME", "")

def html_for_max(text):
    """Конвертирует Telethon HTML в формат MAX.

    MAX поддерживает: <b>, <i>, <code>, <pre>, <del>, <ins>.
    НЕ поддерживает: <a>, <em>, <strong>, <span> и пр.
    """
    if not text:
        return text
    # MAX поддерживает <a> — конвертация не нужна
    # Эквиваленты
    text = text.replace("<em>", "<i>").replace("</em>", "</i>")
    text = text.replace("<strong>", "<b>").replace("</strong>", "</b>")
    # Убираем неподдерживаемые теги (оставляем содержимое)
    text = _re.sub(r"</?(?:span|u|sub|sup|small|big|tt|font)[^>]*>", "", text)
    return text


def replace_links(text, mapping):
    """Замена TG-ссылок на MAX-ссылки."""
    if not text or not mapping:
        return text or ""
    m = {k.lower(): v for k, v in mapping.items() if v}
    def _sub(match):
        u = match.group(1).lower()
        return m.get(u, match.group(0))
    text = _re.sub(r"https?://t\.me/([a-zA-Z_][a-zA-Z0-9_]*)/\d+", _sub, text)
    text = _re.sub(r"\bt\.me/([a-zA-Z_][a-zA-Z0-9_]*)/\d+", _sub, text)
    text = _re.sub(r"https?://t\.me/([a-zA-Z_][a-zA-Z0-9_]*)\b", _sub, text)
    text = _re.sub(r"\bt\.me/([a-zA-Z_][a-zA-Z0-9_]*)\b", _sub, text)
    def _sub_at(match):
        u = match.group(1).lower()
        return m[u] if u in m else match.group(0)
    text = _re.sub(r"(?<![A-Za-z0-9_/])@([a-zA-Z_][a-zA-Z0-9_]{3,})\b", _sub_at, text)
    return text

POLL_INTERVAL = 15
STARTUP_TS = 0  # set in main()
JOB_TIMEOUT = 600

os.makedirs(MEDIA_DIR, exist_ok=True)
client = TelegramClient(SESSION, API_ID, API_HASH)
client.parse_mode = "html"  # чтобы message.text возвращал HTML с разметкой (bold, italic, links)
_DEDUP_DIR = os.environ.get("DEDUP_STATE_DIR", os.path.dirname(os.path.abspath(__file__)))
PROCESSED_JOBS_FILE = os.path.join(_DEDUP_DIR, "processed_jobs.json")
PROCESSED_POSTS_FILE = os.path.join(_DEDUP_DIR, "processed_posts.json")

# 30 дней — в служебном MAX-чате история накапливается, и на любой
# повтор уже-обработанного _XFER_/_POST_ (например после рестарта DE
# или если delete_msg отказал) мы должны однозначно сказать "уже было".
DEDUP_TTL_SEC = 30 * 86400


def _load_processed_jobs():
    try:
        with open(PROCESSED_JOBS_FILE) as f:
            data = json.load(f)
            return {int(k): float(v) for k, v in data.items()}
    except Exception:
        return {}


def _save_processed_jobs():
    try:
        with open(PROCESSED_JOBS_FILE, "w") as f:
            json.dump({str(k): v for k, v in processed_jobs.items()}, f)
    except Exception:
        pass


def _load_processed_posts():
    try:
        with open(PROCESSED_POSTS_FILE) as f:
            data = json.load(f)
            # ключ хранится как "tg_username|m1,m2,m3" → восстанавливаем кортеж
            result = {}
            for k, v in data.items():
                if "|" not in k:
                    continue
                u, m = k.split("|", 1)
                mids = tuple(int(x) for x in m.split(",") if x)
                if u and mids:
                    result[(u, mids)] = float(v)
            return result
    except Exception:
        return {}


def _save_processed_posts():
    try:
        with open(PROCESSED_POSTS_FILE, "w") as f:
            data = {f"{k[0]}|{','.join(str(x) for x in k[1])}": v for k, v in processed_posts.items()}
            json.dump(data, f)
    except Exception:
        pass


processed_jobs = _load_processed_jobs()  # job_id -> timestamp, persist
processed_posts = _load_processed_posts()  # (tg_username, tuple(msg_ids)) -> timestamp, persist


async def add_comment_button(http_session, mid, chat_id):
    """Добавляет кнопку комментариев к посту в MAX."""
    safe_mid = mid.replace(".", "-")
    safe_chat = str(chat_id).replace("-", "n")
    payload = f"{safe_mid}__{safe_chat}"
    edit_body = {
        "attachments": [{
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{
                    "type": "open_app",
                    "text": "\U0001f4ac Комментарии",
                    "web_app": MAX_BOT_USERNAME,
                    "payload": payload,
                }]]
            }
        }]
    }
    try:
        async with http_session.put(
            f"{MAX_API}/messages?message_id={mid}",
            headers=MAX_HEADERS,
            json=edit_body,
        ) as resp:
            if resp.status == 200:
                print(f"    comment button added: {mid}")
    except Exception:
        pass


async def upload_media(session, file_path, media_type):
    max_type = MEDIA_TYPE_MAP.get(media_type, "file")
    try:
        async with session.post(f"{MAX_API}/uploads", headers=MAX_HEADERS,
                               params={"type": max_type}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            upload_url = data.get("url")
            step1_token = data.get("token")
            if not upload_url:
                return None
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=os.path.basename(file_path))
            async with session.post(upload_url, data=form) as resp:
                raw = await resp.text()
                upload_result = {}
                try:
                    upload_result = json.loads(raw) if raw else {}
                except Exception:
                    pass
        token = None
        if isinstance(upload_result, dict):
            token = upload_result.get("token")
            if not token and "photos" in upload_result:
                for v in upload_result["photos"].values():
                    if isinstance(v, dict) and "token" in v:
                        token = v["token"]
                        break
        return {"type": max_type, "payload": {"token": token or step1_token}} if (token or step1_token) else None
    except Exception as e:
        print(f"    upload error: {e}")
        return None


async def do_transfer(job, http_session):
    job_id = job.get("job_id")
    processed_jobs[job_id] = time.time()
    _save_processed_jobs()

    tg_username = job.get("tg_username")
    max_channel_id = job.get("max_channel_id")
    total_posts = job.get("total_posts") or 100
    chat_id = job.get("chat_id")

    add_comments = job.get("add_comments", True)
    existing_ids = set(job.get("existing_ids", []))
    link_mapping = job.get("link_mapping", {}) or {}
    mappings_to_report = []  # (tg_id, max_mid) для записи в post_mapping на RU
    if existing_ids:
        print(f"[JOB {job_id}] Пропускаю {len(existing_ids)} уже перенесённых постов")
    print(f"[JOB {job_id}] @{tg_username} -> MAX {max_channel_id}, лимит {total_posts}, comments={add_comments}")

    try:
        if not client.is_connected():
            await client.connect()

        entity = await client.get_entity(tg_username)
        posts = []
        from datetime import datetime, timezone as _tz
        read_limit = min(total_posts * 3, 3000)
        this_year_start = datetime(datetime.now().year, 1, 1, tzinfo=_tz.utc)
        async for msg in client.iter_messages(entity, limit=read_limit):
            # Для Старт/Про: только посты за текущий год
            if total_posts > 30 and msg.date and msg.date < this_year_start:
                break
            media_path = None
            media_type = None
            if msg.media:
                if isinstance(msg.media, MessageMediaPhoto):
                    media_type = "photo"
                elif isinstance(msg.media, MessageMediaDocument):
                    mime = msg.media.document.mime_type or ""
                    media_type = "video" if mime.startswith("video/") else "document"
                if media_type:
                    try:
                        media_path = await asyncio.wait_for(
                            client.download_media(msg, file=f"{MEDIA_DIR}/{msg.id}"),
                            timeout=30)
                    except asyncio.TimeoutError:
                        print(f"  download timeout msg {msg.id}")
            posts.append({"id": msg.id, "text": msg.text,
                         "media_path": media_path, "media_type": media_type,
                         "grouped_id": msg.grouped_id})

        posts.reverse()

        # Группируем альбомы (посты с одним grouped_id → один пост с несколькими медиа)
        grouped_posts = []
        current_group = None
        for p in posts:
            gid = p.get("grouped_id")
            if gid and current_group and current_group.get("grouped_id") == gid:
                # Добавляем медиа к текущему альбому
                if p.get("media_path"):
                    current_group.setdefault("medias", []).append(
                        {"path": p["media_path"], "type": p["media_type"]})
                # Сохраняем первое непустое id для дедупа
                current_group.setdefault("all_ids", []).append(p["id"])
                # Подбираем подпись если была не на первом
                if not current_group.get("text") and p.get("text"):
                    current_group["text"] = p["text"]
            else:
                # Новый пост
                ng = {
                    "id": p["id"],
                    "text": p.get("text"),
                    "grouped_id": gid,
                    "all_ids": [p["id"]],
                    "medias": [],
                }
                if p.get("media_path"):
                    ng["medias"].append({"path": p["media_path"], "type": p["media_type"]})
                grouped_posts.append(ng)
                current_group = ng

        # Сначала отфильтровываем уже перенесённые
        new_posts = [
            p for p in grouped_posts
            if not all(pid in existing_ids for pid in p.get("all_ids", [p["id"]]))
        ]
        # Берём последние total_posts из НОВЫХ
        posts = new_posts[-total_posts:] if total_posts else new_posts
        print(f"[JOB {job_id}] Прочитано {len(posts)} постов (после группировки альбомов), публикую...")

        published = 0

        async def _publish_one(post):
            nonlocal published
            text = post["text"] or ""
            attachments = []
            for media in post.get("medias", []):
                if media["path"] and os.path.exists(media["path"]):
                    att = await upload_media(http_session, media["path"], media["type"])
                    if att:
                        attachments.append(att)
            if link_mapping and text:
                text = replace_links(text, link_mapping)
            if text:
                text = html_for_max(text)
            body = {}
            if text:
                body["text"] = text
                body["format"] = "html"
            if attachments:
                body["attachments"] = attachments
            if not body:
                print(f"  SKIP empty body: id={post.get('id')} text={repr(post.get('text'))[:80]} medias_in={len(post.get('medias',[]))} attached={len(attachments)}")
                return
            try:
                async with http_session.post(
                    f"{MAX_API}/messages", headers=MAX_HEADERS,
                    params={"chat_id": max_channel_id}, json=body
                ) as resp:
                    status = resp.status
                    body_resp = await resp.text() if status != 200 else ""
                    resp_json = await resp.json(content_type=None) if status == 200 else None
                if status != 200:
                    if "not.ready" in body_resp or "not.processed" in body_resp:
                        print(f"  video retry id={post.get('id')}...")
                        await asyncio.sleep(5)
                        async with http_session.post(f"{MAX_API}/messages", headers=MAX_HEADERS, params={"chat_id": max_channel_id}, json=body) as _rr:
                            status = _rr.status
                            resp_json = await _rr.json(content_type=None) if status == 200 else None
                        if status != 200:
                            print(f"  retry failed id={post.get('id')}: skip")
                            return
                    else:
                        print(f"  FAIL publish {post.get('id')}: HTTP {status} body={body_resp[:200]}")
                        return
                published += 1
                new_mid = (resp_json or {}).get("message", {}).get("body", {}).get("mid") if resp_json else None
                if new_mid:
                    for tid in post.get("all_ids", [post["id"]]):
                        mappings_to_report.append({"channel_id": job.get("channel_id"),
                                                  "tg_id": tid,
                                                  "max_mid": new_mid})
                    if add_comments:
                        await asyncio.sleep(3)
                        new_atts = list(attachments)
                        new_atts.append({
                            "type": "inline_keyboard",
                            "payload": {
                                "buttons": [[{
                                    "type": "open_app",
                                    "text": "\U0001f4ac Комментарии",
                                    "web_app": MAX_BOT_USERNAME,
                                    "payload": f"{new_mid.replace('.', '-')}__{str(max_channel_id).replace('-', 'n')}",
                                }]]
                            }
                        })
                        try:
                            await http_session.put(
                                f"{MAX_API}/messages?message_id={new_mid}",
                                headers=MAX_HEADERS,
                                json={"attachments": new_atts},
                            )
                        except Exception:
                            pass
            except Exception as e:
                print(f"  msg {post['id']}: ERROR {e}")

        for post in posts:
            try:
                await asyncio.wait_for(_publish_one(post), timeout=90)
            except asyncio.TimeoutError:
                print(f"  TIMEOUT msg {post.get('id')}: skip after 90s")
            except Exception as e:
                print(f"  WRAP error msg {post.get('id')}: {e}")
            await asyncio.sleep(0.3)
            for media in post.get("medias", []):
                if media["path"] and os.path.exists(media["path"]):
                    try:
                        os.remove(media["path"])
                    except Exception:
                        pass

        print(f"[JOB {job_id}] Готово: {published}/{len(posts)}")

        # Отправляем mappings чанками (MAX ограничивает text <= 4000 символов)
        if mappings_to_report:
            CHUNK = 30
            total_chunks = (len(mappings_to_report) + CHUNK - 1) // CHUNK
            for i in range(0, len(mappings_to_report), CHUNK):
                chunk_idx = i // CHUNK + 1
                chunk = mappings_to_report[i:i+CHUNK]
                map_data = json.dumps({"mappings": chunk})
                for _retry in range(3):
                    try:
                        async with http_session.post(
                            f"{MAX_API}/messages", headers=MAX_HEADERS,
                            params={"chat_id": CONTROL_CHAT},
                            json={"text": f"_MAP_{map_data}"}
                        ) as _resp:
                            if _resp.status == 200:
                                print(f"[JOB {job_id}] _MAP_ chunk {chunk_idx}/{total_chunks} отправлен ({len(chunk)} записей)")
                                break
                            else:
                                print(f"[JOB {job_id}] _MAP_ chunk {chunk_idx} FAIL: {_resp.status} {(await _resp.text())[:100]}")
                    except Exception as _e:
                        print(f"[JOB {job_id}] _MAP_ chunk {chunk_idx} ERROR: {_e}")
                    await asyncio.sleep(2)

        # Отчитываемся через MAX API
        done_data = json.dumps({"job_id": job_id, "transferred": published, "chat_id": chat_id})
        await http_session.post(
            f"{MAX_API}/messages", headers=MAX_HEADERS,
            params={"chat_id": CONTROL_CHAT},
            json={"text": f"{DONE_PREFIX}{done_data}"})

    except FloodWaitError as e:
        print(f"[JOB {job_id}] FloodWait {e.seconds}s")
        done_data = json.dumps({"job_id": job_id, "transferred": 0, "chat_id": chat_id,
                                "error": f"Telegram ограничил, повторите через {e.seconds}с"})
        await http_session.post(
            f"{MAX_API}/messages", headers=MAX_HEADERS,
            params={"chat_id": CONTROL_CHAT},
            json={"text": f"{DONE_PREFIX}{done_data}"})
    except Exception as e:
        print(f"[JOB {job_id}] ОШИБКА: {e}")
        done_data = json.dumps({"job_id": job_id, "transferred": 0, "chat_id": chat_id,
                                "error": str(e)[:200]})
        try:
            await http_session.post(
                f"{MAX_API}/messages", headers=MAX_HEADERS,
                params={"chat_id": CONTROL_CHAT},
                json={"text": f"{DONE_PREFIX}{done_data}"})
        except Exception:
            pass


async def delete_msg(http_session, mid):
    """Удаляет служебное сообщение из MAX-канала."""
    try:
        await http_session.delete(
            f"{MAX_API}/messages",
            headers=MAX_HEADERS,
            params={"message_id": mid},
        )
    except Exception:
        pass




async def do_single_post(job, http_session):
    """Скачивает пост(ы) из TG через Telethon и публикует в MAX.
    Поддерживает и одиночное сообщение (message_id), и альбом (message_ids=[...])."""
    from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
    tg_username = job.get("tg_username")
    message_ids = job.get("message_ids") or [job.get("message_id")]
    message_ids = [m for m in message_ids if m]
    if not message_ids:
        return
    max_channel_id = job.get("max_channel_id")
    channel_id = job.get("channel_id")
    add_comments = job.get("add_comments", False)

    label = f"msg={message_ids[0]}" if len(message_ids) == 1 else f"album={message_ids}"
    print(f"[SINGLE POST] @{tg_username} {label} -> MAX {max_channel_id}")

    media_paths = []  # (path, type) для очистки
    try:
        if not client.is_connected():
            await client.connect()

        entity = await client.get_entity(tg_username)
        raw = await client.get_messages(entity, ids=message_ids)
        msgs_list = raw if isinstance(raw, list) else [raw]
        msgs_list = [m for m in msgs_list if m]
        if not msgs_list:
            print(f"[SINGLE POST] msgs {message_ids} not found")
            return

        # Текст — первый непустой caption
        text = ""
        for m in msgs_list:
            if m.text:
                text = m.text
                break

        # Скачиваем все медиа
        attachments = []
        for m in msgs_list:
            if not m.media:
                continue
            mtype = None
            if isinstance(m.media, MessageMediaPhoto):
                mtype = "photo"
            elif isinstance(m.media, MessageMediaDocument):
                mime = m.media.document.mime_type or ""
                mtype = "video" if mime.startswith("video/") else "document"
            if not mtype:
                continue
            try:
                mpath = await asyncio.wait_for(
                    client.download_media(m, file=f"{MEDIA_DIR}/{m.id}"),
                    timeout=120)
            except asyncio.TimeoutError:
                print(f"[SINGLE POST] download timeout msg {m.id}")
                continue
            if mpath and os.path.exists(mpath):
                media_paths.append((mpath, mtype))
                att = await upload_media(http_session, mpath, mtype)
                if att:
                    attachments.append(att)

        body = {}
        if text:
            body["text"] = text
            body["format"] = "html"
        if attachments:
            body["attachments"] = attachments
        if not body:
            return

        # Публикуем с retry для video
        published = False
        for _try in range(4):
            async with http_session.post(
                f"{MAX_API}/messages", headers=MAX_HEADERS,
                params={"chat_id": max_channel_id}, json=body
            ) as resp:
                if resp.status == 200:
                    published = True
                    resp_data = await resp.json(content_type=None)
                    new_mid = resp_data.get("message", {}).get("body", {}).get("mid")

                    # Комменты
                    if new_mid and add_comments:
                        await asyncio.sleep(3)
                        cur = []
                        async with http_session.get(
                            f"{MAX_API}/messages?message_ids={new_mid}",
                            headers=MAX_HEADERS
                        ) as gr:
                            if gr.status == 200:
                                gd = await gr.json(content_type=None)
                                if gd.get("messages"):
                                    cur = gd["messages"][0].get("body", {}).get("attachments", [])
                        atts = [a for a in cur if a.get("type") != "inline_keyboard"]
                        safe_mid = new_mid.replace(".", "-")
                        safe_chat = str(max_channel_id).replace("-", "n")
                        atts.append({"type": "inline_keyboard", "payload": {"buttons": [[{
                            "type": "open_app", "text": "\U0001f4ac Комментарии",
                            "web_app": MAX_BOT_USERNAME,
                            "payload": f"{safe_mid}__{safe_chat}",
                        }]]}})
                        await http_session.put(
                            f"{MAX_API}/messages?message_id={new_mid}",
                            headers=MAX_HEADERS,
                            json={"attachments": atts})

                    print(f"[SINGLE POST] published OK mid={new_mid}")
                    break
                else:
                    body_resp = await resp.text()
                    if "not.ready" in body_resp or "not.processed" in body_resp:
                        await asyncio.sleep(15 * (_try + 1))
                        continue
                    print(f"[SINGLE POST] FAIL {resp.status} {body_resp[:100]}")
                    break

    except Exception as e:
        print(f"[SINGLE POST] ERROR: {e}")
    finally:
        for mpath, _ in media_paths:
            if mpath and os.path.exists(mpath):
                try:
                    os.remove(mpath)
                except Exception:
                    pass

async def poll_jobs(http_session):
    """Читает служебный MAX-канал на наличие задач."""
    while True:
        try:
            async with http_session.get(
                f"{MAX_API}/messages",
                headers=MAX_HEADERS,
                params={"chat_id": CONTROL_CHAT, "count": 20},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    for msg in data.get("messages", []):
                        text = msg.get("body", {}).get("text", "")
                        mid = msg.get("body", {}).get("mid", "")
                        msg_ts = msg.get("timestamp", 0) / 1000  # ms to sec
                        if msg_ts < STARTUP_TS:
                            continue  # игнорируем старые сообщения
                        if text.startswith("_POST_"):
                            try:
                                post_job = json.loads(text[len("_POST_"):])
                                mids = post_job.get("message_ids") or [post_job.get("message_id")]
                                mids = tuple(sorted(m for m in mids if m))
                                key = (post_job.get("tg_username"), mids)
                                if key[0] and key[1] and key not in processed_posts:
                                    processed_posts[key] = time.time()
                                    _save_processed_posts()
                                    asyncio.create_task(do_single_post(post_job, http_session))
                            except json.JSONDecodeError:
                                pass
                            if mid:
                                await delete_msg(http_session, mid)
                            continue
                        if text.startswith(CTRL_PREFIX):
                            try:
                                job = json.loads(text[len(CTRL_PREFIX):])
                                if job.get("job_id") and job["job_id"] not in processed_jobs:
                                    processed_jobs[job["job_id"]] = time.time()
                                    _save_processed_jobs()
                                    print(f"Задача из MAX: job_id={job['job_id']}")
                                    asyncio.create_task(
                                        asyncio.wait_for(do_transfer(job, http_session),
                                                        timeout=JOB_TIMEOUT))
                                # Удаляем служебное сообщение
                                if mid:
                                    await delete_msg(http_session, mid)
                            except json.JSONDecodeError:
                                pass
                        elif text.startswith(DONE_PREFIX):
                            # Удаляем _DONE_ сообщения тоже
                            if mid:
                                await delete_msg(http_session, mid)
        except Exception as e:
            print(f"Poll error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def cleanup_media():
    while True:
        await asyncio.sleep(3600)
        try:
            now = time.time()
            for f in os.listdir(MEDIA_DIR):
                path = os.path.join(MEDIA_DIR, f)
                if os.path.isfile(path) and now - os.path.getmtime(path) > 3600:
                    os.remove(path)
            posts_dirty = False
            for k in [k for k, t in processed_posts.items() if now - t > DEDUP_TTL_SEC]:
                processed_posts.pop(k, None)
                posts_dirty = True
            if posts_dirty:
                _save_processed_posts()
            jobs_dirty = False
            for jid in [j for j, t in processed_jobs.items() if now - t > DEDUP_TTL_SEC]:
                processed_jobs.pop(jid, None)
                jobs_dirty = True
            if jobs_dirty:
                _save_processed_jobs()
        except Exception:
            pass


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("Telethon NOT AUTHORIZED!")
        return

    global STARTUP_TS
    # Подхватываем задачи за последние 30 минут — на случай рестарта воркера
    # чтобы pending job'ы из MAX-чата не терялись.
    STARTUP_TS = time.time() - 1800
    print(f"Transfer worker v5 (MAX API relay, channel={CONTROL_CHAT}, catch-up 30 min)")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120, connect=15, sock_read=60)
    ) as session:
        await asyncio.gather(
            poll_jobs(session),
            cleanup_media(),
        )


if __name__ == "__main__":
    asyncio.run(main())
