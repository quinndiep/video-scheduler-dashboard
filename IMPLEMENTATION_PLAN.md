# 📋 Media Scheduler - Complete Implementation Plan

## 🎯 Project Overview

**Goal:** Build a multi-platform video scheduling dashboard that allows users to upload videos from Google Drive to YouTube, TikTok, Instagram, and Facebook with scheduled publishing.

**Current State:** 
- ✅ YouTube uploads working
- ✅ Google Drive integration
- ✅ Basic scheduling (JSON files)
- ⚠️ Instagram/Facebook/TikTok are stubs only
- ⚠️ Plain-text credential storage (security risk)
- ⚠️ In-memory job queue (no persistence)

**Target State:**
- ✅ Secure encrypted credential storage
- ✅ Persistent job queue (SQLite → PostgreSQL)
- ✅ Production-grade task queue (Celery + Redis)
- ✅ Real API integration for all 4 platforms
- ✅ FastAPI-based REST API with auto-docs
- ✅ 70%+ test coverage
- ✅ Rate limiting and retry logic

---

## 🏗️ System Architecture

### **High-Level Architecture**

```
┌─────────────────────────────────────────────────────────────┐
│                      Frontend Dashboard                      │
│  (HTML/CSS/JS - existing, served by FastAPI static files)   │
└────────────────┬────────────────────────────────────────────┘
                 │ HTTP/HTTPS
                 ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI REST API                          │
│                     (/api/v1/*)                              │
│  ┌──────────────┬──────────────┬──────────────┬──────────┐  │
│  │ Auth         │ Upload       │ Schedule     │ Status   │  │
│  │ /auth/*      │ /upload      │ /schedule/*  │ /jobs/*  │  │
│  └──────────────┴──────────────┴──────────────┴──────────┘  │
└────────────────┬───────────────┬─────────────────────────────┘
                 │               │
                 │               └──────────────┐
                 ▼                              ▼
┌─────────────────────────────┐  ┌─────────────────────────────┐
│   SQLite Jobs Database      │  │   Celery Task Queue         │
│   (jobs.db)                 │  │   (Redis broker)            │
│                             │  │                             │
│  - job_id (PK)              │  │  - upload_to_youtube        │
│  - user_id                  │  │  - upload_to_instagram      │
│  - drive_file_id            │  │  - upload_to_facebook       │
│  - platforms (JSON)         │  │  - upload_to_tiktok         │
│  - status                   │  │  - aggregate_results        │
│  - progress                 │  │                             │
│  - scheduled_time           │  │  Workers: 4 (scalable)      │
└─────────────────────────────┘  └─────────────────────────────┘
                 │                              │
                 └──────────────┬───────────────┘
                                ▼
                 ┌──────────────────────────────┐
                 │   Platform Upload Services   │
                 │                              │
                 │  • YouTube Data API v3       │
                 │  • Instagram Graph API       │
                 │  • Facebook Graph API        │
                 │  • TikTok Content Posting    │
                 └──────────────────────────────┘
                                │
                                ▼
                 ┌──────────────────────────────┐
                 │   Encrypted Credential Store │
                 │   (users.json + Fernet)      │
                 │                              │
                 │  • Google OAuth tokens       │
                 │  • Platform access tokens    │
                 │  • Account UUIDs             │
                 │                              │
                 │  Key: macOS Keychain         │
                 └──────────────────────────────┘
```

---

## 📐 Database Schema

### **Jobs Table (SQLite → PostgreSQL)**

```sql
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,           -- UUID
    user_id TEXT NOT NULL,             -- User identifier
    drive_file_id TEXT NOT NULL,       -- Google Drive file ID
    title TEXT NOT NULL,               -- Video title
    description TEXT,                  -- Video description
    platforms TEXT NOT NULL,           -- JSON array: ["youtube", "instagram"]
    youtube_account_id TEXT,           -- UUID of linked YouTube account
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, processing, completed, failed
    progress INTEGER DEFAULT 0,        -- 0-100
    result TEXT,                       -- JSON: per-platform results
    error TEXT,                        -- Error message if failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scheduled_time TIMESTAMP,          -- When to publish (NULL = immediate)
    
    CONSTRAINT valid_status CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled'))
);

-- Indexes for performance
CREATE INDEX idx_jobs_user_status ON jobs(user_id, status);
CREATE INDEX idx_jobs_scheduled ON jobs(scheduled_time) WHERE status='pending';
CREATE INDEX idx_jobs_created ON jobs(created_at DESC);

-- Example result JSON structure:
{
    "youtube": {"status": "success", "video_id": "dQw4w9WgXcQ", "url": "https://youtube.com/watch?v=..."},
    "instagram": {"status": "success", "media_id": "123456789", "url": "https://instagram.com/p/..."},
    "facebook": {"status": "failed", "error": "Invalid access token"},
    "tiktok": {"status": "success", "video_id": "7234567890", "url": "https://tiktok.com/@user/video/..."}
}
```

