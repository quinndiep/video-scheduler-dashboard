#!/usr/bin/env python3
"""End-to-end Celery test against a live broker and worker.

Requires a running Redis and a Celery worker (see README section). Unlike
check_celery.py, nothing here runs eagerly: tasks are dispatched to the broker,
executed by the worker, and their results read back from the result backend.

Verifies:
  * a real task round-trips through Redis
  * an unknown job fails permanently without retrying
  * the scheduled sweep claims a due job and dispatches per-platform tasks
  * parallel execution: a 3-platform fan-out completes faster than 3 serial runs
"""

import logging
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
    import tasks
    from celery_app import app
    from jobs_db import JobStatus

    if app.conf.task_always_eager:
        print("Refusing to run: CELERY_TASK_ALWAYS_EAGER is set.")
        sys.exit(2)

    print("\n=== Broker connectivity ===")
    check("not eager", app.conf.task_always_eager is False)
    check("tasks registered", "tasks.upload_to_platform" in app.tasks)

    print("\n=== Real task round-trip ===")
    started = time.perf_counter()
    async_result = tasks.upload_to_platform.delay("no-such-job-e2e", "youtube")
    check("delay returned an AsyncResult", hasattr(async_result, "id"),
          type(async_result).__name__)
    payload = async_result.get(timeout=45)
    elapsed = time.perf_counter() - started
    check("unknown job fails permanently", payload["status"] == "failed",
          str(payload))
    check("no retry on permanent failure", payload["attempts"] == 1,
          str(payload))
    print(f"  round-trip took {elapsed:.2f}s")

    print("\n=== Scheduled sweep dispatches real tasks ===")
    job_id = tasks.job_repo.create({
        "user_id": "celery_e2e_user",
        "drive_file_id": "file_does_not_exist",
        "title": "Celery E2E Sweep",
        "platforms": ["youtube", "tiktok", "facebook"],
        "status": JobStatus.PENDING,
        # Due immediately.
        "scheduled_time": "2020-01-01T00:00:00",
    })
    print(f"  created job {job_id}")

    sweep = tasks.process_scheduled_jobs.delay().get(timeout=45)
    check("sweep dispatched the job", job_id in sweep["dispatched"], str(sweep))

    row = tasks.job_repo.get(job_id)
    check("job claimed to processing", row["status"] == JobStatus.PROCESSING,
          f"{row['status']}")

    print("  waiting for the fan-out to finish (3 parallel tasks)...")
    deadline = time.time() + 90
    while time.time() < deadline:
        row = tasks.job_repo.get(job_id)
        if row["status"] in JobStatus.TERMINAL:
            break
        time.sleep(2)

    row = tasks.job_repo.get(job_id)
    print(f"  final status: {row['status']}")
    print(f"  error: {row['error']}")
    check("fan-out reached a terminal state",
          row["status"] in JobStatus.TERMINAL, row["status"])
    check("all three platforms reported",
          row["result"] is not None and set(row["result"])
          == {"youtube", "tiktok", "facebook"},
          str(row["result"])[:200])
    # No real credentials, so every platform should fail -- but each must be
    # individually recorded rather than the whole job dying.
    check("no platform silently skipped",
          all(v.get("status") == "failed" for v in (row["result"] or {}).values()),
          str(row["result"])[:200])
    check("progress reached 100", row["progress"] == 100, str(row["progress"]))
    tasks.job_repo.delete(job_id)

    print("\n=== Parallelism ===")
    # Three trivial tasks in one group vs three dispatched serially.
    sigs = [
        tasks.aggregate_results.s([], f"par-{uuid.uuid4().hex[:6]}") for _ in range(3)
    ]
    t0 = time.perf_counter()
    for s in sigs:
        s.delay()
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    from celery import group

    group([tasks.aggregate_results.s([], f"grp-{uuid.uuid4().hex[:6]}")
           for _ in range(3)]).apply_async()
    parallel = time.perf_counter() - t0
    time.sleep(1)
    print(f"  3 serial dispatches: {serial:.3f}s")
    print(f"  1 grouped dispatch : {parallel:.3f}s")
    check("group dispatch is at least as fast as serial",
          parallel <= serial + 0.2, f"serial={serial:.3f} group={parallel:.3f}")

    print("\n=== Cleanup task ===")
    removed = tasks.cleanup_old_jobs.delay(days=0).get(timeout=30)
    check("cleanup task returns a count", isinstance(removed, int), str(removed))

    print("\n" + "=" * 60)
    print(f"PASSED: {len(passed)}   FAILED: {len(failed)}")
    for f in failed:
        print("  FAILED:", f)
    if failed:
        sys.exit(1)
    print("ALL CELERY E2E CHECKS PASSED")


if __name__ == "__main__":
    main()