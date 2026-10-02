#!/usr/bin/env python3
"""Secure multi-user Video Scheduler with per-user Google credentials."""

import json
import os
import re
import secrets
import hashlib
import threading
import tempfile
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse, urlencode
from base64 import b64encode, b64decode

# Google APIs
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Configuration
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"

SESSIONS_FILE = DATA_DIR / "sessions.json"

# Session expiry (24 hours)
SESSION_EXPIRY_HOURS = 24

# Google OAuth scopes
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

# Job persistence (SQLite). Replaces the old in-memory `upload_jobs` dict so
# that queued and in-flight uploads survive a server restart.
from jobs_db import JobRepository, JobStatus

job_repo = JobRepository(str(DATA_DIR / "jobs.db"))

# Track OAuth flows (persistent file-based)
OAUTH_FLOWS_FILE = DATA_DIR / "oauth_flows.json"

def save_oauth_flow(state: str, user_id: str, redirect_uri: str, client_config: dict):
    """Save OAuth flow state to file."""
    flows = {}
    if OAUTH_FLOWS_FILE.exists():
        try:
            flows = json.loads(OAUTH_FLOWS_FILE.read_text())
        except:
            flows = {}
    flows[state] = {
        "user_id": user_id,
        "redirect_uri": redirect_uri,
        "client_config": client_config,
        "created": datetime.now().isoformat()
    }
    OAUTH_FLOWS_FILE.write_text(json.dumps(flows, indent=2))

def get_oauth_flow(state: str) -> dict | None:
    """Get and remove OAuth flow state."""
    if not OAUTH_FLOWS_FILE.exists():
        return None
    try:
        flows = json.loads(OAUTH_FLOWS_FILE.read_text())
        flow_data = flows.pop(state, None)
        OAUTH_FLOWS_FILE.write_text(json.dumps(flows, indent=2))
        return flow_data
    except:
        return None

# Video MIME types
VIDEO_MIME_TYPES = [
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/webm",
    "video/x-matroska", "video/mpeg", "video/3gpp", "video/x-flv",
]


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Hash password with salt."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return b64encode(hashed).decode(), salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    """Verify password against hash."""
    new_hash, _ = hash_password(password, salt)
    return new_hash == hashed


# ---------------------------------------------------------------------
# Encrypted user store helpers (uses CredentialManager from security.py)
# ---------------------------------------------------------------------
from security import CredentialManager, SensitiveDataFilter, setup_secure_logging

_cred_manager = CredentialManager()

def _load_plain() -> dict:
    """Read the raw JSON file *without* decryption.

    The on-disk store is a flat ``{email: user_record}`` map. A legacy file that
    wraps everything in a top-level ``"users"`` key is unwrapped here so both
    shapes keep working.
    """
    if not USERS_FILE.exists():
        return {}
    raw = json.loads(USERS_FILE.read_text() or "{}")
    if set(raw.keys()) == {"users"} and isinstance(raw["users"], dict):
        return raw["users"]
    return raw

def load_users() -> dict:
    """Load the encrypted users database as a flat ``{email: record}`` map.

    The JSON file contains encrypted token strings (base64). This function
    decrypts each token field before returning the in‑memory representation.
    """
    users = _load_plain()
    for uid, user in users.items():
        # Decrypt primary Google token – may be JSON‑encoded dict
        if isinstance(user.get("google_token"), str):
            try:
                decrypted = _cred_manager.decrypt(user["google_token"])
                try:
                    user["google_token"] = json.loads(decrypted)
                except Exception:
                    user["google_token"] = decrypted
            except Exception:
                user.pop("google_token", None)
        # Decrypt stored YouTube accounts (token may be dict JSON)
        for yt in user.get("youtube_accounts", []):
            if isinstance(yt.get("token"), str):
                try:
                    decrypted = _cred_manager.decrypt(yt["token"])
                    try:
                        yt["token"] = json.loads(decrypted)
                    except Exception:
                        yt["token"] = decrypted
                except Exception:
                    yt.pop("token", None)
        # Decrypt platform tokens (plain strings)
        for key in ("instagram_token", "facebook_token", "tiktok_token"):
            if isinstance(user.get(key), str):
                try:
                    user[key] = _cred_manager.decrypt(user[key])
                except Exception:
                    user.pop(key, None)
    return users

def save_users(users: dict):
    """Save the users database, encrypting any token fields.

    The function walks through the flat ``{email: record}`` map and encrypts any
    token value before writing the JSON file back to disk.
    """
    # Deep‑copy so we don’t mutate the caller’s dict
    to_save = json.loads(json.dumps(users))
    for uid, user in to_save.items():
        if isinstance(user.get("google_token"), dict):
            user["google_token"] = _cred_manager.encrypt(
                json.dumps(user["google_token"])
            )
        # YouTube accounts list
        for yt in user.get("youtube_accounts", []):
            if isinstance(yt.get("token"), dict):
                yt["token"] = _cred_manager.encrypt(json.dumps(yt["token"]))
        # Platform tokens (plain strings)
        for key in ("instagram_token", "facebook_token", "tiktok_token"):
            if isinstance(user.get(key), str):
                user[key] = _cred_manager.encrypt(user[key])
    USERS_FILE.write_text(json.dumps(to_save, indent=2))


def load_sessions() -> dict:
    """Load sessions database."""
    if SESSIONS_FILE.exists():
        return json.loads(SESSIONS_FILE.read_text())
    return {}


def save_sessions(sessions: dict):
    """Save sessions database."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def create_session(user_id: str) -> str:
    """Create a new session for user."""
    sessions = load_sessions()
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {
        "user_id": user_id,
        "created": datetime.now().isoformat(),
        "expires": (datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS)).isoformat()
    }
    save_sessions(sessions)
    return session_id


def get_session_user(session_id: str) -> str | None:
    """Get user ID from session, returns None if invalid/expired."""
    if not session_id:
        return None
    sessions = load_sessions()
    session = sessions.get(session_id)
    if not session:
        return None
    if datetime.fromisoformat(session["expires"]) < datetime.now():
        del sessions[session_id]
        save_sessions(sessions)
        return None
    return session["user_id"]


def delete_session(session_id: str):
    """Delete a session."""
    sessions = load_sessions()
    if session_id in sessions:
        del sessions[session_id]
        save_sessions(sessions)


def get_user_credentials(user_id: str) -> Credentials | None:
    """Get Google credentials for user."""
    users = load_users()
    user = users.get(user_id, {})
    token_data = user.get("google_token")
    if not token_data:
        return None
    
    creds = Credentials.from_authorized_user_info(token_data)
    
    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            user["google_token"] = json.loads(creds.to_json())
            save_users(users)
        except Exception as e:
            logging.error(f"Failed to refresh token: {e}")
            return None
    
    return creds


def get_user_drive_service(user_id: str):
    """Get Drive service for user."""
    creds = get_user_credentials(user_id)
    if not creds:
        return None
    return build("drive", "v3", credentials=creds)


def get_user_youtube_service(user_id: str, account_index: int = 0):
    """Get YouTube service for a specific linked account.

    ``account_index`` selects which stored YouTube token to use. Index 0 is the
    primary ``google_token`` (existing behavior). Additional accounts are stored
    under ``youtube_accounts`` as a list of credential dicts.
    """
    users = load_users()
    user = users.get(user_id, {})
    # Primary token fallback
    if account_index == 0:
        token_data = user.get("google_token")
    else:
        accounts = user.get("youtube_accounts", [])
        token_data = accounts[account_index - 1] if 0 < account_index <= len(accounts) else None
    if not token_data:
        return None
    creds = Credentials.from_authorized_user_info(token_data)
    # Refresh if needed
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            return None
    return build("youtube", "v3", credentials=creds)