### **Users Schema (Encrypted JSON)**

```json
{
    "users": {
        "user_123": {
            "email": "user@example.com",
            "google_token": "ENCRYPTED_REFRESH_TOKEN",
            "youtube_accounts": [
                {
                    "account_id": "uuid-1234-5678",
                    "channel_id": "UCxxxxxx",
                    "channel_name": "My Channel",
                    "token": "ENCRYPTED_ACCESS_TOKEN"
                }
            ],
            "instagram_token": "ENCRYPTED_ACCESS_TOKEN",
            "facebook_token": "ENCRYPTED_ACCESS_TOKEN",
            "facebook_page_id": "123456789",
            "tiktok_token": "ENCRYPTED_ACCESS_TOKEN",
            "created_at": "2024-01-01T00:00:00Z",
            "last_login": "2024-01-15T10:30:00Z"
        }
    }
}
```

---

## 🔐 Security Architecture

### **Encryption Layer**

**File:** `security.py`

```python
from cryptography.fernet import Fernet
import keyring
import logging
import re

class CredentialManager:
    """Manages encrypted credential storage"""
    
    def __init__(self):
        self.key = self._get_or_create_key()
        self.fernet = Fernet(self.key)
    
    def _get_or_create_key(self) -> bytes:
        """Retrieve encryption key from macOS Keychain"""
        key = keyring.get_password("video-scheduler", "encryption_key")
        if not key:
            key = Fernet.generate_key().decode()
            keyring.set_password("video-scheduler", "encryption_key", key)
            logging.info("Created new encryption key in Keychain")
        return key.encode()
    
    def encrypt(self, data: str) -> str:
        """Encrypt sensitive data"""
        return self.fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted: str) -> str:
        """Decrypt sensitive data"""
        return self.fernet.decrypt(encrypted.encode()).decode()
    
    def rotate_key(self, users_data: dict) -> None:
        """Rotate encryption key (decrypt with old, re-encrypt with new)"""
        # Decrypt all tokens with current key
        decrypted = self._decrypt_all_tokens(users_data)
        
        # Generate new key
        new_key = Fernet.generate_key().decode()
        keyring.set_password("video-scheduler", "encryption_key", new_key)
        
        # Re-encrypt with new key
        self.key = new_key.encode()
        self.fernet = Fernet(self.key)
        self._encrypt_all_tokens(decrypted)
        
        logging.info("Encryption key rotated successfully")


class SensitiveDataFilter(logging.Filter):
    """Redact sensitive data from logs"""
    
    PATTERNS = [
        r'(access_token["\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        r'(refresh_token["\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        r'(bearer\s+)([A-Za-z0-9\-._~+/]+=*)',
        r'(Authorization:\s*)(Bearer\s+[A-Za-z0-9\-._~+/]+=*)',
    ]
    
    def filter(self, record):
        for pattern in self.PATTERNS:
            record.msg = re.sub(pattern, r'\1[REDACTED]', str(record.msg))
        return True

# Apply filter to all loggers
logging.getLogger().addFilter(SensitiveDataFilter())
```

### **Security Checklist**

