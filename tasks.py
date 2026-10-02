"""
Celery tasks for video upload operations.

Two properties drive the design here:

1. **One Drive download, N platform uploads.** The file is downloaded once and
   the per-platform tasks run in parallel from that single local copy.
2. **A downloaded file lives only on the machine that downloaded it.** Workers
   may be separate processes (or separate hosts), so a path handed to another
   worker is meaningless. Each platform task therefore *stays on the queue that
   holds the file*, and the fan-out is a group of tasks that each download what
   they need rather than a chord over a shared path.

Transient failures (429/5xx/network) are retried with exponential backoff;
permanent failures (bad credentials, deleted video) are recorded immediately so
they cannot burn five retries.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from celery import chord
from celery.exceptions import MaxRetriesExceededError

from celery_app import app
from jobs_db import JobRepository, JobStatus

DATA_DIR = Path(__file__).parent / "data"
job_repo = JobRepository(str(DATA_DIR / "jobs.db"))

logger = logging.getLogger(__name__)


class TransientAPIError(Exception):
    """A temporary failure (429, 5xx, timeout, network) worth retrying."""


class PermanentAPIError(Exception):
    """A permanent failure (4xx other than 429, missing credential) — no retry."""


def _classify(exc: Exception) -> Exception:
    """Map a platform exception onto the transient/permanent taxonomy.

    Args:
        exc: The exception raised by an upload helper.

    Returns:
        TransientAPIError | PermanentAPIError: The classified error. Google API
        errors expose a ``code``; anything without a recognisable 429/5xx code is
        treated as permanent so a bad credential does not retry five times.
    """
    code = getattr(exc, "code", None)
    if code is None:
        resp = getattr(exc, "resp", None)
        code = getattr(resp, "status", None) if resp is not None else None
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None

    message = str(exc).lower()
    if code == 429 or "rate limit" in message or "quota" in message:
        return TransientAPIError(f"Rate limited: {exc}")
    if code is not None and code >= 500:
        return TransientAPIError(f"Server error {code}: {exc}")
    if code is not None and 400 <= code < 500:
        return PermanentAPIError(f"Client error {code}: {exc}")
    return PermanentAPIError(str(exc))


@app.task(
    bind=True,
    name="tasks.upload_to_platform",
    max_retries=5,
    autoretry_for=(TransientAPIError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def upload_to_platform(self, job_id: str, platform: str) -> Dict[str, Any]:
    """Upload one job's video to a single platform, with retries.

    Args:
        job_id: Identifier of the job being executed.
        platform: One of ``youtube``, ``instagram``, ``facebook``, ``tiktok``.

    Returns:
        dict: ``{"platform", "status", "result"|"error", "attempts"}``.
    """
    attempts = (self.request.retries or 0) + 1
    logger.info("[%s] uploading to %s (attempt %s)", job_id, platform, attempts)

    job = job_repo.get(job_id)
    if not job:
        # Not retryable: the row is gone.
        return {
            "platform": platform,
            "status": "failed",
            "error": f"Job {job_id} not found",
            "attempts": attempts,
        }

    try:
        result = _run_platform_upload(job, platform)
        logger.info("[%s] %s upload succeeded", job_id, platform)
        return {
            "platform": platform,
            "status": "success",
            "result": result,
            "attempts": attempts,
        }
    except Exception as exc:  # classify, then retry or give up
        classified = _classify(exc)
        if isinstance(classified, TransientAPIError):
            logger.warning(
                "[%s] %s transient failure (retry %s/%s): %s",
                job_id, platform, self.request.retries, self.max_retries, exc,
            )
            try:
                raise self.retry(exc=classified)
            except MaxRetriesExceededError:
                return {
                    "platform": platform,
                    "status": "failed",
                    "error": f"Retries exhausted: {classified}",
                    "attempts": attempts,
                }
        logger.error("[%s] %s permanent failure: %s", job_id, platform, classified)
        return {
            "platform": platform,
            "status": "failed",
            "error": str(classified),
            "attempts": attempts,
        }


def _run_platform_upload(job: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """Download the job's video and hand it to one platform's uploader.

    Args:
        job: The job row.
        platform: Target platform name.

    Returns:
        dict: The platform uploader's result payload.

    Raises:
        PermanentAPIError: If the platform is unknown or its credential is absent.
    """
    from platform_upload import (
        download_from_drive_for_user,
        get_platform_token,
        upload_to_facebook,
        upload_to_instagram,
        upload_to_tiktok,
        upload_to_youtube,
    )

    user_id = job["user_id"]
    file_id = job["drive_file_id"]
    title = job.get("title") or "Untitled"
    description = job.get("description") or ""
    privacy = job.get("privacy") or "private"

    if platform not in ("youtube", "instagram", "facebook", "tiktok"):
        raise PermanentAPIError(f"Unknown platform: {platform}")

    # Fail fast on a missing credential rather than downloading the whole video.
    if platform != "youtube" and not get_platform_token(user_id, platform):
        raise PermanentAPIError(
            f"No {platform} credential configured for {user_id}"
        )

    def report_progress(pct: int) -> None:
        """Fold download progress into the job's 0-50 band."""
        job_repo.update(job["job_id"], {"progress": int(pct * 0.5)})

    video_path = None
    try:
        video_path = download_from_drive_for_user(
            user_id, file_id, progress_cb=report_progress
        )
        job_repo.update(job["job_id"], {"progress": 50})

        common = (video_path, title, description, privacy)
        if platform == "youtube":
            return upload_to_youtube(
                *common,
                user_id=user_id,
                youtube_account_id=job.get("youtube_account_id"),
            )
        if platform == "instagram":
            return upload_to_instagram(*common, user_id=user_id)
        if platform == "facebook":
            return upload_to_facebook(*common, user_id=user_id)
        return upload_to_tiktok(*common, user_id=user_id)
    finally:
        if video_path and os.path.exists(video_path):
            try:
                os.unlink(video_path)
            except OSError as e:
                logger.warning(
                    "[%s] could not remove temp file %s: %s",
                    job["job_id"], video_path, e,
                )


