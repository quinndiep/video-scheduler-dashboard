#!/usr/bin/env python3
"""In-process FastAPI tests using the Starlette TestClient.

These run without binding a port, so they are fast and CI-friendly. They cover
the FastAPI-specific behaviour that the HTTP smoke test cannot assert against a
live server: OpenAPI generation, Pydantic validation, the 202 + Location
response and the error envelope shape.

Usage: python3 test_app.py
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

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
    client = TestClient(app_module.app)

    print("\n=== OpenAPI ===")
    schema = client.get("/api/v1/openapi.json")
    check("openapi.json 200", schema.status_code == 200, str(schema.status_code))
    paths = schema.json()["paths"]
    check("/api/v1/upload documented", "/api/v1/upload" in paths, sorted(paths)[:5])
    check(
        "UploadRequest schema generated",
        "UploadRequest" in schema.json()["components"]["schemas"],
        sorted(schema.json()["components"]["schemas"])[:8],
    )

    print("\n=== Docs ===")
    check("swagger docs 200", client.get("/api/v1/docs").status_code == 200)
    check("redoc 200", client.get("/api/v1/redoc").status_code == 200)

    print("\n=== Health ===")
    resp = client.get("/health")
    check("health 200", resp.status_code == 200)
    check("health reports healthy", resp.json()["status"] == "healthy")

    print("\n=== Unauthenticated ===")
    resp = client.get("/api/v1/jobs")
    check("401 unauthenticated", resp.status_code == 401, str(resp.status_code))
    body = resp.json()
    check("error envelope", body.get("success") is False
          and body["error"]["message"] == "Authentication required", str(body))

    print("\n=== Auth flow ===")
    email = f"fastapi_{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "TestPass!2345"}
    )
    check("register 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:150]}")
    check("session cookie set", "session" in resp.cookies, list(resp.cookies))

    resp = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "TestPass!2345"}
    )
    check("duplicate register 400", resp.status_code == 400, str(resp.status_code))

    resp = client.get("/api/v1/auth/status")
    check("auth status authenticated", resp.json().get("authenticated") is True,
          str(resp.json()))

    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "WrongPass!1"}
    )
    check("bad password 401", resp.status_code == 401, str(resp.status_code))
    check("no account enumeration",
          resp.json()["error"]["message"] == "Invalid email or password",
          str(resp.json()))

    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "TestPass!2345"},
    )
    check("unknown email same message",
          resp.json()["error"]["message"] == "Invalid email or password",
          str(resp.json()))

    print("\n=== Google gate ===")
    resp = client.post("/api/v1/upload", json={
        "drive_file_id": "f1", "title": "t", "platforms": ["youtube"],
    })
    check("401 without Google", resp.status_code == 401, str(resp.status_code))
    check("google_not_connected code",
          resp.json()["error"].get("code") == "google_not_connected",
          str(resp.json()))

    # Give this test user the same fake Google credential the other suites use.
    from server_secure import load_users, save_users

    users = load_users()
    users[email] = {
        "google_token": {
            "token": "ya29.fake-token",
            "refresh_token": "1//fake",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake.apps.googleusercontent.com",
            "client_secret": "fake",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        }
    }
    save_users(users)

    print("\n=== Pydantic validation (400, not FastAPI's default 422) ===")
    for label, payload in [
        ("missing title", {"drive_file_id": "f", "platforms": ["youtube"]}),
        ("missing drive_file_id", {"title": "t", "platforms": ["youtube"]}),
        ("missing platforms", {"drive_file_id": "f", "title": "t"}),
        ("unsupported platform",
         {"drive_file_id": "f", "title": "t", "platforms": ["myspace"]}),
        ("bad privacy",
         {"drive_file_id": "f", "title": "t", "platforms": ["youtube"],
          "privacy": "secret"}),
        ("title too long",
         {"drive_file_id": "f", "title": "x" * 200, "platforms": ["youtube"]}),
        ("empty title", {"drive_file_id": "f", "title": "", "platforms": ["youtube"]}),
        ("bad scheduled_time",
         {"drive_file_id": "f", "title": "t", "platforms": ["youtube"],
          "scheduled_time": "not-a-date"}),
    ]:
        resp = client.post("/api/v1/upload", json=payload)
        check(f"400 for {label}", resp.status_code == 400,
              f"got {resp.status_code} {resp.text[:120]}")

    resp = client.post("/api/v1/upload",
                       content=b"{not json",
                       headers={"Content-Type": "application/json"})
    check("400 invalid JSON", resp.status_code == 400, str(resp.status_code))

    resp = client.post("/api/v1/upload", content=b"a=1",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("415 wrong content type", resp.status_code == 415, str(resp.status_code))

    resp = client.get("/api/v1/jobs?status=bogus")
    check("400 bad status filter", resp.status_code == 400, str(resp.status_code))

    print("\n=== 202 + Location ===")
    resp = client.post("/api/v1/upload", json={
        "drive_file_id": "drive_test_sched",
        "title": "FastAPI Scheduled",
        "platforms": ["YouTube", "tiktok"],
        "scheduled_time": "2099-01-01T00:00:00",
    })
    check("202 accepted", resp.status_code == 202,
          f"{resp.status_code} {resp.text[:150]}")
    data = resp.json()["data"]
    job_id = data["job_id"]
    check("Location header", resp.headers.get("location") == f"/api/v1/jobs/{job_id}",
          str(resp.headers.get("location")))
    check("stays pending", data["status"] == "pending", str(data))
    check("platforms normalised",
          client.get(f"/api/v1/jobs/{job_id}").json()["data"]["platforms"]
          == ["youtube", "tiktok"], "platforms not normalised")

    print("\n=== Job lifecycle ===")
    resp = client.get("/api/v1/jobs/does-not-exist")
    check("404 unknown job", resp.status_code == 404, str(resp.status_code))

    resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    check("200 cancel", resp.status_code == 200, f"{resp.status_code} {resp.text[:120]}")
    resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    check("409 re-cancel", resp.status_code == 409, str(resp.status_code))

    resp = client.post(f"/api/v1/jobs/{job_id}/start")
    check("200 start cancelled", resp.status_code == 200,
          f"{resp.status_code} {resp.text[:120]}")
    resp = client.post(f"/api/v1/jobs/{job_id}/start")
    check("409 re-start running", resp.status_code == 409, str(resp.status_code))

    print("\n=== 404 / 405 envelope ===")
    resp = client.get("/api/v1/nope")
    check("404 envelope", resp.status_code == 404
          and resp.json()["success"] is False, resp.text[:120])
    resp = client.get("/api/v1/upload")
    check("405 envelope", resp.status_code == 405
          and resp.json()["success"] is False, resp.text[:120])
    check("Allow header on 405", "POST" in (resp.headers.get("allow") or ""),
          str(resp.headers.get("allow")))

    print("\n=== Accounts ===")
    resp = client.get("/api/v1/accounts")
    check("200 list accounts", resp.status_code == 200
          and resp.json()["data"]["accounts"] == [], resp.text[:150])
    resp = client.get("/api/v1/accounts/nope")
    check("404 unknown account", resp.status_code == 404, str(resp.status_code))

    print("\n=== Legacy deprecation headers ===")
    resp = client.get("/api/schedules")
    check("legacy schedules 200", resp.status_code == 200, str(resp.status_code))
    check("Deprecation header", resp.headers.get("deprecation") == "true",
          str(resp.headers.get("deprecation")))
    check("X-API-Migrate-To", resp.headers.get("x-api-migrate-to") == "/api/v1/jobs",
          str(resp.headers.get("x-api-migrate-to")))

    print("\n=== Logout ===")
    check("logout 200", client.post("/api/v1/auth/logout").status_code == 200)
    check("401 after logout", client.get("/api/v1/jobs").status_code == 401)

    print("\n" + "=" * 60)
    print(f"PASSED: {len(passed)}   FAILED: {len(failed)}")
    for f in failed:
        print("  FAILED:", f)
    if failed:
        sys.exit(1)
    print("ALL FASTAPI TESTS PASSED")


if __name__ == "__main__":
    main()