- ✅ All tokens encrypted at rest (Fernet)
- ✅ Encryption key stored in macOS Keychain (not in code)
- ✅ Tokens redacted from logs (custom logging filter)
- ✅ API keys from environment variables (never hardcoded)
- ✅ HTTPS only (Let's Encrypt for production)
- ✅ JWT-based API authentication
- ✅ Minimal OAuth scopes (least privilege)
- ✅ Input validation and sanitization (Pydantic)
- ✅ Rate limiting per user/IP
- ✅ CORS properly configured
- ✅ SQL injection prevention (SQLAlchemy parameterized queries)
- ✅ Audit logging (who, when, what - never the token)

---

## 🔄 Task Queue Architecture (Celery)

### **Celery Configuration**

**File:** `celery_app.py`

```python
from celery import Celery
from celery.schedules import crontab

app = Celery(
    'video_scheduler',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/1'
)

# Configuration
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    
    # Task routing
    task_routes={
        'tasks.upload_to_youtube': {'queue': 'youtube'},
        'tasks.upload_to_instagram': {'queue': 'instagram'},
        'tasks.upload_to_facebook': {'queue': 'facebook'},
        'tasks.upload_to_tiktok': {'queue': 'tiktok'},
        'tasks.aggregate_results': {'queue': 'default'},
    },
    
    # Retry configuration
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_retry_delay=60,  # 1 minute
    task_max_retries=5,
    
    # Rate limiting
    task_annotations={
        'tasks.upload_to_youtube': {'rate_limit': '10/m'},  # 10 per minute
        'tasks.upload_to_instagram': {'rate_limit': '5/m'},
        'tasks.upload_to_facebook': {'rate_limit': '10/m'},
        'tasks.upload_to_tiktok': {'rate_limit': '5/m'},
    },
    
    # Scheduled tasks (cron)
    beat_schedule={
        'process-scheduled-jobs': {
            'task': 'tasks.process_scheduled_jobs',
            'schedule': crontab(minute='*/5'),  # Every 5 minutes
        },
        'cleanup-old-jobs': {
            'task': 'tasks.cleanup_old_jobs',
            'schedule': crontab(hour=2, minute=0),  # 2 AM daily
        },
    },
)
```

### **Task Definitions**

**File:** `tasks.py`

```python
from celery import group, chord
from .celery_app import app
from .jobs_db import JobRepository, JobStatus
from .platform_upload import (
    upload_to_youtube,
    upload_to_instagram,
    upload_to_facebook,
    upload_to_tiktok
)
import logging

logger = logging.getLogger(__name__)


class TransientAPIError(Exception):
    """Raised for temporary API errors that should trigger retry"""
    pass


@app.task(bind=True, max_retries=5)
def upload_to_platform_task(self, job_id: str, platform: str):
    """Upload to a single platform (parallel task)"""
    try:
        job = JobRepository.get(job_id)
        
        # Download video from Google Drive once (shared temp file)
        video_path = download_from_drive(job.drive_file_id)
        
        # Upload to platform
        if platform == 'youtube':
            result = upload_to_youtube(video_path, job.title, job.description, job.youtube_account_id)
        elif platform == 'instagram':
            result = upload_to_instagram(video_path, job.title)
        elif platform == 'facebook':
            result = upload_to_facebook(video_path, job.title)
        elif platform == 'tiktok':
            result = upload_to_tiktok(video_path, job.title)
        else:
            raise ValueError(f"Unknown platform: {platform}")
        
        return {'platform': platform, 'status': 'success', 'result': result}
    
    except TransientAPIError as e:
        # Retry on temporary errors (5xx, 429)
        logger.warning(f"Transient error on {platform}, retry {self.request.retries}/{self.max_retries}")
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))  # Exponential backoff
    
    except Exception as e:
        logger.error(f"Permanent error on {platform}: {e}")
        return {'platform': platform, 'status': 'failed', 'error': str(e)}


@app.task
def aggregate_results(results, job_id: str):
    """Aggregate results from parallel platform uploads"""
    job = JobRepository.get(job_id)
    
    # Combine results
    result_map = {r['platform']: r for r in results}
    
    # Determine overall status
    all_success = all(r['status'] == 'success' for r in results)
    any_success = any(r['status'] == 'success' for r in results)
    
    if all_success:
        status = JobStatus.COMPLETED
    elif any_success:
        status = JobStatus.PARTIALLY_COMPLETED
    else:
        status = JobStatus.FAILED
    
    # Update job
    JobRepository.update(job_id, {
        'status': status,
        'progress': 100,
        'result': result_map
    })
    
    logger.info(f"Job {job_id} completed with status {status}")


def launch_multi_platform_upload(job_id: str, platforms: list[str]):
    """Launch parallel uploads to multiple platforms"""
    
    # Create a chord: parallel tasks + callback
    sub_tasks = [upload_to_platform_task.s(job_id, p) for p in platforms]
    callback = aggregate_results.s(job_id)
    
    # Execute
    chord(sub_tasks)(callback)
    
    logger.info(f"Launched parallel upload for job {job_id} to platforms: {platforms}")


@app.task
def process_scheduled_jobs():
    """Check for scheduled jobs that are ready to run (cron task)"""
    from datetime import datetime
    
    pending_jobs = JobRepository.get_scheduled_jobs_ready()
    
    for job in pending_jobs:
        logger.info(f"Processing scheduled job {job.job_id}")
        launch_multi_platform_upload(job.job_id, job.platforms)


@app.task
def cleanup_old_jobs():
    """Delete completed jobs older than 30 days"""
    from datetime import datetime, timedelta
    
    cutoff = datetime.utcnow() - timedelta(days=30)
    deleted = JobRepository.delete_old_jobs(cutoff)
    
    logger.info(f"Deleted {deleted} old jobs")
```

### **Starting Celery Workers**

```bash
# Start Redis
redis-server

# Start Celery worker (4 concurrent workers)
celery -A celery_app worker --loglevel=info --concurrency=4

# Start Celery Beat (scheduler for cron tasks)
celery -A celery_app beat --loglevel=info

# Start Flower (monitoring dashboard)
celery -A celery_app flower --port=5555
# Visit http://localhost:5555 to monitor tasks
```

---

## 🌐 Platform API Integration Details

### **1. YouTube Data API v3**

**Already Working** ✅

**OAuth Scopes:** `https://www.googleapis.com/auth/youtube.upload`

**Upload Endpoint:**
```python
POST https://www.googleapis.com/upload/youtube/v3/videos
Content-Type: video/*

{
    "snippet": {
        "title": "Video Title",
        "description": "Video Description",
        "tags": ["tag1", "tag2"]
    },
    "status": {
        "privacyStatus": "public",  # public, unlisted, private
        "publishAt": "2024-01-20T15:00:00Z"  # Scheduled publish
    }
}
```

**Rate Limits:** 10,000 quota units/day (upload = ~1600 units)

---

### **2. Instagram Graph API**

**Status:** ⚠️ Stub only (needs implementation)

**OAuth Scopes:** 
- `instagram_basic`
- `instagram_content_publish`
- `pages_read_engagement`

**Two-Step Process:**

**Step 1: Create Media Container**
```python
POST https://graph.facebook.com/v18.0/{ig-user-id}/media
{
    "video_url": "https://publicly-accessible-url.com/video.mp4",  # Must be public!
    "caption": "Video caption with #hashtags",
    "access_token": "YOUR_ACCESS_TOKEN"
}

Response: {"id": "media_container_id"}
```

**Step 2: Publish Media**
```python
POST https://graph.facebook.com/v18.0/{ig-user-id}/media_publish
{
    "creation_id": "media_container_id",
    "access_token": "YOUR_ACCESS_TOKEN"
}

Response: {"id": "published_media_id"}
```

**Video Requirements:**
- Format: MP4, MOV
- Aspect ratio: 4:5 (portrait), 1:1 (square), 1.91:1 (landscape)
- Duration: 3-60 seconds (Reels), 3-10 minutes (Feed)
- Size: Max 100MB
- Resolution: Min 500px width

**Scheduled Publishing:**
```python
# Not directly supported - must use Facebook Creator Studio or queue task at publish time
```

**Rate Limits:** 25 calls/user/hour

---

### **3. Facebook Graph API**

**Status:** ⚠️ Stub only (needs implementation)

**OAuth Scopes:**
- `pages_manage_posts`
- `pages_read_engagement`
- `publish_video`

**Upload Endpoint (Resumable Upload):**

**Step 1: Initialize Upload**
```python
POST https://graph-video.facebook.com/v18.0/{page-id}/videos
{
    "upload_phase": "start",
    "file_size": 123456789,
    "access_token": "YOUR_ACCESS_TOKEN"
}

Response: {
    "video_id": "video_id",
    "upload_session_id": "session_id",
    "start_offset": 0,
    "end_offset": 999999
}
```

**Step 2: Upload Chunks**
```python
POST https://graph-video.facebook.com/v18.0/{page-id}/videos
{
    "upload_phase": "transfer",
    "upload_session_id": "session_id",
    "start_offset": 0,
    "video_file_chunk": <binary data>
}
```

**Step 3: Finalize Upload**
```python
POST https://graph-video.facebook.com/v18.0/{page-id}/videos
{
    "upload_phase": "finish",
    "upload_session_id": "session_id",
    "title": "Video Title",
    "description": "Video Description",
    "scheduled_publish_time": 1705764000,  # Unix timestamp
    "published": false,  # True for immediate, false for scheduled
    "access_token": "YOUR_ACCESS_TOKEN"
}
```

**Video Requirements:**
- Format: MP4, MOV
- Max size: 10GB
- Max length: 240 minutes
- Aspect ratio: Any

**Rate Limits:** 200 calls/hour/user

---

### **4. TikTok Content Posting API**

**Status:** ⚠️ Stub only (needs implementation)

**OAuth Scopes:**
- `video.upload`
- `video.publish`

**Upload Endpoint (Chunked Upload):**

**Step 1: Initialize Upload**
```python
POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/
{
    "post_info": {
        "title": "Video Title",
        "description": "Video description #hashtags",
        "privacy_level": "PUBLIC_TO_EVERYONE",  # or MUTUAL_FOLLOW_FRIENDS, SELF_ONLY
        "disable_duet": false,
        "disable_comment": false,
        "disable_stitch": false
    },
    "source_info": {
        "source": "FILE_UPLOAD",
        "video_size": 123456789,
        "chunk_size": 10485760  # 10MB chunks
    }
}

Response: {
    "data": {
        "publish_id": "publish_id",
        "upload_url": "https://upload-url.tiktokapis.com/..."
    }
}
```

**Step 2: Upload Chunks**
```python
PUT {upload_url}
Content-Type: video/mp4
Content-Range: bytes 0-10485759/123456789

<binary chunk data>
```

**Video Requirements:**
- Format: MP4, MOV, MPEG, 3GP, AVI
- Size: Max 4GB
- Duration: 3 seconds - 10 minutes
- Resolution: 360p - 4K
- Aspect ratio: 9:16 (recommended), 1:1, 16:9

**Scheduled Publishing:**
- ⚠️ **Not supported** by official API
- **Workaround:** Queue Celery task with ETA at scheduled time

**Rate Limits:** Varies by app, typically 100 calls/day

---

## 📋 FastAPI Implementation

### **Main App**

**File:** `app.py`

```python
from fastapi import FastAPI, HTTPException, Depends, status, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from typing import Optional, List
import uuid
from datetime import datetime

from .security import CredentialManager, require_auth
from .jobs_db import JobRepository, Job, JobStatus
from .tasks import launch_multi_platform_upload

app = FastAPI(
    title="Media Scheduler API",
    version="1.0.0",
    description="Multi-platform video scheduling system"
)

# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize services
creds = CredentialManager()
jobs = JobRepository()


# ===== Pydantic Models =====

class UploadRequest(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=255)
    title: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=5000)
    platforms: List[str] = Field(..., min_items=1, max_items=4)
    youtube_account_id: Optional[str] = None
    scheduled_time: Optional[datetime] = None
    
    @validator('platforms')
    def validate_platforms(cls, v):
        valid = {'youtube', 'instagram', 'facebook', 'tiktok'}
        if not set(v).issubset(valid):
            raise ValueError(f"Invalid platforms. Must be subset of {valid}")
        return v


class UploadResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobResponse(BaseModel):
    job_id: str
    user_id: str
    title: str
    platforms: List[str]
    status: str
    progress: int
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: datetime
    scheduled_time: Optional[datetime] = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: dict


# ===== Error Handlers =====

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": exc.status_code,
                "message": exc.detail,
                "details": None
            }
        }
    )


# ===== API Endpoints =====

@app.post("/api/v1/upload", status_code=status.HTTP_202_ACCEPTED, response_model=UploadResponse)
async def create_upload(
    payload: UploadRequest,
    user_id: str = Depends(require_auth)
):
    """
    Create a new video upload job.
    
    Returns 202 Accepted with job_id and Location header.
    """
    # Create job in database
    job_id = str(uuid.uuid4())
    job = Job(
        job_id=job_id,
        user_id=user_id,
        drive_file_id=payload.drive_file_id,
        title=payload.title,
        description=payload.description,
        platforms=payload.platforms,
        youtube_account_id=payload.youtube_account_id,
        status=JobStatus.PENDING,
        scheduled_time=payload.scheduled_time
    )
    jobs.create(job)
    
    # Queue Celery task
    if payload.scheduled_time:
        # Schedule for later (Celery ETA)
        eta = payload.scheduled_time
        launch_multi_platform_upload.apply_async(
            args=[job_id, payload.platforms],
            eta=eta
        )
    else:
        # Immediate
        launch_multi_platform_upload(job_id, payload.platforms)
    
    return UploadResponse(
        job_id=job_id,
        status="queued",
        message="Upload job queued successfully"
    ), {"Location": f"/api/v1/jobs/{job_id}"}


@app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
async def get_job_status(
    job_id: str,
    user_id: str = Depends(require_auth)
):
    """Get job status and results"""
    job = jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return JobResponse(**job.dict())


@app.get("/api/v1/jobs", response_model=List[JobResponse])
async def list_jobs(
    user_id: str = Depends(require_auth),
    status: Optional[str] = None,
    limit: int = 50
):
    """List user's jobs"""
    job_list = jobs.list(user_id, status, limit)
    return [JobResponse(**j.dict()) for j in job_list]


@app.delete("/api/v1/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    user_id: str = Depends(require_auth)
):
    """Cancel a pending job"""
    job = jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if job.status != JobStatus.PENDING:
        raise HTTPException(status_code=400, detail="Can only cancel pending jobs")
    
    jobs.update(job_id, {"status": JobStatus.CANCELLED})
    
    # TODO: Revoke Celery task
    
    return None


# ===== Health Check =====

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }


# ===== OpenAPI Docs =====
# Automatically available at:
# - /docs (Swagger UI)
# - /redoc (ReDoc)
```

---

## 🧪 Testing Strategy

### **Test Structure**

```
tests/
├── __init__.py
├── conftest.py              # Pytest fixtures
├── unit/
│   ├── test_security.py     # Encryption, redaction
│   ├── test_jobs_db.py      # JobRepository CRUD
│   ├── test_tasks.py        # Celery tasks (mocked)
│   └── test_platform_upload.py  # Platform uploaders (mocked)
├── integration/
│   ├── test_api.py          # FastAPI endpoints (TestClient)
│   ├── test_oauth_flow.py   # OAuth flow end-to-end
│   └── test_upload_flow.py  # Full upload workflow
└── e2e/
    └── test_full_workflow.py  # Selenium/Playwright UI tests
```

### **Example Unit Test**

**File:** `tests/unit/test_security.py`

```python
import pytest
from security import CredentialManager, SensitiveDataFilter
import logging

def test_encrypt_decrypt():
    cm = CredentialManager()
    
    original = "ya29.A0AfH6SMBxxxxx_refresh_token"
    encrypted = cm.encrypt(original)
    decrypted = cm.decrypt(encrypted)
    
    assert decrypted == original
    assert encrypted != original
    assert "ya29" not in encrypted


def test_logging_filter():
    logger = logging.getLogger("test")
    handler = logging.StreamHandler()
    handler.addFilter(SensitiveDataFilter())
    logger.addHandler(handler)
    
    # Should be redacted
    logger.info("access_token: ya29.A0AfH6SMBxxxxx")
    # Check log output contains [REDACTED]
```

### **Example Integration Test**

**File:** `tests/integration/test_api.py`

```python
import pytest
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_create_upload_success(mock_auth, mock_celery):
    response = client.post("/api/v1/upload", json={
        "drive_file_id": "1ABC123",
        "title": "Test Video",
        "platforms": ["youtube"]
    }, headers={"Authorization": "Bearer test_token"})
    
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "queued"
    assert "Location" in response.headers


def test_get_job_not_found(mock_auth):
    response = client.get("/api/v1/jobs/nonexistent", 
                         headers={"Authorization": "Bearer test_token"})
    
    assert response.status_code == 404
    data = response.json()
    assert data["success"] is False
```

### **Running Tests**

```bash
# Install test dependencies
pip install pytest pytest-cov pytest-asyncio httpx responses

# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test file
pytest tests/unit/test_security.py -v

# Run integration tests only
pytest tests/integration/ -v
```

### **CI/CD Pipeline (GitHub Actions)**

**File:** `.github/workflows/test.yml`

```yaml
name: Test Suite

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    services:
      redis:
        image: redis:7
        ports:
          - 6379:6379
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov
      
      - name: Run tests
        run: pytest --cov=. --cov-report=xml
      
      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./coverage.xml
```

---

## 🚀 Deployment Guide

### **Local Development**

```bash
# 1. Clone repo
cd /Users/quynhdiep/video-scheduler-dashboard/

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set environment variables
export USER_STORE_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export INSTAGRAM_ACCESS_TOKEN="your_token"
export FACEBOOK_ACCESS_TOKEN="your_token"
export TIKTOK_ACCESS_TOKEN="your_token"

# 5. Start Redis
redis-server

# 6. Start Celery worker (separate terminal)
celery -A celery_app worker --loglevel=info

# 7. Start Celery Beat (separate terminal)
celery -A celery_app beat --loglevel=info

# 8. Start FastAPI server
uvicorn app:app --reload --port 8770

# 9. Access dashboard
open http://localhost:8770
```

### **Docker Deployment**

**File:** `Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose port
EXPOSE 8770

# Start server
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8770"]
```

**File:** `docker-compose.yml`

```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
  
  celery-worker:
    build: .
    command: celery -A celery_app worker --loglevel=info --concurrency=4
    depends_on:
      - redis
    environment:
      - REDIS_URL=redis://redis:6379/0
      - USER_STORE_KEY=${USER_STORE_KEY}
    volumes:
      - ./data:/app/data
  
  celery-beat:
    build: .
    command: celery -A celery_app beat --loglevel=info
    depends_on:
      - redis
    environment:
      - REDIS_URL=redis://redis:6379/0
  
  web:
    build: .
    ports:
      - "8770:8770"
    depends_on:
      - redis
      - celery-worker
    environment:
      - REDIS_URL=redis://redis:6379/0
      - USER_STORE_KEY=${USER_STORE_KEY}
      - INSTAGRAM_ACCESS_TOKEN=${INSTAGRAM_ACCESS_TOKEN}
      - FACEBOOK_ACCESS_TOKEN=${FACEBOOK_ACCESS_TOKEN}
      - TIKTOK_ACCESS_TOKEN=${TIKTOK_ACCESS_TOKEN}
    volumes:
      - ./data:/app/data

volumes:
  redis-data:
```

**Deploy with Docker Compose:**

```bash
# Build and start all services
docker-compose up -d

# View logs
docker-compose logs -f web

# Stop services
docker-compose down
```

---

## 📚 Documentation & Resources

### **API Documentation**
- Auto-generated OpenAPI: `http://localhost:8770/docs`
- ReDoc: `http://localhost:8770/redoc`

### **External API Docs**
- [YouTube Data API v3](https://developers.google.com/youtube/v3)
- [Instagram Graph API](https://developers.facebook.com/docs/instagram-api)
- [Facebook Graph API](https://developers.facebook.com/docs/graph-api)
- [TikTok Content Posting API](https://developers.tiktok.com/doc/content-posting-api-get-started)

### **Tools**
- [Flower (Celery monitoring)](http://localhost:5555)
- [Redis Commander](https://github.com/joeferner/redis-commander)

---

## 📊 Success Metrics

| Metric | Target | Current |
|--------|--------|---------|
| **Security:** Plain-text credentials | 0 | ❌ Many |
| **Reliability:** Job persistence rate | 100% | ❌ 0% (in-memory) |
| **Performance:** API response time | < 200ms | ⚠️ Unknown |
| **Quality:** Test coverage | > 70% | ❌ 0% |
| **Availability:** Uptime | > 99% | ⚠️ Unknown |
| **Platform Coverage:** Working APIs | 4/4 (100%) | ✅ 1/4 (25%) |

---

**Last Updated:** Sprint 1 Start  
**Next Review:** End of Sprint 1 (Day 5)