SUPPORTED_PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")

def validate_platforms(platforms) -> tuple[list, list]:
    """Normalise and validate a requested platform list.

    Args:
        platforms: List of platform names from the request body.

    Returns:
        tuple: ``(normalised_lowercase_list, invalid_names)``. ``invalid_names``
        is empty when every entry is supported.
    """
    if platforms is None:
        return ["youtube"], []
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        return [], ["<platforms must be a list of strings>"]
    normalised = [p.strip().lower() for p in platforms if p.strip()]
    if not normalised:
        return ["youtube"], []
    invalid = sorted({p for p in normalised if p not in SUPPORTED_PLATFORMS})
    return normalised, invalid


def run_upload_job(job: dict):
    """Execute one upload job end‑to‑end, persisting progress to SQLite.

    The video is downloaded from Google Drive exactly once and then dispatched to
    every selected platform, each using its own credential.

    Args:
        job: Job mapping with ``job_id``, ``user_id``, ``drive_file_id``,
            ``title``, ``description``, ``platforms``, ``privacy`` and an
            optional ``youtube_account_id``.
    """
    from platform_upload import upload_to_platforms

    job_id = job["job_id"]
    user_id = job["user_id"]
    drive_file_id = job["drive_file_id"]
    title = job.get("title") or "Untitled"
    description = job.get("description") or ""
    platforms = job.get("platforms") or ["youtube"]
    privacy = job.get("privacy") or "private"
    temp_path = None
    try:
        # The job is normally already claimed (scheduler loop or the "upload now"
        # handler claims it atomically before spawning this worker). Re-claim
        # defensively so a direct call still marks the job as running; ``claim``
        # is a no-op when the row is not PENDING.
        job_repo.claim(job_id)

        # Download from Drive (task 2: Drive is the ready-videos source)
        drive = get_user_drive_service(user_id)
        if not drive:
            raise RuntimeError("Google Drive is not connected for this account")
        file_meta = drive.files().get(fileId=drive_file_id, fields="name,mimeType").execute()
        request = drive.files().get_media(fileId=drive_file_id)
        ext = Path(file_meta["name"]).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            downloader = MediaIoBaseDownload(tmp, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    job_repo.update(job_id, {"progress": int(status.progress() * 50)})
            temp_path = tmp.name

        job_repo.update(job_id, {"progress": 50})

        # Dispatch to each platform (task 1: per-platform credentials)
        def report_progress(pct: int) -> None:
            """Map YouTube chunk progress (0-100) onto the job's 50-100 range."""
            job_repo.update(job_id, {"progress": 50 + int(pct * 0.5)})

        results = upload_to_platforms(
            temp_path, title, description, platforms,
            user_id=user_id,
            youtube_account_id=job.get("youtube_account_id"),
            privacy=privacy,
            progress_cb=report_progress,
        )

        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
            temp_path = None

        job_repo.update(job_id, {
            "status": JobStatus.COMPLETED,
            "progress": 100,
            "result": results,
            "error": None,
        })
    except Exception as e:
        logging.error(f"Upload job {job_id} failed: {e}")
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        job_repo.update(job_id, {"status": JobStatus.FAILED, "error": str(e)})


def _job_to_schedule(job: dict) -> dict:
    """Map a job row onto the legacy schedule shape used by the frontend."""
    return {
        "id": job["job_id"],
        "jobId": job["job_id"],
        "videoId": job["drive_file_id"],
        "title": job["title"],
        "description": job["description"] or "",
        "privacy": job["privacy"] or "private",
        "platforms": job["platforms"],
        "scheduleTime": job["scheduled_time"],
        "status": job["status"],
        "progress": job["progress"],
        "error": job["error"],
        "result": job["result"],
    }


def scheduler_loop(interval: int = 30):
    """Background loop that runs due jobs and repairs crashed ones.

    Runs once at startup (resume) and then every ``interval`` seconds. Jobs left
    in ``PROCESSING`` by a crash are reset to ``PENDING`` so they are retried.

    Each due job is *claimed* with an atomic conditional UPDATE before its worker
    thread starts, so a job is never executed twice even if two ticks overlap or
    a manual "upload now" request races the loop.

    Args:
        interval: Seconds between polls.
    """
    while True:
        try:
            # Resume jobs that were mid-upload when the process died.
            for job in job_repo.get_running_jobs():
                logging.info(f"Resetting interrupted job {job['job_id']} to pending")
                job_repo.update(job["job_id"], {
                    "status": JobStatus.PENDING,
                    "progress": 0,
                })

            for job in job_repo.get_due_jobs():
                if job["status"] != JobStatus.PENDING:
                    continue
                # Claim before spawning: the loser of a race gets False and
                # skips the job entirely.
                if not job_repo.claim(job["job_id"]):
                    continue
                logging.info(f"Scheduler claimed job {job['job_id']}")
                threading.Thread(
                    target=run_upload_job, args=(job,), daemon=True
                ).start()
        except Exception as e:
            logging.error(f"Scheduler loop error: {e}")
        time.sleep(interval)


def resume_pending_jobs():
    """Report pending/due jobs at startup and start the scheduler thread."""
    pending = job_repo.get_pending_jobs()
    logging.info(f"Found {len(pending)} pending job(s) on startup")
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    return thread


# Maximum accepted request body, generous enough for a Drive id + long
# description but small enough to reject a runaway upload.
MAX_BODY_BYTES = 256 * 1024

# Field limits enforced by :func:`validate_upload_payload`.
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000


# ---------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------
# Each entry is ``(methods, compiled_regex_or_None, handler_name)``. An exact
# path uses ``None`` for the pattern; a parameterised path uses a regex whose
# groups become the handler's path parameters. Regexes (rather than the plain
# dict from the plan) are required to express paths such as
# /api/v1/jobs/{job_id}/cancel.
def _route(methods, path, handler):
    """Build one route entry from a path template.

    Args:
        methods: Tuple of allowed HTTP methods.
        path: Literal path (e.g. ``/api/v1/jobs``) or a regex containing named
            groups for parameters (e.g. ``/api/v1/jobs/(?P<job_id>[^/]+)``).
        handler: Name of the handler method on the request handler class.

    Returns:
        tuple: ``(methods, compiled_regex, handler)``.
    """
    # A literal path is matched exactly; a regex may contain {param} markers.
    pattern = path if "(?P<" in path else re.escape(path)
    return (methods, re.compile("^" + pattern + "/?$"), handler)


# New v1 routes. Tried first for every request.
V1_ROUTES = [
    _route(("GET",), "/api/v1/auth/status", "handle_auth_status"),
    _route(("POST",), "/api/v1/auth/register", "handle_register"),
    _route(("POST",), "/api/v1/auth/login", "handle_login"),
    _route(("POST",), "/api/v1/auth/logout", "handle_logout"),
    _route(("POST",), "/api/v1/auth/setup-google", "handle_setup_google"),
    _route(("POST",), "/api/v1/auth/start-oauth", "handle_start_oauth"),
    _route(("GET",), "/api/v1/status", "handle_status"),
    _route(("GET",), "/api/v1/accounts", "handle_account_route"),
    _route(("GET",), r"/api/v1/accounts/(?P<account_id>[^/]+)", "handle_account_route"),
    _route(("DELETE",), r"/api/v1/accounts/(?P<account_id>[^/]+)", "handle_account_route"),
    _route(("GET",), "/api/v1/jobs", "handle_list_jobs"),
    _route(("GET",), r"/api/v1/jobs/(?P<job_id>[^/]+)", "handle_get_job"),
    _route(("POST",), r"/api/v1/jobs/(?P<job_id>[^/]+)/cancel", "handle_cancel_job"),
    _route(("POST",), r"/api/v1/jobs/(?P<job_id>[^/]+)/start", "handle_start_job"),
    _route(("POST",), "/api/v1/upload", "handle_create_upload"),
    _route(("GET",), "/api/v1/drive/videos", "handle_videos"),
    _route(("GET",), "/api/v1/drive/folders", "handle_folders"),
    _route(("GET",), "/api/v1/youtube/channel", "handle_youtube_channel"),
]

# Legacy routes kept for backward compatibility; every response carries
# deprecation headers pointing at its /api/v1/ equivalent.
LEGACY_ROUTES = [
    _route(("GET",), "/api/auth/status", "handle_auth_status"),
    _route(("POST",), "/api/auth/register", "handle_register"),
    _route(("POST",), "/api/auth/login", "handle_login"),
    _route(("POST",), "/api/auth/logout", "handle_logout"),
    _route(("POST",), "/api/auth/setup-google", "handle_setup_google"),
    _route(("POST",), "/api/auth/start-oauth", "handle_start_oauth"),
    _route(("POST",), "/api/auth/add-youtube-account", "handle_add_youtube_account"),
    _route(("GET",), "/api/status", "handle_status"),
    _route(("GET",), "/api/videos", "handle_videos"),
    _route(("GET",), "/api/folders", "handle_folders"),
    _route(("GET",), "/api/youtube/channel", "handle_youtube_channel"),
    _route(("GET",), "/api/schedules", "handle_get_schedules"),
    _route(("GET",), "/api/upload/status", "handle_upload_status"),
    _route(("GET",), "/api/jobs", "handle_list_jobs_legacy"),
    _route(("GET",), "/api/v1/accounts", "handle_account_route"),
    _route(("GET",), r"/api/v1/accounts/(?P<account_id>[^/]+)", "handle_account_route"),
    _route(("DELETE",), r"/api/v1/accounts/(?P<account_id>[^/]+)", "handle_account_route"),
    _route(("POST",), "/api/youtube/upload", "handle_youtube_upload"),
    _route(("POST",), "/api/schedule", "handle_schedule"),
    _route(("POST",), "/api/schedule/delete", "handle_delete_schedule"),
    _route(("POST",), "/api/schedule/upload", "handle_upload_scheduled"),
]

# Explicit legacy path -> v1 replacement used for the deprecation headers.
LEGACY_REPLACEMENTS = {
    "/api/auth/status": "/api/v1/auth/status",
    "/api/auth/register": "/api/v1/auth/register",
    "/api/auth/login": "/api/v1/auth/login",
    "/api/auth/logout": "/api/v1/auth/logout",
    "/api/status": "/api/v1/status",
    "/api/videos": "/api/v1/drive/videos",
    "/api/folders": "/api/v1/drive/folders",
    "/api/schedules": "/api/v1/jobs",
    "/api/jobs": "/api/v1/jobs",
    "/api/youtube/upload": "/api/v1/upload",
    "/api/schedule": "/api/v1/upload",
    "/api/schedule/delete": "/api/v1/jobs/{job_id}/cancel",
    "/api/schedule/upload": "/api/v1/jobs/{job_id}/start",
    "/api/upload/status": "/api/v1/jobs/{job_id}",
}


class HTTPStatus:
    """HTTP status codes used across the API.

    Defined locally so the module does not depend on ``http.HTTPStatus`` being
    imported under that name, and so the intent of each code is obvious at the
    call site.
    """

    OK = 200
    CREATED = 201
    ACCEPTED = 202
    NO_CONTENT = 204
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    CONFLICT = 409
    PAYLOAD_TOO_LARGE = 413
    UNSUPPORTED_MEDIA_TYPE = 415
    UNPROCESSABLE = 422
    TOO_MANY_REQUESTS = 429
    INTERNAL_SERVER_ERROR = 500


def success_response(data=None, status_code=HTTPStatus.OK):
    """Build a standard success envelope.

    Args:
        data: JSON-serialisable payload, or ``None`` for an empty response.
        status_code: HTTP status to return (defaults to ``200``).

    Returns:
        tuple: ``(body_dict, status_code)`` ready for
        :meth:`SecureDashboardHandler.send_json_response`.
    """
    return {"success": True, "data": data}, status_code


def error_response(message, code=None, details=None, status_code=HTTPStatus.BAD_REQUEST):
    """Build a standard error envelope.

    Args:
        message: Human-readable description of the failure.
        code: Optional stable machine-readable code; defaults to the status.
        details: Optional dict/list with per-field information.
        status_code: HTTP status to return (defaults to ``400``).

    Returns:
        tuple: ``(body_dict, status_code)``.
    """
    return {
        "success": False,
        "error": {
            "message": message,
            "code": code or status_code,
            "details": details,
        },
    }, status_code


def accepted_response(job_id, location_path, status="queued"):
    """Build a ``202 Accepted`` body plus its ``Location`` header.

    Args:
        job_id: Identifier of the newly created job.
        location_path: URI where the caller can poll for the job's state.
        status: Initial job status to report (defaults to ``queued``).

    Returns:
        tuple: ``(body_dict, HTTPStatus.ACCEPTED, {"Location": location_path})``.
    """
    return (
        {
            "success": True,
            "data": {"job_id": job_id, "status": status},
        },
        HTTPStatus.ACCEPTED,
        {"Location": location_path},
    )


def validate_upload_payload(payload):
    """Validate a ``POST /api/v1/upload`` body.

    Args:
        payload: Decoded JSON body expected to be a dict.

    Returns:
        tuple: ``(normalised_dict, error_message_or_None)``. When the error is
        ``None`` the returned dict holds cleaned values safe to persist.
    """
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object"

    # Accept both the v1 snake_case fields and the camelCase names the existing
    # frontend still sends, so the legacy UI keeps working against v1.
    drive_file_id = payload.get("drive_file_id") or payload.get("driveFileId")
    title = payload.get("title")
    platforms_raw = payload.get("platforms")
    description = payload.get("description") or ""
    privacy = payload.get("privacy") or "private"
    scheduled_time = payload.get("scheduled_time") or payload.get("scheduleTime")
    youtube_account_id = payload.get("youtube_account_id") or payload.get(
        "youtubeAccountId"
    )

    missing = [
        name
        for name, value in (
            ("drive_file_id", drive_file_id),
            ("title", title),
            ("platforms", platforms_raw),
        )
        if not value
    ]
    if missing:
        return None, f"Missing required field(s): {', '.join(missing)}"

    if not isinstance(drive_file_id, str) or not drive_file_id.strip():
        return None, "drive_file_id must be a non-empty string"
    if not isinstance(title, str) or not title.strip():
        return None, "title must be a non-empty string"
    if len(title) > MAX_TITLE_LEN:
        return None, f"title too long (max {MAX_TITLE_LEN} characters)"
    if not isinstance(description, str):
        return None, "description must be a string"
    if len(description) > MAX_DESCRIPTION_LEN:
        return None, f"description too long (max {MAX_DESCRIPTION_LEN} characters)"
    if privacy not in ("public", "unlisted", "private"):
        return None, f"privacy must be public, unlisted or private (got '{privacy}')"

    platforms, invalid = validate_platforms(platforms_raw)
    if invalid:
        return None, (
            f"Unsupported platform(s): {', '.join(invalid)}. "
            f"Supported: {', '.join(SUPPORTED_PLATFORMS)}"
        )

    # A scheduled_time that cannot be parsed would make the job permanently due
    # (NULL sorts as "run now"), so reject it up front.
    if scheduled_time:
        from jobs_db import _parse_dt

        if _parse_dt(scheduled_time) is None:
            return None, (
                f"scheduled_time must be an ISO-8601 datetime (got '{scheduled_time}')"
            )

    return (
        {
            "drive_file_id": drive_file_id.strip(),
            "title": title.strip(),
            "description": description,
            "platforms": platforms,
            "privacy": privacy,
            "scheduled_time": scheduled_time,
            "youtube_account_id": youtube_account_id,
        },
        None,
    )


class SecureDashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler with authentication."""
    
    def __init__(self, *args, **kwargs):
        self.directory = str(Path(__file__).parent)
        super().__init__(*args, directory=self.directory, **kwargs)
    
    def get_session_id(self) -> str | None:
        """Extract session ID from cookie."""
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        if "session" in cookie:
            return cookie["session"].value
        return None
    
    def get_current_user(self) -> tuple[str | None, dict | None]:
        """Get current user from session."""
        session_id = self.get_session_id()
        user_id = get_session_user(session_id)
        if not user_id:
            return None, None
        users = load_users()
        return user_id, users.get(user_id)
    
    def resolve_route(self, method: str):
        """Match the request path against the route tables.

        v1 routes take precedence; a legacy match is allowed but flagged so the
        response carries deprecation headers.

        Args:
            method: HTTP method of the incoming request.

        Returns:
            tuple: ``(handler_method_or_None, path_params, is_legacy, allow)``.
            ``allow`` is the comma-joined list of methods allowed on a matched
            path (empty when no route matched at all).
        """
        parsed = urlparse(self.path)
        path = parsed.path
        self.route_params = {}

        matched_path = False
        for table, is_legacy in ((V1_ROUTES, False), (LEGACY_ROUTES, True)):
            for methods, pattern, handler_name in table:
                m = pattern.match(path)
                if not m:
                    continue
                matched_path = True
                if method not in methods:
                    continue
                self.route_params = m.groupdict()
                handler = getattr(self, handler_name, None)
                if handler is None:
                    logging.error(f"Route {path} -> unknown handler {handler_name}")
                    return None, {}, is_legacy, ""
                return handler, self.route_params, is_legacy, ",".join(methods)

        if matched_path:
            # Path exists but not for this verb -> 405.
            allowed = set()
            for table in (V1_ROUTES, LEGACY_ROUTES):
                for methods, pattern, _ in table:
                    if pattern.match(path):
                        allowed.update(methods)
            return None, {}, False, ",".join(sorted(allowed))
        return None, {}, False, ""

    def deprecation_headers(self, path: str) -> dict:
        """Build the deprecation headers for a legacy route response.

        Args:
            path: The legacy request path.

        Returns:
            dict: Headers to merge into the response (empty when no mapping).
        """
        replacement = LEGACY_REPLACEMENTS.get(path)
        if not replacement:
            return {}
        logging.warning(f"Legacy route used: {path} -> migrate to {replacement}")
        return {
            "Deprecation": "true",
            "X-API-Deprecated": "true",
            "X-API-Migrate-To": replacement,
            "Link": f'<{replacement}>; rel="successor-version"',
        }

    def send_json_response(self, data: dict, status_code: int = 200,
                           headers: dict | None = None):
        """Send a JSON response with optional extra headers.

        Args:
            data: Body to serialise.
            status_code: HTTP status code.
            headers: Optional mapping of additional response headers.
        """
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        # Merge any headers staged by the dispatcher (deprecation) or by a
        # handler (e.g. Connection: close on an oversized body).
        merged = dict(getattr(self, "_pending_headers", None) or {})
        if headers:
            merged.update(headers)
        for key, value in merged.items():
            self.send_header(key, value)
        self.end_headers()
        payload = json.dumps(data, default=str).encode()
        self.wfile.write(payload)

    def drain_body(self, length: int, chunk: int = 65536, cap: int = 1024 * 1024):
        """Read and discard part of a request body we are about to reject.

        Only useful for *small* overruns: draining a large body while the client
        is still writing it can make the peer observe a TCP reset instead of
        our error response. For genuinely oversized bodies the server closes the
        connection instead (see :meth:`get_json_body`).

        Args:
            length: Number of bytes the client announced via Content-Length.
            chunk: Read size per iteration.
            cap: Hard limit on bytes actually read.
        """
        remaining = min(length, cap)
        try:
            while remaining > 0:
                data = self.rfile.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
        except (OSError, ValueError):
            # Client already gone; nothing more we can do.
            pass

    def get_json_body(self):
        """Read and decode the JSON request body.

        Returns:
            tuple: ``(payload, error_response_tuple_or_None)``. On success the
            error slot is ``None``; otherwise it holds an
            :func:`error_response` result ready for
            :meth:`send_json_response`.
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None, error_response(
                "Invalid Content-Length header",
                status_code=HTTPStatus.BAD_REQUEST,
            )
        if length <= 0:
            return None, error_response(
                "Request body is required", status_code=HTTPStatus.BAD_REQUEST
            )
        if length > MAX_BODY_BYTES:
            # Handled in _preflight_body_check before routing; keep this guard
            # so the helper is safe to call directly.
            self.drain_body(length)
            self.close_connection = True
            self._pending_headers = dict(
                getattr(self, "_pending_headers", None) or {}
            )
            self._pending_headers["Connection"] = "close"
            return None, error_response(
                f"Request body exceeds {MAX_BODY_BYTES} bytes",
                status_code=HTTPStatus.PAYLOAD_TOO_LARGE,
            )
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type not in ("application/json", ""):
            return None, error_response(
                "Content-Type must be application/json",
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        try:
            return json.loads(raw.decode()), None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, error_response(
                f"Invalid JSON body: {e}", status_code=HTTPStatus.BAD_REQUEST
            )

    def _preflight_body_check(self):
        """Reject an oversized request body *before* any handler runs.

    Handlers answer auth/validation failures without reading the body, which
    would leave the announced bytes unread in the socket and make the kernel
    send an RST — the client then sees a connection reset instead of our error.
    Checking the size up front lets us drain (or close) deterministically.

    Returns:
        bool: ``True`` when the request has been answered and must stop.
    """
        # Only POST/PUT/PATCH carry a body worth checking.
        if self.command not in ("POST", "PUT", "PATCH"):
            return False
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self.send_json_response(
                *error_response(
                    "Invalid Content-Length header",
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            )
            return True
        if length <= MAX_BODY_BYTES:
            return False

        # Drain what is already buffered, then close. Reading only up to the cap
        # is deliberate: draining unbounded data lets a client stall the server.
        self.drain_body(length)
        self.close_connection = True
        self._pending_headers = dict(getattr(self, "_pending_headers", None) or {})
        self._pending_headers["Connection"] = "close"
        self.send_json_response(
            *error_response(
                f"Request body exceeds {MAX_BODY_BYTES} bytes",
                status_code=HTTPStatus.PAYLOAD_TOO_LARGE,
            )
        )
        return True

    def _dispatch(self):
        """Route the current request, or emit the appropriate 4xx/5xx."""
        if self._preflight_body_check():
            return
        handler, params, is_legacy, allow = self.resolve_route(self.command)
        self.route_params = params
        if handler is None:
            if allow:
                # Path exists but not for this HTTP method.
                self.send_json_response(
                    *error_response(
                        f"Method {self.command} not allowed for this path",
                        code=405,
                        details={"allow": allow},
                        status_code=405,
                    ),
                    headers={"Allow": allow},
                )
                return
            self.send_json_response(
                *error_response(
                    "Endpoint not found",
                    details={"path": urlparse(self.path).path},
                    status_code=HTTPStatus.NOT_FOUND,
                )
            )
            return
        headers = (
            self.deprecation_headers(urlparse(self.path).path) if is_legacy else {}
        )
        self._pending_headers = headers
        try:
            handler()
        except Exception as e:  # last-resort guard so one bad route can't kill us
            logging.exception(f"Unhandled error in {handler.__name__}: {e}")
            self.send_json_response(
                *error_response(
                    "Internal server error",
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/login.html"):
            self.serve_index()
            return
        if parsed.path == "/oauth/callback":
            self.handle_oauth_callback(parsed)
            return
        self._dispatch()

    def do_POST(self):
        """Handle POST requests."""
        self._dispatch()

    def do_DELETE(self):
        """Handle DELETE requests."""
        self._dispatch()
    
    def send_json(self, data: dict, status: int = 200, headers: dict | None = None):
        """Send a JSON response, merging in any pending deprecation headers.

        This is the legacy-shaped entry point still used by the pre-v1 handlers;
        it delegates to :meth:`send_json_response`, which merges the staged
        headers on its own.
        """
        self.send_json_response(data, status, headers)

    def set_session_cookie(self, session_id: str):
        """Set session cookie."""
        self.send_header("Set-Cookie", f"session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_EXPIRY_HOURS * 3600}")
    
    def clear_session_cookie(self):
        """Clear session cookie."""
        self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; Max-Age=0")

    # ---------------------------------------------------------------------
    # Versioned account management (list, retrieve, delete)
    # ---------------------------------------------------------------------
    def handle_account_route(self, parsed=None):
        """Handle /api/v1/accounts* endpoints.

        GET  /api/v1/accounts               – list accounts
        GET  /api/v1/accounts/<account_id>  – get account details
        DELETE /api/v1/accounts/<account_id> – delete account
        """
        user_id, user = self.get_current_user()
        if not user_id:
            self.send_json_response(
                *error_response(
                    "Authentication required", status_code=HTTPStatus.UNAUTHORIZED
                )
            )
            return
        user = user or {}
        accounts = user.get("youtube_accounts", [])
        account_id = (getattr(self, "route_params", None) or {}).get("account_id")

        # /api/v1/accounts – list all
        if not account_id:
            self.send_json_response(*success_response({
                "accounts": [
                    {"account_id": a.get("account_id"), "email": a.get("email")}
                    for a in accounts
                ],
                "total": len(accounts),
            }))
            return

        # /api/v1/accounts/<account_id>
        match = next(
            (a for a in accounts if a.get("account_id") == account_id), None
        )
        if not match:
            self.send_json_response(
                *error_response(
                    "Account not found", status_code=HTTPStatus.NOT_FOUND
                )
            )
            return
        if self.command == "GET":
            # Never echo the stored token back to the client.
            safe = {k: v for k, v in match.items() if k != "token"}
            self.send_json_response(*success_response({"account": safe}))
            return
        if self.command == "DELETE":
            accounts.remove(match)
            users = load_users()
            users[user_id]["youtube_accounts"] = accounts
            save_users(users)
            self.send_json_response(*success_response({
                "account_id": account_id,
                "message": "Account deleted",
            }))
            return
        self.send_json_response(
            *error_response(
                "Method not allowed for this path",
                code=405,
                details={"allow": "GET, DELETE"},
                status_code=405,
            ),
            headers={"Allow": "GET, DELETE"},
        )

    
    def serve_index(self):
        """Serve index.html or login page based on auth status."""
        user_id, user = self.get_current_user()
        
        # Serve the appropriate page
        if user_id and user and user.get("google_token"):
            # User is logged in and has Google connected
            self.path = "/index.html"
        else:
            # Show login/setup page
            self.path = "/login.html"
        
        super().do_GET()
    
    def handle_auth_status(self):
        """Return current auth status."""
        user_id, user = self.get_current_user()
        
        if not user_id:
            self.send_json({
                "authenticated": False,
                "google_connected": False
            })
            return
        
        google_connected = bool(user and user.get("google_token"))
        has_credentials = bool(user and user.get("google_client_id"))
        
        self.send_json({
            "authenticated": True,
            "user_id": user_id,
            "email": user.get("email") if user else None,
            "google_connected": google_connected,
            "has_credentials": has_credentials
        })
    
    def handle_register(self):
        """Register new user."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        
        if not email or not password:
            self.send_json({"success": False, "error": "Email and password required"}, 400)
            return
        
        if len(password) < 8:
            self.send_json({"success": False, "error": "Password must be at least 8 characters"}, 400)
            return
        
        users = load_users()
        
        if email in users:
            self.send_json({"success": False, "error": "Email already registered"}, 400)
            return
        
        hashed, salt = hash_password(password)
        users[email] = {
            "email": email,
            "password_hash": hashed,
            "password_salt": salt,
            "created": datetime.now().isoformat()
        }
        save_users(users)
        
        # Auto-login
        session_id = create_session(email)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.set_session_cookie(session_id)
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "message": "Registration successful"}).encode())
    
    def handle_login(self):
        """Login user."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        
        users = load_users()
        user = users.get(email)
        
        if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
            self.send_json({"success": False, "error": "Invalid email or password"}, 401)
            return
        
        session_id = create_session(email)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.set_session_cookie(session_id)
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "message": "Login successful"}).encode())
    
    def handle_logout(self):
        """Logout user."""
        session_id = self.get_session_id()
        if session_id:
            delete_session(session_id)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.clear_session_cookie()
        self.end_headers()
        self.wfile.write(json.dumps({"success": True}).encode())
    
    def handle_setup_google(self):
        """Save user's Google OAuth credentials."""
        user_id, user = self.get_current_user()
        if not user_id:
            self.send_json({"success": False, "error": "Not authenticated"}, 401)
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        client_id = body.get("client_id", "").strip()
        client_secret = body.get("client_secret", "").strip()
        
        if not client_id or not client_secret:
            self.send_json({"success": False, "error": "Client ID and secret required"}, 400)
            return
        
        users = load_users()
        users[user_id]["google_client_id"] = client_id
        users[user_id]["google_client_secret"] = client_secret
        save_users(users)
        
        self.send_json({"success": True, "message": "Google credentials saved"})
    
    def handle_start_oauth(self):
        """Start OAuth flow - returns auth URL."""
        user_id, user = self.get_current_user()
        if not user_id:
            self.send_json({"success": False, "error": "Not authenticated"}, 401)
            return
        
        if not user.get("google_client_id"):
            self.send_json({"success": False, "error": "Google credentials not configured"}, 400)
            return
        
        # Get the host from request
        host = self.headers.get("Host", "localhost:8765")
        protocol = "https" if "railway" in host or "render" in host or "herokuapp" in host else "http"
        redirect_uri = f"{protocol}://{host}/oauth/callback"
        
        # Generate state token (includes user_id for callback)
        state = b64encode(json.dumps({"user_id": user_id, "nonce": secrets.token_urlsafe(16)}).encode()).decode()
        
        # Build auth URL manually (NO PKCE)
        import urllib.parse
        auth_params = {
            "client_id": user["google_client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state
        }
        auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(auth_params)
        
        # Store flow data for callback (file-based, survives restarts)
        client_config = {
            "web": {
                "client_id": user["google_client_id"],
                "client_secret": user["google_client_secret"],
            }
        }
        save_oauth_flow(state, user_id, redirect_uri, client_config)
        
        self.send_json({"success": True, "auth_url": auth_url})
    
    def handle_add_youtube_account(self):
        """Begin OAuth to link an *additional* YouTube channel.

        Serves ``POST /api/auth/add-youtube-account``. Identical to the normal
        OAuth start except the state payload carries ``add_account: True``, so
        the callback stores the result under ``youtube_accounts`` with a stable
        UUID instead of replacing the primary ``google_token``.
        """
        user_id, user = self.get_current_user()
        if not user_id or not user:
            self.send_json_response(
                *error_response(
                    "Authentication required", status_code=HTTPStatus.UNAUTHORIZED
                )
            )
            return
        if not user.get("google_client_id") or not user.get("google_client_secret"):
            self.send_json_response(
                *error_response(
                    "Google credentials are not configured for this account",
                    code="google_not_configured",
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            )
            return

        host = self.headers.get("Host", "localhost:8765")
        protocol = (
            "https"
            if any(d in host for d in ("railway", "render", "herokuapp"))
            else "http"
        )
        redirect_uri = f"{protocol}://{host}/oauth/callback"

        state = b64encode(
            json.dumps(
                {
                    "user_id": user_id,
                    "nonce": secrets.token_urlsafe(16),
                    "add_account": True,
                }
            ).encode()
        ).decode()

        import urllib.parse

        auth_params = {
            "client_id": user["google_client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            # An existing channel is already authorised; without this Google
            # silently reuses the first account instead of letting the user
            # pick a different one.
            "prompt": "consent select_account",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "state": state,
        }
        auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(
            auth_params
        )

        save_oauth_flow(
            state,
            user_id,
            redirect_uri,
            {
                "web": {
                    "client_id": user["google_client_id"],
                    "client_secret": user["google_client_secret"],
                }
            },
        )
        self.send_json_response(*success_response({"auth_url": auth_url}))

    def handle_oauth_callback(self, parsed):
        """Handle OAuth callback from Google."""
        params = parse_qs(parsed.query)
        
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        
        if error:
            self.send_response(302)
            self.send_header("Location", f"/?error={error}")
            self.end_headers()
            return
        
        if not code or not state:
            self.send_response(302)
            self.send_header("Location", "/?error=missing_params")
            self.end_headers()
            return
        
        try:
            # Get stored flow data
            flow_data = get_oauth_flow(state)
            
            if flow_data:
                user_id = flow_data["user_id"]
                redirect_uri = flow_data["redirect_uri"]
                client_config = flow_data["client_config"]
            else:
                # Fallback: decode state and get user config
                state_data = json.loads(b64decode(state).decode())
                user_id = state_data.get("user_id")
                
                users = load_users()
                user = users.get(user_id)
                
                if not user:
                    raise Exception("User not found")
                
                host = self.headers.get("Host", "localhost:8765")
                protocol = "https" if "railway" in host or "render" in host or "herokuapp" in host else "http"
                redirect_uri = f"{protocol}://{host}/oauth/callback"
                
                client_config = {
                    "web": {
                        "client_id": user["google_client_id"],
                        "client_secret": user["google_client_secret"],
                    }
                }
            
            # Exchange code for tokens using direct HTTP (no PKCE needed)
            import urllib.request
            import urllib.parse
            
            token_data = urllib.parse.urlencode({
                "code": code,
                "client_id": client_config["web"]["client_id"],
                "client_secret": client_config["web"]["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code"
            }).encode()
            
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            with urllib.request.urlopen(req) as response:
                token_response = json.loads(response.read().decode())
            
            # Build credentials dict
            creds_data = {
                "token": token_response["access_token"],
                "refresh_token": token_response.get("refresh_token"),
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": client_config["web"]["client_id"],
                "client_secret": client_config["web"]["client_secret"],
                "scopes": SCOPES
            }
            
            # Save token
            # Save token – handle primary account vs additional YouTube accounts
            users = load_users()
            # Determine if this OAuth flow was for an additional YouTube account
            is_additional = False
            # Prefer flow_data if present; otherwise inspect the state payload
            if flow_data and flow_data.get("client_config", {}).get("add_account"):
                is_additional = True
            else:
                try:
                    state_payload = json.loads(b64decode(state).decode())
                    is_additional = bool(state_payload.get("add_account"))
                except Exception:
                    is_additional = False
            if is_additional:
                accounts = users[user_id].setdefault("youtube_accounts", [])
                # Attach a stable UUID for the new account
                from security import generate_account_id
                creds_data["account_id"] = generate_account_id()
                accounts.append(creds_data)
            else:
                users[user_id]["google_token"] = creds_data
            save_users(users)
            
            # Create new session and redirect
            session_id = create_session(user_id)
            
            self.send_response(302)
            self.set_session_cookie(session_id)
            self.send_header("Location", "/?connected=true")
            self.end_headers()
            
        except Exception as e:
            logging.error(f"OAuth callback error: {e}")
            self.send_response(302)
            self.send_header("Location", f"/?error=oauth_failed")
            self.end_headers()
    
    def require_auth(self) -> tuple[str | None, dict | None]:
        """Check auth and return the user, or send a 401 and return ``(None, None)``.

        Returns:
            tuple: ``(user_id, user_record)`` on success, ``(None, None)`` after
            a 401/403 response has already been written.
        """
        user_id, user = self.get_current_user()
        if not user_id or not user:
            self.send_json_response(
                *error_response(
                    "Authentication required",
                    status_code=HTTPStatus.UNAUTHORIZED,
                )
            )
            return None, None
        if not user.get("google_token"):
            self.send_json_response(
                *error_response(
                    "Google account is not connected; complete OAuth first",
                    code="google_not_connected",
                    status_code=HTTPStatus.UNAUTHORIZED,
                )
            )
            return None, None
        return user_id, user
    
    def handle_status(self):
        """Get connection status."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            # Get user info from Google
            creds = get_user_credentials(user_id)
            service = build("oauth2", "v2", credentials=creds)
            user_info = service.userinfo().get().execute()
            
            # Check YouTube channel
            youtube = get_user_youtube_service(user_id)
            channel_response = youtube.channels().list(part="snippet", mine=True).execute()
            channel = channel_response.get("items", [{}])[0].get("snippet", {}) if channel_response.get("items") else None
            
            self.send_json({
                "success": True,
                "connected": True,
                "user": user_info.get("email"),
                "name": user_info.get("name"),
                "picture": user_info.get("picture"),
                "youtube_channel": channel.get("title") if channel else None
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_folders(self):
        """List Drive folders."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            service = get_user_drive_service(user_id)
            results = service.files().list(
                q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                pageSize=50,
                fields="files(id, name)"
            ).execute()
            
            self.send_json({
                "success": True,
                "folders": results.get("files", [])
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_videos(self, parsed=None):
        """List videos from Drive.

        Serves ``GET /api/v1/drive/videos`` and the legacy ``GET /api/videos``.
        """
        parsed = parsed or urlparse(self.path)
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            params = parse_qs(parsed.query)
            folder_id = params.get("folder", [None])[0]
            
            service = get_user_drive_service(user_id)
            
            mime_query = " or ".join([f"mimeType='{m}'" for m in VIDEO_MIME_TYPES])
            query = f"({mime_query}) and trashed=false"
            if folder_id:
                query += f" and '{folder_id}' in parents"
            
            results = service.files().list(
                q=query,
                pageSize=100,
                fields="files(id, name, size, mimeType, thumbnailLink, webViewLink, createdTime)"
            ).execute()
            
            videos = []
            for f in results.get("files", []):
                videos.append({
                    "id": f["id"],
                    "title": f["name"],
                    "size": self.format_size(int(f.get("size", 0))),
                    "sizeBytes": int(f.get("size", 0)),
                    "mimeType": f.get("mimeType"),
                    "thumbnail": f.get("thumbnailLink"),
                    "webViewLink": f.get("webViewLink"),
                    "downloadLink": f"https://drive.google.com/uc?id={f['id']}&export=download",
                    "createdTime": f.get("createdTime")
                })
            
            self.send_json({"success": True, "videos": videos})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
    
    def handle_youtube_channel(self):
        """Get YouTube channel info."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        try:
            youtube = get_user_youtube_service(user_id)
            response = youtube.channels().list(
                part="snippet,statistics",
                mine=True
            ).execute()
            
            if not response.get("items"):
                self.send_json({"success": False, "error": "No YouTube channel found"})
                return
            
            channel = response["items"][0]
            self.send_json({
                "success": True,
                "channel": {
                    "id": channel["id"],
                    "title": channel["snippet"]["title"],
                    "thumbnail": channel["snippet"]["thumbnails"]["default"]["url"],
                    "subscriberCount": channel["statistics"].get("subscriberCount", 0)
                }
            })
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, 500)
    
    def handle_youtube_upload(self):
        """Upload video to YouTube."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        drive_file_id = body.get("driveFileId")
        title = body.get("title", "Untitled")
        description = body.get("description", "")
        privacy = body.get("privacy", "private")
        
        if not drive_file_id:
            self.send_json({"success": False, "error": "Drive file ID required"}, 400)
            return
        
        platforms, invalid = validate_platforms(body.get("platforms"))
        if invalid:
            self.send_json({
                "success": False,
                "error": f"Unsupported platform(s): {', '.join(invalid)}",
                "supported": list(SUPPORTED_PLATFORMS),
            }, 400)
            return
        
        # Create persistent job record
        job_id = job_repo.create({
            "user_id": user_id,
            "drive_file_id": drive_file_id,
            "title": title,
            "description": description,
            "platforms": platforms,
            "privacy": privacy,
            "status": JobStatus.PENDING,
        })
        
        # Claim immediately so a fast scheduler tick cannot start the same job
        # a second time while this request is still returning.
        job_repo.claim(job_id)
        
        # Start upload in background
        thread = threading.Thread(
            target=self.upload_video_async,
            args=(job_id, user_id, drive_file_id, title, description, privacy,
                  platforms, body.get("youtubeAccountId"))
        )
        thread.daemon = True
        thread.start()
        
        self.send_json({"success": True, "jobId": job_id})
    
    def upload_video_async(self, job_id: str, user_id: str, drive_file_id: str, title: str,
                            description: str, privacy: str, platforms: list | None = None,
                            youtube_account_id: str | None = None):
        """Background upload task, persisting progress in the jobs database.

        Thin wrapper around :func:`run_upload_job` so request handlers and the
        scheduler thread share exactly the same upload path.
        """
        run_upload_job(
            {
                "job_id": job_id,
                "user_id": user_id,
                "drive_file_id": drive_file_id,
                "title": title,
                "description": description,
                "platforms": platforms or ["youtube"],
                "youtube_account_id": youtube_account_id,
                "privacy": privacy,
            }
        )
    
    def handle_upload_status(self, parsed=None):
        """Get upload job status (legacy ``GET /api/upload/status?jobId=...``)."""
        parsed = parsed or urlparse(self.path)
        params = parse_qs(parsed.query)
        job_id = params.get("jobId", [None])[0]
        
        if not job_id:
            self.send_json({"success": False, "error": "Job not found"}, 404)
            return
        
        job = job_repo.get(job_id)
        if not job:
            self.send_json({"success": False, "error": "Job not found"}, 404)
            return
        
        self.send_json({
            "success": True,
            "jobId": job["job_id"],
            "status": job["status"],
            "progress": job["progress"],
            "message": job["error"] if job["status"] == JobStatus.FAILED else job["status"],
            "result": job["result"],
            "error": job["error"],
        })
    
    def handle_list_jobs(self):
        """List the current user's upload jobs (v1 envelope).

        Serves ``GET /api/v1/jobs``. Supports ``?status=<state>`` and
        ``?limit=<n>``. The legacy ``GET /api/jobs`` is handled separately by
        :meth:`handle_list_jobs_legacy` so it keeps its original flat shape.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        status = params.get("status", [None])[0]
        if status and status not in JobStatus.ALL:
            self.send_json_response(
                *error_response(
                    f"Invalid status filter '{status}'",
                    details={"allowed": list(JobStatus.ALL)},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            )
            return
        try:
            limit = int(params.get("limit", ["50"])[0])
        except ValueError:
            self.send_json_response(
                *error_response(
                    "limit must be an integer", status_code=HTTPStatus.BAD_REQUEST
                )
            )
            return
        limit = max(1, min(limit, 200))

        jobs = job_repo.list(user_id=user_id, status=status, limit=limit)
        self.send_json_response(*success_response({
            "jobs": jobs,
            "total": len(jobs),
            "limit": limit,
        }))

    def handle_list_jobs_legacy(self):
        """List jobs with the legacy flat response shape.

        Serves ``GET /api/jobs``. Kept byte-compatible with the pre-v1
        response (``{"success": true, "jobs": [...]}``) so existing frontends
        keep working; new code should use ``GET /api/v1/jobs``.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return
        jobs = job_repo.list(user_id=user_id, limit=100)
        self.send_json({"success": True, "jobs": jobs})

    def handle_get_job(self):
        """Retrieve a single job.

        Serves ``GET /api/v1/jobs/{job_id}``.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return

        job_id = (getattr(self, "route_params", None) or {}).get("job_id")
        if not job_id:
            self.send_json_response(
                *error_response(
                    "job_id is required", status_code=HTTPStatus.BAD_REQUEST
                )
            )
            return

        job = job_repo.get(job_id)
        if not job:
            self.send_json_response(
                *error_response(
                    "Job not found", status_code=HTTPStatus.NOT_FOUND
                )
            )
            return
        # Do not leak another user's job metadata.
        if job["user_id"] != user_id:
            self.send_json_response(
                *error_response(
                    "Access denied", status_code=HTTPStatus.FORBIDDEN
                )
            )
            return
        self.send_json_response(*success_response(job))

    def handle_cancel_job(self):
        """Cancel a pending job.

        Serves ``POST /api/v1/jobs/{job_id}/cancel``. Only ``PENDING`` jobs can
        be cancelled; anything already running or finished returns ``409``.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return

        job_id = (getattr(self, "route_params", None) or {}).get("job_id")
        job = job_repo.get(job_id) if job_id else None
        if not job:
            self.send_json_response(
                *error_response(
                    "Job not found", status_code=HTTPStatus.NOT_FOUND
                )
            )
            return
        if job["user_id"] != user_id:
            self.send_json_response(
                *error_response(
                    "Access denied", status_code=HTTPStatus.FORBIDDEN
                )
            )
            return
        if job["status"] != JobStatus.PENDING:
            self.send_json_response(
                *error_response(
                    f"Cannot cancel a job with status '{job['status']}'",
                    details={"status": job["status"]},
                    status_code=HTTPStatus.CONFLICT,
                )
            )
            return

        # Update on the PENDING row only, so we never cancel a job that the
        # scheduler claimed in the meantime.
        cancelled = job_repo.cancel(job_id)
        if not cancelled:
            self.send_json_response(
                *error_response(
                    "Job is no longer cancellable (it started running)",
                    status_code=HTTPStatus.CONFLICT,
                )
            )
            return
        self.send_json_response(*success_response({
            "job_id": job_id,
            "status": JobStatus.CANCELLED,
        }))

    def handle_start_job(self):
        """Start a pending job immediately.

        Serves ``POST /api/v1/jobs/{job_id}/start``, the v1 equivalent of the
        legacy ``/api/schedule/upload`` trigger.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return

        job_id = (getattr(self, "route_params", None) or {}).get("job_id")
        job = job_repo.get(job_id) if job_id else None
        if not job:
            self.send_json_response(
                *error_response(
                    "Job not found", status_code=HTTPStatus.NOT_FOUND
                )
            )
            return
        if job["user_id"] != user_id:
            self.send_json_response(
                *error_response(
                    "Access denied", status_code=HTTPStatus.FORBIDDEN
                )
            )
            return

        if job["status"] == JobStatus.CANCELLED:
            job_repo.update(job_id, {
                "status": JobStatus.PENDING, "error": None, "progress": 0,
            })
            job = job_repo.get(job_id)

        # Claim atomically so a scheduler tick cannot double-start the job.
        if not job_repo.claim(job_id):
            self.send_json_response(
                *error_response(
                    f"Job is not startable from status '{job['status']}'",
                    details={"status": job["status"]},
                    status_code=HTTPStatus.CONFLICT,
                )
            )
            return
        job = job_repo.get(job_id) or job
        threading.Thread(target=run_upload_job, args=(job,), daemon=True).start()
        self.send_json_response(
            *success_response({"job_id": job_id, "status": JobStatus.PROCESSING})
        )

    def handle_create_upload(self):
        """Create an upload job.

        Serves ``POST /api/v1/upload``. Responds ``202 Accepted`` with a
        ``Location`` header pointing at the new job. Jobs with a
        ``scheduled_time`` stay ``PENDING`` for the scheduler; jobs without one
        are claimed and started immediately.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return

        payload, err = self.get_json_body()
        if err:
            self.send_json_response(*err)
            return

        job_data, validation_error = validate_upload_payload(payload)
        if validation_error:
            self.send_json_response(
                *error_response(
                    validation_error, status_code=HTTPStatus.BAD_REQUEST
                )
            )
            return

        try:
            job_id = job_repo.create({
                "user_id": user_id,
                "drive_file_id": job_data["drive_file_id"],
                "title": job_data["title"],
                "description": job_data["description"],
                "platforms": job_data["platforms"],
                "youtube_account_id": job_data["youtube_account_id"],
                "privacy": job_data["privacy"],
                "scheduled_time": job_data["scheduled_time"],
                "status": JobStatus.PENDING,
            })
        except Exception as e:
            logging.error(f"Failed to create upload job: {e}")
            self.send_json_response(
                *error_response(
                    "Could not create upload job",
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )
            return

        location = f"/api/v1/jobs/{job_id}"

        # No scheduled time -> run now. Claim first so the scheduler cannot
        # start the same job twice.
        if not job_data["scheduled_time"]:
            if job_repo.claim(job_id):
                fresh = job_repo.get(job_id)
                threading.Thread(
                    target=run_upload_job, args=(fresh,), daemon=True
                ).start()
            else:
                self.send_json_response(
                    *error_response(
                        "Job could not be claimed for immediate upload",
                        status_code=HTTPStatus.CONFLICT,
                    ),
                    headers={"Location": location},
                )
                return
            self.send_json_response(
                *accepted_response(job_id, location, status=JobStatus.PROCESSING)
            )
            return

        body, status, headers = accepted_response(
            job_id, location, status=JobStatus.PENDING
        )
        headers["Location"] = location
        self.send_json_response(body, status, headers)
    
    def handle_get_schedules(self):
        """Get user's scheduled uploads (pending + recent finished jobs)."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        jobs = job_repo.list(user_id=user_id, limit=100)
        schedules = [_job_to_schedule(j) for j in jobs]
        self.send_json({"success": True, "schedules": schedules})
    
    def handle_schedule(self):
        """Create scheduled upload(s) as persistent jobs."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        
        created = []
        rejected = []
        for item in body.get("items", []):
            platforms, invalid = validate_platforms(item.get("platforms"))
            if invalid:
                rejected.append({
                    "title": item.get("title"),
                    "invalid": invalid,
                    "supported": list(SUPPORTED_PLATFORMS),
                })
                continue
            job_id = job_repo.create({
                "user_id": user_id,
                "drive_file_id": item.get("videoId") or item.get("driveFileId"),
                "title": item.get("title") or "Untitled",
                "description": item.get("description", ""),
                "platforms": platforms,
                "youtube_account_id": item.get("youtubeAccountId"),
                "privacy": item.get("privacy", "private"),
                # ``scheduleTime`` is the user-picked moment; a missing value
                # means "run on the next scheduler tick".
                "scheduled_time": item.get("scheduleTime"),
                "status": JobStatus.PENDING,
            })
            created.append(job_id)
        
        self.send_json({
            "success": not rejected,
            "message": f"Scheduled {len(created)} video(s)",
            "jobIds": created,
            "rejected": rejected,
        })
    
    def handle_delete_schedule(self):
        """Cancel a scheduled upload.

        This is a **soft delete**: the job row is kept and its status is set to
        ``CANCELLED`` so the scheduler skips it while the history/audit trail
        survives. Use :meth:`JobRepository.delete` for a hard delete.
        """
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        schedule_id = body.get("id")
        
        job = job_repo.get(schedule_id)
        if not job or job["user_id"] != user_id:
            self.send_json({"success": False, "error": "Schedule not found"}, 404)
            return
        
        job_repo.update(schedule_id, {
            "status": JobStatus.CANCELLED,
            "error": "Cancelled by user",
        })
        
        self.send_json({"success": True, "message": "Schedule deleted"})
    
    def handle_upload_scheduled(self):
        """Trigger upload for scheduled item immediately."""
        user_id, user = self.require_auth()
        if not user_id:
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        schedule_id = body.get("id")
        
        job = job_repo.get(schedule_id)
        if not job or job["user_id"] != user_id:
            self.send_json({"success": False, "error": "Schedule not found"}, 404)
            return
        
        if job["status"] == JobStatus.CANCELLED:
            job_repo.update(schedule_id, {"status": JobStatus.PENDING, "error": None})
            job = job_repo.get(schedule_id)
        
        # Claim before starting so a concurrent scheduler tick cannot also run
        # this job; if it is already running we report the existing jobId.
        if not job_repo.claim(schedule_id):
            return self.send_json({
                "success": True,
                "jobId": job["job_id"],
                "message": "Job already running",
            })
        job = job_repo.get(schedule_id) or job
        
        thread = threading.Thread(target=run_upload_job, args=(job,), daemon=True)
        thread.start()
        
        self.send_json({"success": True, "jobId": job["job_id"]})
    
    def log_message(self, format, *args):
        """Custom logging."""
        logging.info(f"[Server] {args[0]}")

def run_server(port=8765, host="0.0.0.0"):
    """Start the secure dashboard server."""
    # Emit INFO-level records so scheduler activity is visible, then attach the
    # redaction filter that strips tokens from every log line.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    setup_secure_logging()
    # Resume any pending/due jobs persisted from a previous run
    resume_pending_jobs()
    server = HTTPServer((host, port), SecureDashboardHandler)

    print(f"🔐 Secure Video Scheduler Dashboard")
    print(f"🚀 Running at http://{host}:{port}")
    print(f"📁 Data directory: {DATA_DIR}")
    print("Press Ctrl+C to stop")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        server.shutdown()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    run_server(port)
