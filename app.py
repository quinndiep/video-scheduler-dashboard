"""
FastAPI application for the Media Scheduler.

Production-ready async API server that fronts the same business logic the
stdlib HTTP server uses. It is a drop-in replacement for ``server_secure.py``:
the ``/api/v1/`` routes, the ``{success, data|error}`` envelope, the HTTP status
codes and the ``Location`` header on 202 responses are all identical, so an
existing client cannot tell the difference.

Business logic (Drive download, per-platform uploads, job persistence, the
scheduler loop) is imported from ``server_secure`` and ``jobs_db`` rather than
duplicated, so both servers stay in sync.

Run with::

    uvicorn app:app --host 0.0.0.0 --port 8770
    python app.py                     # convenience wrapper
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field, field_validator

from jobs_db import JobRepository, JobStatus
from security import setup_secure_logging
from server_secure import (
    DATA_DIR,
    SCOPES,
    SESSION_EXPIRY_HOURS,
    SUPPORTED_PLATFORMS,
    create_session,
    delete_session,
    get_oauth_flow,
    get_session_user,
    get_user_drive_service,
    get_user_youtube_service,
    hash_password,
    load_users,
    run_upload_job,
    save_oauth_flow,
    save_users,
    validate_platforms,
    verify_password,
)

# ---------------------------------------------------------------------
# Logging & services
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    force=True,
)
setup_secure_logging()

JOBS_DB_PATH = str(DATA_DIR / "jobs.db")
job_repo = JobRepository(JOBS_DB_PATH)

# Maximum accepted request body, matching the stdlib server's limit.
MAX_BODY_BYTES = 256 * 1024

app = FastAPI(
    title="Media Scheduler API",
    version="1.0.0",
    description=(
        "Multi-platform video upload scheduling: queue or schedule Drive "
        "videos for YouTube, Instagram, Facebook and TikTok."
    ),
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
)

# CORS: ``allow_origins=["*"]`` with credentials is invalid (browsers reject it
# and it lets any site ride the session cookie). Pin the allowed origins via the
# ``CORS_ALLOW_ORIGINS`` env var (comma-separated); the localhost dev origins are
# the fallback so local testing keeps working.
def _allowed_origins() -> List[str]:
    configured = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
    if configured:
        return [o.strip() for o in configured.split(",") if o.strip()]
    return [
        "http://127.0.0.1:8770",
        "http://localhost:8770",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------
class UploadRequest(BaseModel):
    """Request body for creating an upload job."""

    drive_file_id: str = Field(
        ..., min_length=1, max_length=255, description="Google Drive file ID"
    )
    title: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=5000)
    platforms: List[str] = Field(
        ..., min_length=1, max_length=len(SUPPORTED_PLATFORMS)
    )
    youtube_account_id: Optional[str] = None
    scheduled_time: Optional[datetime] = Field(
        None, description="When to publish (UTC). Omit to upload immediately."
    )
    privacy: Optional[str] = "private"

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, value: List[str]) -> List[str]:
        """Reject unsupported platforms and normalise case/whitespace."""
        normalised, invalid = validate_platforms(value)
        if invalid:
            raise ValueError(
                f"Unsupported platform(s): {', '.join(invalid)}. "
                f"Supported: {', '.join(SUPPORTED_PLATFORMS)}"
            )
        return normalised

    @field_validator("privacy")
    @classmethod
    def _check_privacy(cls, value: Optional[str]) -> str:
        """Restrict privacy to the three YouTube settings."""
        if value not in ("public", "unlisted", "private"):
            raise ValueError("privacy must be public, unlisted or private")
        return value


class JobResponse(BaseModel):
    """A persisted upload job."""

    job_id: str
    user_id: str
    drive_file_id: str
    title: str
    description: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    youtube_account_id: Optional[str] = None
    status: str
    progress: int = 0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    scheduled_time: Optional[str] = None


class SuccessEnvelope(BaseModel):
    """Standard success envelope."""

    success: bool = True
    data: Any = None


class ErrorEnvelope(BaseModel):
    """Standard error envelope (matches the stdlib server exactly)."""

    success: bool = False
    error: Dict[str, Any]


class MessageData(BaseModel):
    """Simple ``{job_id, status}`` style payload."""

    job_id: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    total: Optional[int] = None
    limit: Optional[int] = None
    count: Optional[int] = None
    user_id: Optional[str] = None
    google_connected: Optional[bool] = None
    authenticated: Optional[bool] = None
    auth_url: Optional[str] = None
    accounts: Optional[List[Dict[str, Any]]] = None
    account: Optional[Dict[str, Any]] = None
    videos: Optional[List[Dict[str, Any]]] = None
    folders: Optional[List[Dict[str, Any]]] = None
    jobs: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------
def error_body(message: str, code: int, details: Any = None) -> Dict[str, Any]:
    """Build the standard error body.

    Args:
        message: Human-readable failure description.
        code: Machine-readable code (mirrors the HTTP status).
        details: Optional per-field detail.

    Returns:
        dict: The ``{"success": False, "error": {...}}`` body.
    """
    return {
        "success": False,
        "error": {"message": message, "code": code, "details": details},
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Render ``HTTPException`` in the standard error envelope.

    A handler may pass ``detail={"message": ..., "code": ...}`` to override the
    machine-readable code, which is how ``google_not_connected`` and friends keep
    a stable identifier regardless of the HTTP status.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message", "Request failed")
        code = detail.get("code", exc.status_code)
        details = detail.get("details")
    else:
        message = str(detail)
        code = exc.status_code
        details = None
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(message, code, details),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Render Pydantic validation failures in the standard envelope.

    Uses **400 Bad Request** rather than FastAPI's default 422 so that responses
    are byte-identical to the stdlib server's ``validate_upload_payload``. The
    per-field breakdown is preserved in ``error.details``.
    """
    from fastapi.encoders import jsonable_encoder

    try:
        details = jsonable_encoder(exc.errors())
    except Exception:
        # A validator's ctx can hold a non-encodable exception object.
        details = [
            {
                "loc": list(err.get("loc", [])),
                "msg": err.get("msg"),
                "type": err.get("type"),
            }
            for err in exc.errors()
        ]
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=error_body(
            "Request validation failed",
            status.HTTP_400_BAD_REQUEST,
            details=details,
        ),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Catch-all so an unexpected error never leaks a stack trace."""
    logging.exception(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body("Internal server error", 500),
    )


@app.middleware("http")
async def guard_request_envelope(request: Request, call_next):
    """Reject oversized bodies and wrong content types before routing.

    FastAPI/Starlette do not enforce a body-size cap or require JSON, so both
    checks live here. Responses use the same error envelope as every other
    endpoint.
    """
    if request.method in ("POST", "PUT", "PATCH"):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content=error_body(
                    f"Request body exceeds {MAX_BODY_BYTES} bytes",
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                ),
            )
        # Only JSON APIs are served; form/multipart uploads are not accepted.
        content_type = request.headers.get("content-type", "")
        base = content_type.split(";")[0].strip().lower()
        if base and base != "application/json":
            return JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content=error_body(
                    "Content-Type must be application/json",
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                ),
            )
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def starlette_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render Starlette-level errors (404/405) in the standard envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(str(exc.detail), exc.status_code),
        headers=getattr(exc, "headers", None),
    )


# ---------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------
def current_user(request: Request) -> str:
    """Resolve the signed-in user from the ``session`` cookie.

    Delegates to the same ``get_session_user`` helper the stdlib server uses, so
    expiry and deletion behave identically on both servers.

    Raises:
        HTTPException: 401 when the session is missing, unknown or expired.
    """
    session_id = request.cookies.get("session")
    user_id = get_session_user(session_id) if session_id else None
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user_id


def require_google(user_id: str = Depends(current_user)) -> str:
    """Ensure the caller has completed Google OAuth.

    Args:
        user_id: The signed-in user, resolved by :func:`current_user`.

    Returns:
        str: The same ``user_id``, so the dependency can be used directly in
        endpoints that need both the id and the Google check.

    Raises:
        HTTPException: 401 when the user record is missing or Google is not
            connected.
    """
    user = load_users().get(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    if not user.get("google_token"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Google account is not connected; complete OAuth first",
                "code": "google_not_connected",
            },
        )
    return user_id


# ---------------------------------------------------------------------
# Health & meta
# ---------------------------------------------------------------------
@app.get("/health", tags=["meta"])
async def health_check():
    """Liveness probe used by the process manager."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "jobs_db": JOBS_DB_PATH,
    }


