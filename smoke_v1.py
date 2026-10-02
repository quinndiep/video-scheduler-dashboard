#!/usr/bin/env python3
"""Day 4 smoke test: /api/v1 namespace, HTTP standards, validation.

Checks, in order:
  * 401 for unauthenticated v1 calls
  * 202 + Location header on POST /api/v1/upload (immediate and scheduled)
  * 200 + standard envelope on GET /api/v1/jobs and /api/v1/jobs/{id}
  * 400 for missing fields, bad platform, bad datetime, bad status filter
  * 404 for unknown job id and unknown endpoint
  * 409 when cancelling a job that is not pending
  * 405 + Allow header for the wrong HTTP method
  * 415 for a non-JSON Content-Type, 413 for an oversized body
  * legacy routes still work and carry Deprecation headers
"""

import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770"
EMAIL = "smoke_day2@example.com"
PASSWORD = "SmokeTest!2345"

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


def call(opener, path, payload=None, method=None, content_type="application/json",
         raw_body=None):
    """Issue a request and return ``(status, headers, parsed_body)``.

    ``headers`` is lower-cased: HTTP header names are case-insensitive, but
    uvicorn emits them lower-case while the stdlib server emits them title-case,
    so the tests must not depend on casing.
    """
    body = raw_body if raw_body is not None else (
        json.dumps(payload).encode() if payload is not None else None
    )
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": content_type} if body is not None else {},
    )
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read()
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                (json.loads(raw) if raw else {}),
            )
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw[:200].decode(errors="replace")}
        return e.code, {k.lower(): v for k, v in e.headers.items()}, parsed


def anon():
    """Return an opener with no session cookie."""
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )


def authed():
    """Return an opener carrying a valid session cookie."""
    op = anon()
    call(op, "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD})
    return op


def main():
    print("\n=== 1. Unauthenticated access ===")
    status, _, body = call(anon(), "/api/v1/jobs")
    check("401 on unauthenticated /api/v1/jobs", status == 401, f"got {status}")
    check("error envelope shape", body.get("success") is False
          and "message" in body.get("error", {}), json.dumps(body)[:160])

    op = authed()
    status, _, body = call(op, "/api/v1/auth/status")
    check("login works", status == 200 and body.get("authenticated") is True,
          json.dumps(body)[:160])

    print("\n=== 2. POST /api/v1/upload -> 202 + Location ===")
    status, headers, body = call(op, "/api/v1/upload", {
        "drive_file_id": "drive_v1_immediate",
        "title": "v1 Immediate Upload",
        "platforms": ["youtube"],
    })
    check("202 Accepted", status == 202, f"got {status} {json.dumps(body)[:200]}")
    job_id = (body.get("data") or {}).get("job_id")
    check("job_id in data", bool(job_id), json.dumps(body)[:200])
    check("Location header", headers.get("location") == f"/api/v1/jobs/{job_id}",
          f"got {headers.get('location')}")
    check("success envelope", body.get("success") is True
          and isinstance(body.get("data"), dict), json.dumps(body)[:160])

    print("\n=== 3. Scheduled upload -> 202, stays pending ===")
    status, headers, body = call(op, "/api/v1/upload", {
        "drive_file_id": "drive_v1_scheduled",
        "title": "v1 Scheduled Upload",
        "description": "Runs later",
        "platforms": ["youtube", "tiktok"],
        "privacy": "unlisted",
        "scheduled_time": "2099-06-01T08:30:00",
    })
    sched_id = (body.get("data") or {}).get("job_id")
    check("202 for scheduled job", status == 202, f"got {status}")
    check("Location header for scheduled", headers.get("location")
          == f"/api/v1/jobs/{sched_id}", f"got {headers.get('location')}")
    check("status reported pending",
          (body.get("data") or {}).get("status") == "pending",
          json.dumps(body)[:200])

    print("\n=== 4. GET /api/v1/jobs ===")
    status, _, body = call(op, "/api/v1/jobs")
    data = body.get("data") or {}
    check("200 on job list", status == 200, f"got {status}")
    check("list has total/limit", "total" in data and "limit" in data,
          json.dumps(body)[:200])
    check("scheduled job listed as pending",
          any(j["job_id"] == sched_id and j["status"] == "pending"
              for j in data.get("jobs", [])), "scheduled job missing/incorrect")

    status, _, body = call(op, "/api/v1/jobs?status=pending&limit=5")
    check("status filter + limit honoured",
          status == 200 and body["data"]["limit"] == 5
          and all(j["status"] == "pending" for j in body["data"]["jobs"]),
          json.dumps(body)[:200])

    print("\n=== 5. GET /api/v1/jobs/{id} ===")
    status, _, body = call(op, f"/api/v1/jobs/{sched_id}")
    check("200 on job fetch", status == 200, f"got {status}")
    check("job body matches", body.get("data", {}).get("title")
          == "v1 Scheduled Upload", json.dumps(body)[:200])

    status, _, body = call(op, "/api/v1/jobs/does-not-exist")
    check("404 unknown job", status == 404, f"got {status}")

    print("\n=== 6. Validation -> 400 ===")
    for label, payload in [
        ("missing title", {"drive_file_id": "x", "platforms": ["youtube"]}),
        ("missing drive_file_id", {"title": "t", "platforms": ["youtube"]}),
        ("missing platforms", {"drive_file_id": "x", "title": "t"}),
        ("unsupported platform",
         {"drive_file_id": "x", "title": "t", "platforms": ["myspace"]}),
        ("bad privacy",
         {"drive_file_id": "x", "title": "t", "platforms": ["youtube"],
          "privacy": "secret"}),
        ("title too long",
         {"drive_file_id": "x", "title": "t" * 200, "platforms": ["youtube"]}),
        ("bad scheduled_time",
         {"drive_file_id": "x", "title": "t", "platforms": ["youtube"],
          "scheduled_time": "not-a-date"}),
        ("platforms not a list",
         {"drive_file_id": "x", "title": "t", "platforms": "youtube"}),
    ]:
        status, _, body = call(op, "/api/v1/upload", payload)
        check(f"400 for {label}", status == 400, f"got {status} {json.dumps(body)[:120]}")

    status, _, body = call(op, "/api/v1/jobs?status=bogus")
    check("400 for invalid status filter", status == 400, f"got {status}")

    print("\n=== 7. Malformed requests -> 400 / 415 / 413 ===")
    status, _, body = call(op, "/api/v1/upload", raw_body=b"{not json")
    check("400 invalid JSON", status == 400, f"got {status}")

    status, _, body = call(op, "/api/v1/upload", raw_body=b"id=1",
                           content_type="application/x-www-form-urlencoded")
    check("415 wrong content type", status == 415, f"got {status}")

    big = json.dumps({"drive_file_id": "x", "title": "t",
                      "platforms": ["youtube"],
                      "description": "y" * (300 * 1024)}).encode()
    status, _, body = call(op, "/api/v1/upload", raw_body=big)
    check("413 oversized body", status == 413, f"got {status}")

    print("\n=== 8. Cancel -> 200 / 409 ===")
    status, _, body = call(op, f"/api/v1/jobs/{sched_id}/cancel", payload={})
    check("200 cancel pending job", status == 200, f"got {status} {json.dumps(body)[:160]}")
    check("cancelled status returned",
          (body.get("data") or {}).get("status") == "cancelled",
          json.dumps(body)[:200])

    status, _, body = call(op, f"/api/v1/jobs/{sched_id}/cancel", payload={})
    check("409 re-cancel", status == 409, f"got {status}")

    status, _, body = call(op, "/api/v1/jobs/nope/cancel", payload={})
    check("404 cancel unknown job", status == 404, f"got {status}")

    print("\n=== 9. Restart a cancelled job, then start it ===")
    status, _, body = call(op, f"/api/v1/jobs/{sched_id}/start", payload={})
    check("200 start cancelled job", status == 200,
          f"got {status} {json.dumps(body)[:160]}")
    check("start reports processing",
          (body.get("data") or {}).get("status") == "processing",
          json.dumps(body)[:200])

    status, _, body = call(op, f"/api/v1/jobs/{sched_id}/start", payload={})
    check("409 re-start a running job", status == 409, f"got {status}")

    print("\n=== 10. 404 / 405 ===")
    status, _, body = call(op, "/api/v1/nonexistent")
    check("404 unknown endpoint", status == 404, f"got {status}")

    status, headers, body = call(op, "/api/v1/upload", method="GET")
    check("405 wrong method", status == 405, f"got {status}")
    check("Allow header present", "POST" in (headers.get("allow") or ""),
          f"got {headers.get('allow')}")

    print("\n=== 11. Legacy routes still work + deprecation ===")
    status, headers, body = call(op, "/api/schedules")
    check("legacy /api/schedules works", status == 200, f"got {status}")
    check("Deprecation header on legacy",
          (headers.get("deprecation") or "").lower() == "true",
          f"headers={ {k: v for k, v in headers.items() if 'eprecat' in k} }")
    check("X-API-Migrate-To header",
          headers.get("x-api-migrate-to") == "/api/v1/jobs",
          f"got {headers.get('x-api-migrate-to')}")

    status, _, body = call(op, "/api/jobs")
    check("legacy /api/jobs works", status == 200, f"got {status}")

    status, headers, body = call(op, "/api/upload/status?jobId=" + (sched_id or ""))
    check("legacy /api/upload/status works", status == 200, f"got {status}")

    print("\n=== 12. v1 has no deprecation headers ===")
    status, headers, _ = call(op, "/api/v1/jobs")
    check("no Deprecation on v1", headers.get("deprecation") is None,
          f"got {headers.get('deprecation')}")

    print("\n" + "=" * 60)
    print(f"PASSED: {len(passed)}   FAILED: {len(failed)}")
    for f in failed:
        print("  FAILED:", f)
    if failed:
        sys.exit(1)
    print("ALL DAY 4 CHECKS PASSED")


if __name__ == "__main__":
    main()