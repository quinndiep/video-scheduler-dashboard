#!/usr/bin/env python3
"""Verify the API falls back to an in-process thread when the broker is down.

Points Celery at an unreachable broker and calls ``dispatch_upload`` directly,
confirming it degrades to a thread instead of raising. This is what keeps the
API usable on a developer machine with no Redis running.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
    # Point the broker at a port nothing listens on.
    import celery_app

    celery_app.app.conf.broker_url = "redis://127.0.0.1:6399/0"

    import app as app_module
    import tasks
    from jobs_db import JobStatus

    job_id = tasks.job_repo.create({
        "user_id": "fallback_user",
        "drive_file_id": "file_fallback",
        "title": "Fallback Test",
        "platforms": ["youtube"],
        "status": JobStatus.PENDING,
    })
    tasks.job_repo.claim(job_id)

    print("=== Broker unreachable ===")
    try:
        executor = app_module.dispatch_upload(job_id, ["youtube"])
        check("dispatch_upload did not raise", True)
        check("fell back to a thread", executor == "thread", executor)
    except Exception as e:
        check("dispatch_upload did not raise", False, f"{type(e).__name__}: {e}")

    import time

    time.sleep(3)
    row = tasks.job_repo.get(job_id)
    print(f"  job status after fallback: {row['status']}")
    check("fallback thread actually ran",
          row["status"] in JobStatus.TERMINAL, row["status"])

    print("\n" + "=" * 60)
    print(f"PASSED: {len(passed)}   FAILED: {len(failed)}")
    for f in failed:
        print("  FAILED:", f)
    if failed:
        sys.exit(1)
    print("FALLBACK TEST PASSED")


if __name__ == "__main__":
    main()