# ---------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------
SESSION_COOKIE = "session"
SESSION_MAX_AGE = SESSION_EXPIRY_HOURS * 3600


class CredentialsRequest(BaseModel):
    """Email + password body for register/login."""

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=8, max_length=200)


class GoogleCredentialsRequest(BaseModel):
    """OAuth client credentials for this account."""

    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)


def _set_session_cookie(response: JSONResponse, session_id: str) -> JSONResponse:
    """Attach the HttpOnly session cookie used by both servers."""
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post(
    "/api/v1/auth/register",
    response_model=SuccessEnvelope,
    tags=["auth"],
    responses={400: {"model": ErrorEnvelope}},
)
async def register(payload: CredentialsRequest):
    """Create an account and sign the user in.

    Raises:
        HTTPException: 400 when the email is taken.
    """
    email = payload.email.strip().lower()
    users = load_users()
    if email in users:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Email already registered", "code": "email_taken"},
        )
    hashed, salt = hash_password(payload.password)
    users[email] = {
        "email": email,
        "password_hash": hashed,
        "password_salt": salt,
        "created": datetime.now().isoformat(),
    }
    save_users(users)
    session_id = create_session(email)
    return _set_session_cookie(
        JSONResponse(
            content={"success": True, "message": "Registration successful"}
        ),
        session_id,
    )


