"""
Celery task wrapping the investigation pipeline, so an in-flight
investigation survives an API process restart (it's durable in Redis/Celery,
not just an in-memory asyncio task tied to the FastAPI worker process).

app/main.py and app/routers/alerts.py call `run_investigation_task.delay(...)`
instead of FastAPI's BackgroundTasks. If the Celery broker is unreachable
(e.g. Redis down, or in tests / DEMO_MODE without a worker), the callers
catch that and fall back to running the same logic inline via
`run_investigation_sync` -- see the try/except around `.delay()` in
app/main.py for the exact fallback behavior.

backend/scripts/run_demo.py deliberately does NOT go through Celery at all
-- it calls app.agents.coordinator.run_investigation directly via
asyncio.run(), so the demo never needs Redis running.
"""
import asyncio
import logging

from .celery_app import celery_app
from .database import SessionLocal

logger = logging.getLogger("rootcause.tasks")


def run_investigation_sync(incident_id: str) -> None:
    """Shared implementation: opens its own DB session and runs the
    investigation coroutine to completion. Called either directly (as a
    BackgroundTasks/inline fallback) or from the Celery task below."""
    from .agents.coordinator import run_investigation

    db = SessionLocal()
    try:
        asyncio.run(run_investigation(incident_id, db))
    except Exception:
        logger.exception("Investigation failed for incident %s", incident_id)
    finally:
        db.close()


@celery_app.task(name="rootcause.run_investigation", bind=True, max_retries=2, default_retry_delay=10)
def run_investigation_task(self, incident_id: str) -> None:
    run_investigation_sync(incident_id)
