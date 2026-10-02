"""
Celery application configuration for the Media Scheduler.

Provides a distributed task queue for video uploads so that long-running Drive
downloads and platform uploads survive worker restarts and can be scaled out.

Broker settings are read from the environment so the same code runs locally and
in production:

===============================  ====================================
``CELERY_BROKER_URL``            broker (default ``redis://localhost:6379/0``)
``CELERY_RESULT_BACKEND``        result store (default Redis db 1)
``CELERY_TASK_ALWAYS_EAGER``     set to 1 to run tasks inline, no broker
===============================  ====================================

Run the pieces:

    # NOTE: use --pool=solo. Celery 5.6.3's prefork pool is broken in this
    # environment: the child never fills ``celery.app.trace._localized``, so
    # every task dies with ``NotRegistered`` (see ``worker_pool`` below).
    celery -A celery_app worker --loglevel=info --pool=solo -Q default,platforms,scheduler,maintenance
    celery -A celery_app beat --loglevel=info
    celery -A celery_app flower --port=5555

To scale out, run several solo workers (each consumes from the same queues) —
that gives real concurrency without the broken prefork fork path. A thread pool
(``--pool=threads``) also works if you want multiple tasks in one process.
"""

import logging
import os

from celery import Celery
from celery.schedules import crontab

# ---------------------------------------------------------------------
# Broker configuration
# ---------------------------------------------------------------------
BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

# Eager mode executes tasks inline instead of shipping them to a broker. It makes
# the test suite (and single-machine development) work without Redis running.
ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "0") == "1"

app = Celery("media_scheduler", broker=BROKER_URL, backend=RESULT_BACKEND)

# Per-platform queues let operators scale each platform independently: a slow
# TikTok API cannot starve YouTube uploads.
PLATFORM_QUEUES = ("youtube", "instagram", "facebook", "tiktok")

app.conf.update(
    # Serialisation: JSON only, so no pickle payloads can cross the broker.
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Delivery guarantees
    task_acks_late=True,            # ack after the task finishes
    task_reject_on_worker_lost=True,  # requeue if the worker dies mid-task
    worker_prefetch_multiplier=1,     # one task at a time per worker process
    task_always_eager=ALWAYS_EAGER,
    # Retry policy
    task_acks_on_failure_or_timeout=True,
    task_default_retry_delay=60,
    task_routes={
        "tasks.upload_to_platform": {"queue": "platforms"},
        "tasks.aggregate_results": {"queue": "default"},
        "tasks.process_scheduled_jobs": {"queue": "scheduler"},
        "tasks.cleanup_old_jobs": {"queue": "maintenance"},
    },
    # Per-task rate limits are applied in tasks.py, where each platform's
    # documented quota is expressed as its own annotation.
    result_expires=3600,
    beat_schedule={
        "process-scheduled-jobs": {
            "task": "tasks.process_scheduled_jobs",
            # Every minute rather than every 5: the scheduler's own loop in
            # server_secure runs at 30s, so uploads should not lag behind.
            "schedule": crontab(minute="*"),
        },
        "cleanup-old-jobs": {
            "task": "tasks.cleanup_old_jobs",
            "schedule": crontab(hour=2, minute=0),
        },
    },
)


class LoggingTask(app.Task):
    """Base task that logs success, failure and retry outcomes uniformly."""

    def on_success(self, retval, task_id, args, kwargs):
        logging.info("Task %s [%s] succeeded", self.name, task_id)
        return super().on_success(retval, task_id, args, kwargs)

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logging.error("Task %s [%s] failed: %s", self.name, task_id, exc)
        return super().on_failure(exc, task_id, args, kwargs, einfo)

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        logging.warning(
            "Task %s [%s] retrying: %s", self.name, task_id, exc
        )
        return super().on_retry(exc, task_id, args, kwargs, einfo)


app.Task = LoggingTask

# Register the task modules. ``tasks`` imports ``celery_app`` for the ``app``
# object, so this import must happen *after* the app is fully configured — and
# it replaces autodiscover_tasks(), which cannot resolve this project because
# it is a flat directory rather than an installed package.
import tasks  # noqa: E402,F401  (registers tasks.* on the app)

# Workaround for a Celery 5.6 prefork bug. ``fast_trace_task`` reads
# ``celery.app.trace._localized``, a cache the worker populates on init. The
# prefork child never fills it, so every task dies — first with "not enough
# values to unpack (expected 3, got 0)", and if the cache is seeded early, with
# "NotRegistered" (Celery swaps ``app._tasks`` during finalization, leaving the
# cache pointing at a stale dict).
#
# Neither reseeding nor clearing ``use_fast_trace_task`` fixes it reliably, so
# the worker is started with ``--pool=solo`` instead. This hook is a
# best-effort safety net for the cases where the cache *is* populated correctly.
from celery import signals  # noqa: E402
from celery.app import trace as celery_trace  # noqa: E402


def _refresh_fast_trace_cache() -> None:
    """Point ``_localized`` at the *current* task registry and accept list."""
    celery_trace._localized[:] = [
        app._tasks,
        celery_trace.prepare_accept_content(app.conf.accept_content),
        celery_trace.gethostname(),
    ]


@signals.worker_init.connect
def _seed_fast_trace_cache(**_kwargs):
    """Refresh the fast-trace cache in the worker process."""
    _refresh_fast_trace_cache()


@signals.worker_process_init.connect
def _reseed_fast_trace_cache(**_kwargs):
    """Refresh it again in each forked child, after its task imports."""
    _refresh_fast_trace_cache()


if __name__ == "__main__":
    app.start()