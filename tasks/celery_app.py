from celery import Celery

from bot.config import settings

celery_app = Celery(
    "maxmover",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Moscow",
    enable_utc=True,
    task_track_started=True,
)

celery_app.autodiscover_tasks(["tasks"])

celery_app.conf.beat_schedule = {
    "cleanup-old-data-daily": {
        "task": "tasks.scheduled_tasks.cleanup_old_data",
        "schedule": 86400,  # раз в день
    },
    "health-check-5min": {
        "task": "tasks.scheduled_tasks.health_check",
        "schedule": 300,  # каждые 5 минут
    },
}

import tasks.transfer_tasks   # noqa: E402, F401
import tasks.scheduled_tasks  # noqa: E402, F401