@app.post(
    "/api/v1/auth/login",
    response_model=SuccessEnvelope,
    tags=["auth"],
    responses={401: {"model": ErrorEnvelope}},
)
async def login(payload: CredentialsRequest):
    """Sign in and set the session cookie.

    Raises:
        HTTPException: 401 on bad credentials (the same message for an unknown
            email and a wrong password, so accounts cannot be enumerated).
    """
    email = payload.email.strip().lower()
    user = load_users().get(email)
    if not user or not verify_password(
        payload.password, user["password_hash"], user["password_salt"]
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Invalid email or password", "code": "bad_credentials"},
        )
    session_id = create_session(email)
    return _set_session_cookie(
        JSONResponse(content={"success": True, "message": "Login successful"}),
        session_id,
    )


@app.post("/api/v1/auth/logout", tags=["auth"])
async def logout(request: Request):
    """Invalidate the session and clear the cookie."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        delete_session(session_id)
    response = JSONResponse(content={"success": True, "message": "Logged out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/v1/auth/status", tags=["auth"])
async def auth_status(request: Request):
    """Report whether the caller is signed in and Google-connected."""
    session_id = request.cookies.get(SESSION_COOKIE)
    user_id = get_session_user(session_id) if session_id else None
    if not user_id:
        return {"authenticated": False, "google_connected": False}
    user = load_users().get(user_id) or {}
    return {
        "authenticated": True,
        "user_id": user_id,
        "email": user_id,
        "google_connected": bool(user.get("google_token")),
    }


@app.post(
    "/api/v1/auth/setup-google",
    response_model=SuccessEnvelope,
    tags=["auth"],
    responses={400: {"model": ErrorEnvelope}, 401: {"model": ErrorEnvelope}},
)
async def setup_google(payload: GoogleCredentialsRequest,
                       user_id: str = Depends(current_user)):
    """Store the OAuth client id/secret for this account.

    Raises:
        HTTPException: 400 when either value is missing.
    """
    users = load_users()
    record = users.get(user_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    record["google_client_id"] = payload.client_id
    record["google_client_secret"] = payload.client_secret
    save_users(users)
    return {"success": True, "message": "Google credentials saved"}


@app.post(
    "/api/v1/auth/start-oauth",
    response_model=SuccessEnvelope,
    tags=["auth"],
    responses={400: {"model": ErrorEnvelope}, 401: {"model": ErrorEnvelope}},
)
async def start_oauth(user_id: str = Depends(current_user)):
    """Return the Google consent URL for the primary account.

    Raises:
        HTTPException: 400 when the client id/secret are not configured.
    """
    user = load_users().get(user_id) or {}
    if not user.get("google_client_id") or not user.get("google_client_secret"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Google credentials are not configured for this account",
                "code": "google_not_configured",
            },
        )
    auth_url = _build_oauth_url(user, user_id, add_account=False)
    return {"success": True, "data": {"auth_url": auth_url}}


@app.post(
    "/api/v1/auth/add-youtube-account",
    response_model=SuccessEnvelope,
    tags=["auth"],
    responses={400: {"model": ErrorEnvelope}, 401: {"model": ErrorEnvelope}},
)
async def add_youtube_account(user_id: str = Depends(current_user)):
    """Return a consent URL that links an *additional* YouTube channel.

    Raises:
        HTTPException: 400 when the client id/secret are not configured.
    """
    user = load_users().get(user_id) or {}
    if not user.get("google_client_id") or not user.get("google_client_secret"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Google credentials are not configured for this account",
                "code": "google_not_configured",
            },
        )
    auth_url = _build_oauth_url(user, user_id, add_account=True)
    return {"success": True, "data": {"auth_url": auth_url}}


def _build_oauth_url(user: Dict[str, Any], user_id: str, add_account: bool) -> str:
    """Build (and persist) a Google OAuth consent URL.

    Args:
        user: The caller's user record, which must contain the client id/secret.
        user_id: Owner of the flow, stored in the OAuth state payload.
        add_account: When ``True`` the flow stores the result as an additional
            YouTube account and lets the user pick a different channel.

    Returns:
        str: The consent URL to redirect the browser to.
    """
    from base64 import b64encode
    import secrets
    import urllib.parse

    redirect_uri = f"{os.environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:8770')}/oauth/callback"
    state = b64encode(
        json.dumps(
            {
                "user_id": user_id,
                "nonce": secrets.token_urlsafe(16),
                "add_account": add_account,
            }
        ).encode()
    ).decode()

    params = {
        "client_id": user["google_client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # Without select_account Google silently reauthorises the first channel.
        "prompt": "consent select_account" if add_account else "consent",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
    }
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
    return "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------
# Jobs API
# ---------------------------------------------------------------------
def _owned_job(job_id: str, user_id: str) -> Dict[str, Any]:
    """Fetch a job and verify ownership.

    Args:
        job_id: Identifier of the job.
        user_id: Caller's user id.

    Returns:
        dict: The job row.

    Raises:
        HTTPException: 404 when the job does not exist, 403 when it belongs to
            somebody else.
    """
    job = job_repo.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        )
    if job["user_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )
    return job


def dispatch_upload(job_id: str, platforms: List[str]) -> str:
    """Start a job's upload, preferring Celery and falling back to a thread.

    Celery is the production path: the work survives a worker restart and can be
    scaled out. But the API must still work on a laptop with no Redis running, so
    if the broker is unreachable we fall back to an in-process thread.

    Args:
        job_id: Job to execute.
        platforms: Target platform names.

    Returns:
        str: ``"celery"`` or ``"thread"``, so callers can log which path ran.
    """
    try:
        from tasks import launch_multi_platform_upload

        launch_multi_platform_upload(job_id, platforms)
        logging.info(f"Job {job_id} dispatched to Celery")
        return "celery"
    except Exception as e:
        # Broker down, celery/redis not installed, or import error: stay usable.
        logging.warning(
            f"Celery dispatch unavailable ({type(e).__name__}: {e}); "
            f"falling back to an in-process thread for job {job_id}"
        )
        threading.Thread(
            target=run_upload_job,
            args=(job_repo.get(job_id),),
            daemon=True,
        ).start()
        return "thread"


@app.post(
    "/api/v1/upload",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    responses={
        202: {
            "description": "Job accepted",
            "content": {"application/json": {"example": {
                "success": True,
                "data": {"job_id": "…", "status": "processing"},
            }}},
        },
        400: {"model": ErrorEnvelope},
        401: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
async def create_upload(
    payload: UploadRequest,
    user_id: str = Depends(require_google),
):
    """Create an upload job.

    With no ``scheduled_time`` the job is claimed and started immediately;
    otherwise it stays ``PENDING`` for the background scheduler.

    Returns:
        JSONResponse: 202 with ``Location: /api/v1/jobs/{job_id}``.
    """
    job_id = job_repo.create(
        {
            "user_id": user_id,
            "drive_file_id": payload.drive_file_id,
            "title": payload.title,
            "description": payload.description or "",
            "platforms": payload.platforms,
            "youtube_account_id": payload.youtube_account_id,
            "privacy": payload.privacy,
            "scheduled_time": payload.scheduled_time,
            "status": JobStatus.PENDING,
        }
    )

    location = f"/api/v1/jobs/{job_id}"

    if not payload.scheduled_time:
        # Claim atomically so the scheduler cannot start the same job twice.
        if job_repo.claim(job_id):
            dispatch_upload(job_id, payload.platforms)
            reported = JobStatus.PROCESSING
        else:
            reported = JobStatus.PENDING
    else:
        reported = JobStatus.PENDING

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "success": True,
            "data": {"job_id": job_id, "status": reported},
        },
        headers={"Location": location},
    )


@app.get(
    "/api/v1/jobs",
    response_model=SuccessEnvelope,
    tags=["jobs"],
    responses={401: {"model": ErrorEnvelope}},
)
async def list_jobs(
    job_status: Optional[str] = Query(None, alias="status"),
    limit: int = 50,
    user_id: str = Depends(current_user),
):
    """List the caller's jobs, newest first.

    Args:
        job_status: Optional ``?status=`` filter; must be a known job state.
        limit: Maximum rows to return (1-200).

    Returns:
        dict: ``{"success": True, "data": {"jobs": [...], "total": n, "limit": n}}``

    Raises:
        HTTPException: 400 for an unknown status or a non-integer limit.
    """
    if job_status and job_status not in JobStatus.ALL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"Invalid status filter '{job_status}'",
                "code": "invalid_status",
                "details": {"allowed": list(JobStatus.ALL)},
            },
        )
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "limit must be between 1 and 200", "code": "bad_limit"},
        )
    jobs = job_repo.list(user_id=user_id, status=job_status, limit=limit)
    return {
        "success": True,
        "data": {"jobs": jobs, "total": len(jobs), "limit": limit},
    }


@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=SuccessEnvelope,
    tags=["jobs"],
    responses={
        401: {"model": ErrorEnvelope},
        403: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
    },
)
async def get_job(job_id: str, user_id: str = Depends(current_user)):
    """Return a single job owned by the caller.

    Raises:
        HTTPException: 404 when unknown, 403 when owned by another user.
    """
    job = _owned_job(job_id, user_id)
    return {"success": True, "data": job}


@app.post(
    "/api/v1/jobs/{job_id}/cancel",
    response_model=SuccessEnvelope,
    tags=["jobs"],
    responses={
        403: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
    },
)
async def cancel_job(job_id: str, user_id: str = Depends(current_user)):
    """Cancel a pending job.

    The cancellation is a conditional UPDATE, so a request racing the
    scheduler's claim cannot cancel a job that already started.

    Raises:
        HTTPException: 409 when the job is no longer pending.
    """
    job = _owned_job(job_id, user_id)
    if not job_repo.cancel(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Cannot cancel job with status '{job['status']}'"
                    if job["status"] != JobStatus.PENDING
                    else "Job is no longer cancellable (it started running)"
                ),
                "code": "not_cancellable",
                "details": {"status": job["status"]},
            },
        )
    return {
        "success": True,
        "data": {"job_id": job_id, "status": JobStatus.CANCELLED},
    }


@app.post(
    "/api/v1/jobs/{job_id}/start",
    response_model=SuccessEnvelope,
    tags=["jobs"],
    responses={
        403: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
    },
)
async def start_job(job_id: str, user_id: str = Depends(current_user)):
    """Start a pending or cancelled job immediately.

    Raises:
        HTTPException: 409 when the job is already running or finished.
    """
    job = _owned_job(job_id, user_id)
    if job["status"] == JobStatus.CANCELLED:
        job_repo.update(
            job_id, {"status": JobStatus.PENDING, "error": None, "progress": 0}
        )
        job = job_repo.get(job_id)

    if not job_repo.claim(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": f"Job is not startable from status '{job['status']}'",
                "code": "not_startable",
                "details": {"status": job["status"]},
            },
        )
    dispatch_upload(job_id, job.get("platforms") or ["youtube"])
    return {
        "success": True,
        "data": {"job_id": job_id, "status": JobStatus.PROCESSING},
    }


# ---------------------------------------------------------------------
# Drive API
# ---------------------------------------------------------------------
VIDEO_MIME_TYPES = [
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/webm",
    "video/x-matroska", "video/mpeg", "video/3gpp", "video/x-flv",
]


@app.get(
    "/api/v1/drive/videos",
    response_model=SuccessEnvelope,
    tags=["drive"],
    responses={401: {"model": ErrorEnvelope}},
)
async def list_drive_videos(folder_id: Optional[str] = None,
                            user_id: str = Depends(current_user)):
    """List videos in Drive (optionally inside one folder)."""
    drive = get_user_drive_service(user_id)
    if not drive:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Google Drive is not connected", "code": "no_drive"},
        )
    query = "trashed=false"
    if folder_id:
        query += f" and '{folder_id}' in parents"
    else:
        query += " and 'root' in parents"

    try:
        response = drive.files().list(
            q=query,
            fields=(
                "files(id,name,mimeType,size,modifiedTime,"
                "thumbnailLink,webViewLink,webContentLink,videoMediaMetadata)"
            ),
            orderBy="name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        logging.error(f"Drive list failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": f"Drive request failed: {e}", "code": "drive_error"},
        )

    files = [
        f
        for f in response.get("files", [])
        if f.get("mimeType") in VIDEO_MIME_TYPES
    ]
    return {"success": True, "data": {"videos": files, "count": len(files)}}


@app.get(
    "/api/v1/drive/folders",
    response_model=SuccessEnvelope,
    tags=["drive"],
    responses={401: {"model": ErrorEnvelope}},
)
async def list_drive_folders(parent_id: Optional[str] = None,
                             user_id: str = Depends(current_user)):
    """List Drive folders, optionally inside a given parent."""
    drive = get_user_drive_service(user_id)
    if not drive:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Google Drive is not connected", "code": "no_drive"},
        )
    parent = parent_id or "root"
    try:
        response = drive.files().list(
            q=(
                f"mimeType='application/vnd.google-apps.folder' "
                f"and trashed=false and '{parent}' in parents"
            ),
            fields="files(id,name)",
            orderBy="name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        logging.error(f"Drive folder list failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": f"Drive request failed: {e}", "code": "drive_error"},
        )
    folders = response.get("files", [])
    return {"success": True, "data": {"folders": folders, "count": len(folders)}}


@app.get(
    "/api/v1/youtube/channel",
    response_model=SuccessEnvelope,
    tags=["drive"],
    responses={401: {"model": ErrorEnvelope}},
)
async def youtube_channel(user_id: str = Depends(current_user)):
    """Return the connected channel's public metadata."""
    youtube = get_user_youtube_service(user_id)
    if not youtube:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "YouTube is not connected", "code": "no_youtube"},
        )
    try:
        response = youtube.channels().list(
            part="snippet,statistics", mine=True
        ).execute()
    except Exception as e:
        logging.error(f"YouTube channel lookup failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": f"YouTube request failed: {e}", "code": "youtube_error"},
        )
    items = response.get("items") or []
    if not items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "No YouTube channel found", "code": "no_channel"},
        )
    channel = items[0]
    return {
        "success": True,
        "data": {
            "channel": {
                "id": channel["id"],
                "title": channel["snippet"]["title"],
                "thumbnail": channel["snippet"]["thumbnails"]["default"]["url"],
                "subscriberCount": channel.get("statistics", {}).get(
                    "subscriberCount", 0
                ),
            }
        },
    }


