# MaxMover

Self-hosted бот для переноса постов из Telegram-каналов в [MAX](https://max.ru) (мессенджер VK).

## Что умеет

- 🔄 Перенос всех постов с TG-канала в MAX-канал (текст, фото, видео, документы, альбомы)
- 📤 Автопостинг TG → MAX (новые посты в TG автоматически публикуются в MAX)
- 📥 Кроспостинг MAX → TG (посты, написанные в MAX, дублируются в TG)
- 🕐 Отложенные посты — расписание публикации в TG, MAX или сразу куда
- 💬 Комментарии под постами в MAX через мини-приложение
- 🔗 Автозамена t.me-ссылок на ссылки MAX-каналов

---

## ⚠️ Требования и ограничения

### Что обязательно нужно

1. **VPS или сервер** на Linux с Docker и docker-compose. ~1 GB RAM, 20 GB диск, дешёвый план у любого провайдера ($5–10/мес).
2. **Публичный домен с HTTPS** — например `yourbot.example.com`. MAX без HTTPS не шлёт webhook. Бесплатно через Let's Encrypt + nginx (см. ниже).
3. **TG-бот** — создаётся за минуту через [@BotFather](https://t.me/BotFather).
4. **MAX-бот** — создаётся через @MasterBot в самом MAX.
5. **Телефонный номер для Telethon** — нужен один раз для скачивания постов из чужих TG-каналов.

### Где должен быть сервер

Telegram блокируется в РФ. Поэтому:

- **Простой вариант — VPS вне РФ** (Германия, Нидерланды, Финляндия). Тогда всё работает в один docker-compose, ничего настраивать не нужно.
- **Сложный вариант — VPS в РФ** + MTProto-прокси для Telethon + SOCKS5-прокси для Bot API. Их адреса прописываются в `.env`.

### Лимиты Telegram которые мы не можем обойти

- TG Bot API не может скачать видео > 20 МБ. Большие видео обрабатываются через Telethon — для этого Telethon должен иметь доступ к TG (т.е. сервер вне РФ или MTProto-прокси).
- TG может отдавать `FloodWait` при массовом переносе — бот его обрабатывает, но при сотнях постов перенос займёт время.

---

## Установка (быстрый путь — Docker, всё на одном сервере вне РФ)

### 1. Создаём ботов

**TG-бот:**
1. Откройте [@BotFather](https://t.me/BotFather), напишите `/newbot`
2. Придумайте имя и username (заканчивается на `_bot`)
3. Сохраните **токен** — он нужен для `TG_BOT_TOKEN`
4. Откройте `/setprivacy` → выберите вашего бота → **Disable** (чтобы бот видел все сообщения в TG-канале)

**MAX-бот:**
1. В MAX найдите чат `@MasterBot`
2. Нажмите `/newbot`, придумайте имя
3. Получите **токен** — он нужен для `MAX_BOT_TOKEN`
4. Username бота вида `id123456_1_bot` — нужен для `MAX_BOT_USERNAME`

### 2. Получаем Telegram API_ID и API_HASH

1. Перейдите на [my.telegram.org](https://my.telegram.org), войдите по номеру телефона
2. **API development tools** → создайте приложение (любое имя)
3. Сохраните `App api_id` и `App api_hash` — для `TG_API_ID` и `TG_API_HASH`

### 3. Создаём служебную MAX-группу

1. В MAX создайте новую **группу** (не канал!), назовите как угодно — например, «Служебная»
2. Добавьте туда **вашего MAX-бота** (поиск по username из шага 1)
3. **Сделайте его администратором** — нажмите на бота → ⚙️ → дайте права **«Удалять сообщения»** и «Публиковать сообщения»
   - **Это критично!** Без админ-прав бот не сможет чистить служебные сообщения и со временем начнёт перепубликовывать старые задачи. См. [troubleshooting](#troubleshooting).
4. Узнайте `chat_id` группы:
   - Откройте группу в MAX
   - Найдите её в API: `curl "https://platform-api.max.ru/chats" -H "Authorization: ВАШ_MAX_TOKEN"` — найдёте по названию, `chat_id` будет отрицательным числом
   - Это значение для `RELAY_CHAT_ID`

### 4. Настраиваем сервер

```bash
# на VPS
git clone https://github.com/<ваш-username>/maxmover.git
cd maxmover

cp .env.example .env
nano .env       # заполнить токены, API_ID, RELAY_CHAT_ID, ADMIN_TG_ID, WEBHOOK_HOST
```

### 5. Авторизуем Telethon (один раз)

Telethon (для скачивания постов с чужих TG-каналов) требует подтверждение по SMS:

```bash
# В корне проекта (где .env)
docker-compose run --rm bot python -c "
from telethon import TelegramClient
import os
client = TelegramClient('sessions/bot_session', int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
client.start(phone=os.environ['TG_PHONE'])
print('OK!')
"
```

Введите код из SMS (или из самого Telegram если уже залогинены). Файл `sessions/bot_session.session` создастся — его нельзя дублировать на другом сервере.

### 6. Применяем миграции БД

```bash
docker-compose run --rm bot alembic upgrade head
```

### 7. Запускаем

```bash
docker-compose up -d
```

Проверьте логи:

```bash
docker-compose logs -f bot
```

Должно быть `Бот запущен`.

### 8. Настраиваем nginx с SSL для MAX webhook

MAX шлёт webhook на ваш `WEBHOOK_HOST`. Настройте nginx + certbot:

```nginx
# /etc/nginx/sites-available/maxmover
server {
    listen 443 ssl;
    server_name yourbot.example.com;

    ssl_certificate     /etc/letsencrypt/live/yourbot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourbot.example.com/privkey.pem;

    location /webhook/max {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
    location /api/comments {
        proxy_pass http://127.0.0.1:8443;
    }
    location /miniapp {
        alias /opt/maxmover/miniapp;
        index index.html;
    }
}
```

Привяжите webhook в MAX:

```bash
curl -X POST "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: ВАШ_MAX_TOKEN" \
  -d '{"url":"https://yourbot.example.com/webhook/max"}'
```

### 9. Комментарии в MAX (опционально)

Под каждым постом в MAX-канале появляется кнопка **«💬 Комментарии»**.
Нажатие открывает мини-приложение, где читатели оставляют комментарии.
Без этого шага всё остальное (перенос, автопост, отложка) работает — но
кнопка комментариев будет показывать ошибку «приложение не найдено».

Чтобы кнопка работала:

1. Убедитесь что `/miniapp` отдаётся через nginx (см. конфиг выше).
2. Откройте в браузере `https://yourbot.example.com/miniapp/` —
   должна открыться страница с полем ввода комментария. Если 404 —
   проверьте `alias` в nginx и что папка `miniapp/` доступна.
3. В MAX зайдите в чат с **@MasterBot** → команда `/myapps` →
   выберите вашего бота → **Edit web app URL** → введите:

   ```
   https://yourbot.example.com/miniapp/
   ```

4. Подождите минуту и попробуйте включить комментарии в кабинете
   бота: `/my` → нажмите ❌ Комментарии → должно стать ✅.
5. Опубликуйте тестовый пост. Под ним появится кнопка
   «💬 Комментарии» — нажмите, проверьте что мини-приложение
   открывается и принимает текст.

### 10. Тест

1. Откройте свой TG-бот, нажмите `/start`
2. Пройдите по инструкции — бот спросит TG-канал, MAX-канал, начнёт перенос
3. Готово!

---

## Архитектура (если что-то идёт не так)

```
┌──────────────────────────────────┐
│ Сервер (1 VPS вне РФ)             │
│                                   │
│  ┌────────┐  ┌──────────────┐    │
│  │ bot    │──│ celery_worker│    │
│  └────────┘  └──────────────┘    │
│       │            │              │
│       ▼            ▼              │
│  ┌─────────────────────────┐     │
│  │ db (postgres) + redis   │     │
│  └─────────────────────────┘     │
│                                   │
│  ┌──────────────────────┐         │
│  │ max_webhook (HTTPS)  │ ←── MAX │
│  └──────────────────────┘         │
│                                   │
│  ┌──────────────────────┐         │
│  │ telethon-api         │ ←── TG  │
│  │ (Telethon — большие  │         │
│  │  видео и переносы)   │         │
│  └──────────────────────┘         │
└──────────────────────────────────┘
```

`telethon-api/` — отдельный сервис на Telethon, нужен потому что **Bot API** не может скачивать большие файлы и не работает в РФ без прокси. Если ваш сервер в РФ — `telethon-api` лучше вынести на отдельный VPS вне РФ.

---

## Troubleshooting

### «Бот в группе не админ» / `user.not.admin` при попытке удалять

В служебной группе (`RELAY_CHAT_ID`) **бот должен быть админом** с правом удалять сообщения. Иначе сообщения накапливаются и через 30 дней может произойти повторная публикация старых задач.

### Перенос «застрял»

```bash
# логи DE-воркера (если он отдельным контейнером):
docker-compose logs -f telethon-api
# или systemd-юнит:
journalctl -u transfer-worker.service -f
```

Если видите `download timeout` — Telethon не справился с большим видео. Это нормально: пост пропускается, перенос продолжается.

### Видео >20 МБ не публикуется в MAX через автопост или /schedule

Это лимит TG Bot API — он физически не может скачать файл больше 20 МБ. Решение для автопоста — `telethon-api` (см. архитектуру). Для отложки `/schedule` через бота — нет лёгкого решения, шлите большие видео сразу в TG-канал (autopost подхватит через Telethon).

### MAX перестал слать webhook

1. Проверьте SSL (Let's Encrypt истекает каждые 3 месяца — должен авторенью):
   ```bash
   sudo certbot certificates
   ```
2. Проверьте подписку на webhook:
   ```bash
   curl https://platform-api.max.ru/subscriptions -H "Authorization: ВАШ_MAX_TOKEN"
   ```

### Бот в TG молчит

Это случается при обрыве соединения с прокси. Бот сам перезапустится через 6 минут (встроен watchdog). Если не помогает:

```bash
docker-compose restart bot
```

### Алерты не приходят

Установите `ADMIN_TG_ID` в `.env` (узнать свой id — в `@userinfobot`).

---

## Безопасность

- Не публикуйте `.env` в git! Он в `.gitignore`.
- `MAX_BOT_TOKEN` и `TG_BOT_TOKEN` — секретные. Если утекли — пересоздайте у `@BotFather`/`@MasterBot`.
- `sessions/bot_session.session` — файл Telethon-сессии. Нельзя дублировать на другом сервере, иначе TG разорвёт обе.

---

## Лицензия

MIT (см. `LICENSE`).

Вкладывалось много времени и труда. Если этот код тебе помог — звезда на гитхабе будет приятна.
