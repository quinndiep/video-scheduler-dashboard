#!/usr/bin/env python3
"""Seed a smoke-test user with a fake (non-functional) Google token.

The token is a syntactically valid credential blob so that ``require_auth``
passes; it will never authenticate against Google, which is exactly what we want
for exercising the scheduling/persistence code paths offline.

Usage: python seed_smoke_user.py <email>
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import server_secure as srv

FAKE_CREDS = {
    "token": "ya29.fake-token-for-smoke-test",
    "refresh_token": "1//fake-refresh-token",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "fake-client-id.apps.googleusercontent.com",
    "client_secret": "fake-client-secret",
    "scopes": list(srv.SCOPES),
}


def main(email: str) -> None:
    users = srv.load_users()
    user = users.setdefault(email, {"password_hash": "", "password_salt": ""})
    user["google_token"] = dict(FAKE_CREDS)
    # Give the other platforms a fake credential each so per-platform
    # credential resolution can be exercised without real API tokens.
    user["instagram_token"] = "IGUFAKEtok_1234567890"
    user["facebook_token"] = "EAABfakeFacebookToken123"
    user["tiktok_token"] = "TTfakeTikTokToken456"
    srv.save_users(users)
    print(f"Seeded fake Google + platform credentials for {email}")
    raw = json.loads(srv.USERS_FILE.read_text())
    stored = raw[email]
    print("google_token on disk starts with:", str(stored["google_token"])[:12])
    print("instagram_token on disk starts with:", str(stored["instagram_token"])[:12])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "smoke_day2@example.com")