# ---------------------------------------------------------------------
# Accounts API
# ---------------------------------------------------------------------
@app.get(
    "/api/v1/accounts",
    response_model=SuccessEnvelope,
    tags=["accounts"],
    responses={401: {"model": ErrorEnvelope}},
)
async def list_accounts(user_id: str = Depends(current_user)):
    """List the caller's linked YouTube accounts (never their tokens)."""
    user = load_users().get(user_id) or {}
    accounts = user.get("youtube_accounts", [])
    return {
        "success": True,
        "data": {
            "accounts": [
                {"account_id": a.get("account_id"), "email": a.get("email")}
                for a in accounts
            ],
            "count": len(accounts),
        },
    }


@app.get(
    "/api/v1/accounts/{account_id}",
    response_model=SuccessEnvelope,
    tags=["accounts"],
    responses={403: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}},
)
async def get_account(account_id: str, user_id: str = Depends(current_user)):
    """Return one linked account's metadata, with the token stripped out."""
    user = load_users().get(user_id) or {}
    match = next(
        (
            a
            for a in user.get("youtube_accounts", [])
            if a.get("account_id") == account_id
        ),
        None,
    )
    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
        )
    safe = {k: v for k, v in match.items() if k != "token"}
    return {"success": True, "data": {"account": safe}}


