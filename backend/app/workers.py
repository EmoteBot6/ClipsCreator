import os

from celery import Celery
from app.state import REDIS_URL

celery = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL, include=["app.tasks"])
celery.conf.update(
    task_track_started=True,
    # Keep terminal results longer than the watcher's stale-task window.
    result_expires=max(604800, int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_TASK_STALE_SECONDS", "86400")) * 2),
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=max(1, int(os.getenv("LEAGUECLIPS_WORKER_CONCURRENCY", "2"))),
)
