"""
Celery application instance, backed by Redis (already present in
requirements.txt / docker-compose.yml). Run a worker with:

    celery -A app.celery_app worker --loglevel=info

See docker-compose.yml's `worker` service. The investigation task itself
lives in app/tasks.py (kept separate from this module so it can be imported
without pulling in Celery for callers -- e.g. run_demo.py -- that don't need
a broker at all).
"""
from celery import Celery

from .config import get_settings

settings = get_settings()

celery_app = Celery(
    "rootcause",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Investigations can involve several outbound HTTP calls (Prometheus,
    # Loki, GitLab, K8s, Ollama) -- ack late so a worker crash mid-task
    # requeues it instead of silently dropping it.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

celery_app.autodiscover_tasks(["app"])
