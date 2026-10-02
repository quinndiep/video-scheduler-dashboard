#!/usr/bin/env python3
"""Verify the Celery app and task module register correctly.

Runs in eager mode so no broker is required. Checks task registration, routing,
the beat schedule, and that the platform fan-out produces a correct aggregated
result for a simulated multi-platform upload.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Eager mode keeps this script runnable without Redis.
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

logging.basicConfig(level=logging.INFO)

passed = []
failed = []


def check(label, condition, detail=""):
    """Record one assertion result."""
    if condition:
        passed.append(label)
        print(f"  PASS {label}")
    else:
        failed.append(f"{label} :: {detail}")
        print(f"  FAIL {label} :: {detail}")


def main():
    import celery_app
    import tasks
    from jobs_db import JobStatus

    app = celery_app.app

    print("\n=== Celery configuration ===")
    check("broker configured", bool(app.conf.broker_url), app.conf.broker_url)
    check("result backend configured", bool(app.conf.result_backend),
          app.conf.result_backend)
    check("acks_late enabled", app.conf.task_acks_late is True)
    check("reject_on_worker_lost enabled",
          app.conf.task_reject_on_worker_lost is True)
    check("prefetch multiplier is 1", app.conf.worker_prefetch_multiplier == 1,
          str(app.conf.worker_prefetch_multiplier))
    check("json serializer only",
          app.conf.task_serializer == "json"
          and app.conf.accept_content == ["json"], app.conf.accept_content)
    check("UTC enabled", app.conf.enable_utc is True)

    print("\n=== Registered tasks ===")
    registered = sorted(t for t in app.tasks if t.startswith("tasks."))
    print("  tasks:", registered)
    for name in [
        "tasks.upload_to_platform",
        "tasks.aggregate_results",
        "tasks.process_scheduled_jobs",
        "tasks.cleanup_old_jobs",
    ]:
        check(f"{name} registered", name in registered)

    print("\n=== Beat schedule ===")
    beat = sorted(app.conf.beat_schedule)
    print("  entries:", beat)
    check("process-scheduled-jobs scheduled",
          "process-scheduled-jobs" in beat)
    check("cleanup-old-jobs scheduled", "cleanup-old-jobs" in beat)

    print("\n=== JobStatus taxonomy ===")
    check("PARTIALLY_COMPLETED exists",
          JobStatus.PARTIALLY_COMPLETED == "partially_completed")
    check("TERMINAL includes partial",
          JobStatus.PARTIALLY_COMPLETED in JobStatus.TERMINAL)

    print("\n=== Error classification ===")
    check("rate limit -> transient",
          isinstance(tasks._classify(RuntimeError("Quota exceeded, rate limit")),
                    tasks.TransientAPIError),
          tasks._classify(RuntimeError("rate limit")))

    class FakeError(Exception):
        def __init__(self, code):
            super().__init__(f"HTTP {code}")
            self.code = code

    check("503 -> transient",
          isinstance(tasks._classify(FakeError(503)), tasks.TransientAPIError))
    check("429 -> transient",
          isinstance(tasks._classify(FakeError(429)), tasks.TransientAPIError))
    check("403 -> permanent",
          isinstance(tasks._classify(FakeError(403)), tasks.PermanentAPIError))
    check("400 -> permanent",
          isinstance(tasks._classify(FakeError(400)), tasks.PermanentAPIError))

    print("\n=== Aggregation ===")
    results = [
        {"platform": "youtube", "status": "success", "result": {"videoId": "abc"}},
        {"platform": "tiktok", "status": "success", "result": {"publishId": "z"}},
    ]
    out = tasks.aggregate_results.apply(args=(results, "test-job-all-ok"))
    check("all success -> completed", out.get()["status"] == JobStatus.COMPLETED,
          str(out.get()))

    results = [
        {"platform": "youtube", "status": "success", "result": {}},
        {"platform": "tiktok", "status": "failed", "error": "no token"},
    ]
    out = tasks.aggregate_results.apply(args=(results, "test-job-partial"))
    payload = out.get()
    check("mixed -> partially_completed",
          payload["status"] == JobStatus.PARTIALLY_COMPLETED, str(payload))
    check("failed platform listed", payload["failed"] == ["tiktok"], str(payload))
    check("succeeded platform listed",
          payload["succeeded"] == ["youtube"], str(payload))

    results = [{"platform": "youtube", "status": "failed", "error": "x"}]
    out = tasks.aggregate_results.apply(args=(results, "test-job-all-fail"))
    check("all failed -> failed", out.get()["status"] == JobStatus.FAILED,
          str(out.get()))

    print("\n=== Aggregate writes to the job row ===")
    job_id = tasks.job_repo.create({
        "user_id": "celery_test_user",
        "drive_file_id": "file_celery",
        "title": "Celery Aggregate Test",
        "platforms": ["youtube", "tiktok"],
        "status": JobStatus.PROCESSING,
    })
    tasks.aggregate_results.apply(args=([
        {"platform": "youtube", "status": "success", "result": {"videoId": "v1"}},
        {"platform": "tiktok", "status": "failed", "error": "no credential"},
    ], job_id))
    row = tasks.job_repo.get(job_id)
    check("job marked partially_completed",
          row["status"] == JobStatus.PARTIALLY_COMPLETED, row["status"])
    check("progress 100", row["progress"] == 100, str(row["progress"]))
    check("per-platform results persisted",
          set(row["result"]) == {"youtube", "tiktok"}, str(row["result"])[:120])
    check("error mentions failed platform",
          "tiktok" in (row["error"] or ""), str(row["error"]))
    tasks.job_repo.delete(job_id)

    print("\n=== Unknown job is permanent, not retried ===")
    out = tasks.upload_to_platform.apply(args=("no-such-job", "youtube"))
    payload = out.get()
    check("unknown job fails fast",
          payload["status"] == "failed" and "not found" in payload["error"],
          str(payload))

    print("\n=== Scheduled sweep ===")
    out = tasks.process_scheduled_jobs.apply().get()
    check("sweep returns counts", "due" in out and "dispatched" in out, str(out))

    print("\n" + "=" * 60)
    print(f"PASSED: {len(passed)}   FAILED: {len(failed)}")
    for f in failed:
        print("  FAILED:", f)
    if failed:
        sys.exit(1)
    print("ALL CELERY CHECKS PASSED")


if __name__ == "__main__":
    main()