@app.delete(
    "/api/v1/accounts/{account_id}",
    response_model=SuccessEnvelope,
    tags=["accounts"],
    responses={404: {"model": ErrorEnvelope}},
)
async def delete_account(account_id: str, user_id: str = Depends(current_user)):
    """Unlink a YouTube account (soft delete: the credential is removed)."""
    users = load_users()
    accounts = users.get(user_id, {}).get("youtube_accounts", [])
    match = next(
        (a for a in accounts if a.get("account_id") == account_id), None
    )
    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
        )
    accounts.remove(match)
    users[user_id]["youtube_accounts"] = accounts
    from server_secure import save_users  # local import avoids a cycle at import time

    save_users(users)
    return {
        "success": True,
        "data": {"account_id": account_id, "message": "Account deleted"},
    }


# ---------------------------------------------------------------------
# Legacy aliases (deprecation headers match the stdlib server)
# ---------------------------------------------------------------------
def _legacy_headers() -> Dict[str, str]:
    """Return the deprecation headers attached to every legacy response."""
    return {
        "Deprecation": "true",
        "X-API-Deprecated": "true",
        "X-API-Migrate-To": "/api/v1/jobs",
        "Link": '</api/v1/jobs>; rel="successor-version"',
    }


@app.get("/api/schedules", tags=["legacy"], include_in_schema=False)
async def schedules_legacy(user_id: str = Depends(current_user)):
    """Deprecated: use ``GET /api/v1/jobs``."""
    jobs = job_repo.list(user_id=user_id, limit=100)
    schedules = [
        {
            "id": j["job_id"],
            "jobId": j["job_id"],
            "videoId": j["drive_file_id"],
            "title": j["title"],
            "description": j["description"] or "",
            "privacy": j["privacy"] or "private",
            "platforms": j["platforms"],
            "scheduleTime": j["scheduled_time"],
            "status": j["status"],
            "progress": j["progress"],
            "error": j["error"],
        }
        for j in jobs
    ]
    return JSONResponse(
        content={"success": True, "schedules": schedules},
        headers=_legacy_headers(),
    )


