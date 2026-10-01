#!/usr/bin/env python3
"""Standalone Google OAuth setup for video scheduler dashboard."""

import json
import os
import sys
from pathlib import Path

# Configuration
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes/profiles/scheduler")))
TOKEN_PATH = HERMES_HOME / "google_token.json"
CLIENT_SECRET_PATH = HERMES_HOME / "google_client_secret.json"
PENDING_AUTH_PATH = HERMES_HOME / "google_oauth_pending.json"

# Drive + YouTube scopes
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

REDIRECT_URI = "http://localhost:1"


def get_auth_url():
    """Generate OAuth URL for user to visit."""
    if not CLIENT_SECRET_PATH.exists():
        print(f"ERROR: No client secret at {CLIENT_SECRET_PATH}")
        sys.exit(1)
    
    from google_auth_oauthlib.flow import Flow
    
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    
    # Save pending auth state
    PENDING_AUTH_PATH.write_text(json.dumps({
        "state": state,
        "code_verifier": flow.code_verifier,
        "redirect_uri": REDIRECT_URI,
    }, indent=2))
    
    print(auth_url)
    return auth_url


def exchange_code(code_or_url: str):
    """Exchange auth code for token."""
    if not CLIENT_SECRET_PATH.exists():
        print("ERROR: No client secret stored.")
        sys.exit(1)
    
    if not PENDING_AUTH_PATH.exists():
        print("ERROR: No pending OAuth session. Run --auth-url first.")
        sys.exit(1)
    
    pending = json.loads(PENDING_AUTH_PATH.read_text())
    
    # Extract code from URL if full URL provided
    if code_or_url.startswith("http"):
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(code_or_url).query)
        if "code" not in params:
            print("ERROR: No 'code' parameter in URL")
            sys.exit(1)
        code = params["code"][0]
    else:
        code = code_or_url
    
    from google_auth_oauthlib.flow import Flow
    
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=pending["redirect_uri"],
        state=pending["state"],
        code_verifier=pending["code_verifier"],
    )
    
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        print(f"ERROR: Token exchange failed: {e}")
        sys.exit(1)
    
    creds = flow.credentials
    token_data = json.loads(creds.to_json())
    token_data["type"] = "authorized_user"
    
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    
    print(f"OK: Token saved to {TOKEN_PATH}")


def check_auth():
    """Check if we have valid auth."""
    if not TOKEN_PATH.exists():
        print("NOT_AUTHENTICATED")
        return False
    
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        
        if creds.valid:
            print("AUTHENTICATED")
            return True
        
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_data = json.loads(creds.to_json())
            token_data["type"] = "authorized_user"
            TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
            print("AUTHENTICATED (refreshed)")
            return True
    except Exception as e:
        print(f"ERROR: {e}")
        return False
    
    print("TOKEN_INVALID")
    return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--auth-url", action="store_true")
    group.add_argument("--auth-code", metavar="CODE")
    
    args = parser.parse_args()
    
    if args.check:
        sys.exit(0 if check_auth() else 1)
    elif args.auth_url:
        get_auth_url()
    elif args.auth_code:
        exchange_code(args.auth_code)
