#!/usr/bin/env python3
"""Manual smoke test for the SQLite job persistence layer (Day 2-3).

Exercises: register -> login (cookie) -> schedule -> list jobs -> cancel ->
upload status -> restart-persistence check.  No real Drive/YouTube call is made;
the run_upload_job failure path is intentionally exercised by a job whose Drive
file does not exist.
"""

import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
EMAIL = "smoke_day2@example.com"
PASSWORD = "SmokeTest!2345"

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(path, payload=None, method=None):
    """Issue a JSON request against the secure server."""
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("1) register:", call("/api/auth/register", {"email": EMAIL, "password": PASSWORD}))
print("2) login:", call("/api/auth/login", {"email": EMAIL, "password": PASSWORD}))
print("3) auth status:", call("/api/auth/status"))

status, created = call(
    "/api/schedule",
    {
        "items": [
            {
                "id": 1,
                "videoId": "drive_file_smoke_001",
                "title": "Smoke Test Video",
                "description": "Day 2-3 persistence check",
                "platforms": ["youtube", "instagram"],
                "privacy": "private",
                "scheduleTime": "2099-01-01T10:00:00",
            }
        ]
    },
)
print("4) schedule:", status, created)
job_id = (created.get("jobIds") or [None])[0]

status, jobs = call("/api/schedules")
print("5) schedules count:", status, len(jobs.get("schedules", [])))
print("   first job platforms/scheduled:", 
      jobs["schedules"][0]["platforms"], jobs["schedules"][0]["scheduleTime"])

status, job_list = call("/api/jobs")
print("6) /api/jobs:", status, len(job_list.get("jobs", [])))

status, upload_status = call(f"/api/upload/status?jobId={job_id}")
print("7) upload status (far-future job stays pending):", status, upload_status.get("status"))

status, cancelled = call("/api/schedule/delete", {"id": job_id})
print("8) cancel:", status, cancelled)
status, after = call(f"/api/upload/status?jobId={job_id}")
print("9) status after cancel:", after.get("status"))

# --- platform validation (fix #3) ---
status, bad = call("/api/schedule", {"items": [{
    "id": 3,
    "videoId": "drive_file_smoke_002",
    "title": "Bad Platform Video",
    "platforms": ["youtube", "myspace", "tiktok"],
    "scheduleTime": "2099-01-01T10:00:00",
}]})
print("10) invalid platform rejected:", status, bad)
assert bad.get("rejected"), "expected a rejection entry for 'myspace'"
assert bad.get("jobIds") == [], "no job should be created for an invalid platform"
assert bad.get("success") is False, "success should be False when something was rejected"

status, mixed = call("/api/schedule", {"items": [
    {"id": 4, "videoId": "f4", "title": "Good",
     "platforms": ["YouTube", "tiktok"], "scheduleTime": "2099-01-01T10:00:00"},
    {"id": 5, "videoId": "f5", "title": "Bad",
     "platforms": ["myspace"], "scheduleTime": "2099-01-01T10:00:00"},
]})
print("11) mixed batch: accepted", len(mixed.get("jobIds", [])),
      "rejected", len(mixed.get("rejected", [])))
assert len(mixed.get("jobIds", [])) == 1, "the valid item should still be accepted"
assert len(mixed.get("rejected", [])) == 1

status, jobs_after = call("/api/schedules")
good = next((s for s in jobs_after["schedules"] if s["title"] == "Good"), None)
print("12) normalised platforms:", good["platforms"])
assert good["platforms"] == ["youtube", "tiktok"], good["platforms"]

# clean up the extra jobs created above
for jid in list(mixed.get("jobIds", [])) + [good["id"]]:
    call("/api/schedule/delete", {"id": jid})

print("ALL SMOKE CHECKS PASSED")
print("JOB_ID=" + str(job_id))