@app.get("/api/jobs", tags=["legacy"], include_in_schema=False)
async def list_jobs_legacy(user_id: str = Depends(current_user)):
    """Deprecated: use ``GET /api/v1/jobs``."""
    jobs = job_repo.list(user_id=user_id, limit=100)
    return JSONResponse(
        content={"success": True, "jobs": jobs}, headers=_legacy_headers()
    )


@app.get("/api/upload/status", tags=["legacy"], include_in_schema=False)
async def upload_status_legacy(jobId: str, user_id: str = Depends(current_user)):
    """Deprecated: use ``GET /api/v1/jobs/{job_id}``."""
    job = _owned_job(jobId, user_id)
    return JSONResponse(
        content={"success": True, **job},
        headers=_legacy_headers(),
    )


# ---------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).parent


@app.get("/", include_in_schema=False)
async def index():
    """Serve the dashboard shell."""
    return FileResponse(str(BASE_DIR / "index.html"))


@app.get("/login.html", include_in_schema=False)
async def login_page():
    """Serve the login/setup page."""
    return FileResponse(str(BASE_DIR / "login.html"))


# ---------------------------------------------------------------------
# Startup: resume the scheduler exactly as the stdlib server does
# ---------------------------------------------------------------------
@app.on_event("startup")
async def start_scheduler():
    """Start the background scheduler and report pending jobs."""
    from server_secure import resume_pending_jobs

    pending = job_repo.get_pending_jobs()
    logging.info(f"Found {len(pending)} pending job(s) on startup")
    threading.Thread(target=resume_pending_jobs, daemon=True).start()


@app.on_event("shutdown")
async def stop_scheduler():
    """Note scheduler shutdown (the loop is a daemon thread and exits with us)."""
    logging.info("Scheduler daemon thread exiting with the application")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8770)),
        log_level="info",
    )