@app.task(name="tasks.aggregate_results", bind=True)
def aggregate_results(self, results: List[Dict[str, Any]], job_id: str) -> Dict[str, Any]:
    """Fold per-platform task results into the job's final state.

    A job that succeeded on some platforms but not others is recorded as
    ``PARTIALLY_COMPLETED`` — finished, but the operator must know.

    Args:
        results: List of per-platform result dicts.
        job_id: The job to finalise.

    Returns:
        dict: ``{"job_id", "status", "results", "succeeded", "failed"}``.
    """
    logger.info("[%s] aggregating %s platform result(s)", job_id, len(results))

    by_platform = {r.get("platform"): r for r in results if r}
    succeeded = [p for p, r in by_platform.items() if r.get("status") == "success"]
    failed = [p for p, r in by_platform.items() if r.get("status") != "success"]

    if not results:
        final_status = JobStatus.FAILED
    elif not failed:
        final_status = JobStatus.COMPLETED
    elif succeeded:
        final_status = JobStatus.PARTIALLY_COMPLETED
    else:
        final_status = JobStatus.FAILED

    job_repo.update(
        job_id,
        {
            "status": final_status,
            "progress": 100,
            "result": by_platform,
            "error": (
                f"Failed on: {', '.join(sorted(failed))}" if failed else None
            ),
        },
    )
    logger.info("[%s] final status %s (ok=%s failed=%s)",
                job_id, final_status, sorted(succeeded), sorted(failed))
    return {
        "job_id": job_id,
        "status": final_status,
        "results": by_platform,
        "succeeded": sorted(succeeded),
        "failed": sorted(failed),
    }


def launch_multi_platform_upload(job_id: str, platforms: List[str]):
    """Fan a job out to one parallel task per platform.

    Each task downloads the file itself, so the fan-out works across processes
    and hosts without sharing a filesystem path.

    Args:
        job_id: Job to execute.
        platforms: Target platform names.

    Returns:
        celery.result.GroupResult: Handle for the fan-out group.

    Raises:
        PermanentAPIError: If the job does not exist.
    """
    job = job_repo.get(job_id)
    if not job:
        raise PermanentAPIError(f"Job {job_id} not found")

    platforms = [p.lower() for p in (platforms or ["youtube"])]
    logger.info("[%s] fanning out to %s", job_id, platforms)

    signatures = [
        upload_to_platform.s(job_id, platform) for platform in platforms
    ]

    # In eager mode the signatures have already run; aggregate inline.
    if app.conf.task_always_eager:
        results = [sig.get() for sig in signatures]
        aggregate_results.apply(args=(results, job_id))
        return results

    # A chord is the correct primitive: Celery stores the callback in the result
    # backend, so it fires even though the producer and workers are different
    # processes. Nothing is cached client-side -- an earlier version kept the
    # chord in a module global, which cannot survive the hop to the worker.
    #
    # ``on_error`` is not used: every upload task returns normally (recording its
    # own failure), so the callback always runs.
    return chord(signatures, aggregate_results.s(job_id)).apply_async()


@app.task(name="tasks.process_scheduled_jobs")
def process_scheduled_jobs() -> Dict[str, Any]:
    """Claim and dispatch every job whose ``scheduled_time`` has arrived.

    Claiming is atomic, so a job already picked up by the in-process scheduler
    in ``server_secure`` is skipped rather than run twice.

    Returns:
        dict: ``{"due", "dispatched", "skipped"}``.
    """
    due = job_repo.get_due_jobs()
    dispatched = []
    skipped = []

    for job in due:
        if job["status"] != JobStatus.PENDING:
            continue
        if not job_repo.claim(job["job_id"]):
            skipped.append(job["job_id"])
            continue
        try:
            launch_multi_platform_upload(job["job_id"], job.get("platforms") or ["youtube"])
            dispatched.append(job["job_id"])
        except Exception as e:
            logger.error("[%s] dispatch failed: %s", job["job_id"], e)
            job_repo.update(
                job["job_id"], {"status": JobStatus.FAILED, "error": str(e)}
            )

    logger.info(
        "scheduled sweep: %s due, %s dispatched, %s skipped",
        len(due), len(dispatched), len(skipped),
    )
    return {"due": len(due), "dispatched": dispatched, "skipped": skipped}


@app.task(name="tasks.cleanup_old_jobs")
def cleanup_old_jobs(days: int = 30) -> int:
    """Delete completed/failed jobs older than ``days``.

    Args:
        days: Age threshold in days.

    Returns:
        int: Number of jobs removed.
    """
    removed = job_repo.cleanup_old_jobs(days=days)
    logger.info("cleanup removed %s old job(s)", removed)
    return removed