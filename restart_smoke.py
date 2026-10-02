#!/usr/bin/env python3
"""Day 2-3 restart + scheduler smoke test.

Steps:
 1. Insert a job whose scheduled_time is already in the past (due immediately).
 2. Wait for the scheduler loop to pick it up; the upload itself must fail
    because the Drive file / Google token is fake, which is the expected path.
 3. Restart the server process and confirm the job row survived and the startup
    resume logic reports pending jobs.

Usage: python restart_smoke.py <mode>
  mode=due    -> insert a due job and poll for it to leave 'pending'
  mode=check  -> print all rows (used after a restart)
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BASE = "http://127.0.0.1:8770"
EMAIL = "smoke_day2@example.com"
PASSWORD = "SmokeTest!2345"

import server_secure as srv  # noqa: E402


def api(path, payload=None, opener=None):
    """Issue a JSON request against the local secure server."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def login():
    """Return an opener carrying a valid session cookie."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )
    api("/api/auth/login", {"email": EMAIL, "password": PASSWORD}, opener)
    return opener


def insert_due_job(opener):
    """Create a job scheduled in the past so the scheduler fires it at once."""
    status, resp = api("/api/schedule", {
        "items": [{
            "id": 2,
            "videoId": "drive_file_that_does_not_exist",
            "title": "Due Job Smoke Test",
            "description": "Should fail fast and be recorded as failed",
            "platforms": ["youtube"],
            "privacy": "private",
            "scheduleTime": "2020-01-01T00:00:00",
        }]
    }, opener)
    print("insert due job:", status, resp)
    return (resp.get("jobIds") or [None])[0]


def poll_until_settled(job_id, opener, seconds=45):
    """Poll the job until it is no longer pending/processing."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        _, data = api(f"/api/upload/status?jobId={job_id}", None, opener)
        state = (data.get("status"), data.get("progress"))
        if state != last:
            print(f"  t+{int(time.time() - (deadline - seconds))}s status={state}")
            last = state
        if data.get("status") in ("completed", "failed", "cancelled"):
            print("final:", json.dumps(data, indent=2)[:600])
            return data
        time.sleep(3)
    print("TIMEOUT waiting for job to settle, last:", last)
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "due"
    if mode == "due":
        opener = login()
        job_id = insert_due_job(opener)
        poll_until_settled(job_id, opener)
    else:
        rows = srv.job_repo.list(limit=50)
        print("rows in DB after restart:")
        for row in rows:
            print(f"  {row['job_id'][:8]} {row['status']:<12} "
                  f"progress={row['progress']:<4} scheduled={row['scheduled_time']} "
                  f"error={str(row['error'])[:60]}")


if __name__ == "__main__":
    main()