from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    tg_bot_token: str
    tg_bot_username: str = ""  # username бота без @, например myposts_bot
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str

    # MAX
    max_bot_token: str
    max_bot_username: str = ""  # username бота в MAX (для open_app кнопок мини-приложения)

    # Служебный MAX-чат для relay между основным сервером и DE-воркером.
    # Создаётся в MAX как обычная группа, бот должен быть АДМИНОМ
    # с правом удалять сообщения. Иначе история накапливается и DE
    # может перепубликовать старые задачи (см. README про инцидент-2026-04-26).
    relay_chat_id: str = ""  # id служебной группы в MAX (создайте сами)

    # Telegram-id администратора (получает алерты health_check)
    admin_tg_id: int = 0

    # Database
    database_url: str = "postgresql+asyncpg://maxmover:maxmover@localhost:5432/maxmover"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Payments (опционально — если хотите использовать платные тарифы через ЮKassa)
    yokassa_shop_id: str = ""
    yokassa_secret_key: str = ""
    open_mode: bool = True  # True = все фичи доступны всем, тарифов нет

    # Webhook
    webhook_host: str = ""  # пример: https://yourbot.example.com (нужен SSL)
    webhook_path: str = "/webhook/tg"
    max_webhook_path: str = "/webhook/max"

    # Proxy (если ваш сервер в РФ — без них Telegram не работает)
    tg_api_proxy: str = ""  # socks5://user:pass@host:port для Bot API (aiogram)
    tg_mtproto_proxy_host: str = ""  # MTProto-прокси для Telethon
    tg_mtproto_proxy_port: int = 443
    tg_mtproto_proxy_secret: str = ""  # hex secret

    # Адрес мини-аппы (для inline-кнопок «Комментарии» в MAX)
    webapp_url: str = ""  # пример: https://yourbot.example.com

    # DE-worker (если используется отдельный сервер с Telethon в Германии)
    telethon_api_url: str = ""  # пример: http://1.2.3.4:8900

    # Dev
    max_dev_mode: bool = False

    @property
    def max_api_base_url(self) -> str:
        return "https://platform-api.max.ru"


settings = Settings()
