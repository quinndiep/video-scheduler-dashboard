"""
Job persistence layer for the video scheduler using SQLite and SQLAlchemy.

Replaces the previous in-memory ``upload_jobs`` dictionary so that scheduled and
in-flight uploads survive a server restart.

The module is intentionally free of any HTTP or credential logic – it only knows
how to create, read, update and delete *jobs*.  Uploading itself is handled by
``platform_upload`` and the request handlers in ``server_secure.py``.
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DB_PATH = str(DATA_DIR / "jobs.db")


class JobStatus:
    """Job status constants.

    A job starts as ``PENDING``; the scheduler flips it to ``PROCESSING`` when it
    starts uploading and finally to one of the terminal states.

    ``PARTIALLY_COMPLETED`` is used when a multi-platform upload succeeds on some
    platforms and fails on others — the job is finished, but the operator needs
    to know not everything went through.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = (
        PENDING,
        PROCESSING,
        COMPLETED,
        PARTIALLY_COMPLETED,
        FAILED,
        CANCELLED,
    )
    TERMINAL = (COMPLETED, PARTIALLY_COMPLETED, FAILED, CANCELLED)


class JobModel(Base):
    """SQLAlchemy model for the ``jobs`` table."""

    __tablename__ = "jobs"

    job_id = Column(String(36), primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    drive_file_id = Column(String(255), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    platforms = Column(Text, nullable=False, default="[]")  # JSON array
    youtube_account_id = Column(String(64), nullable=True)
    status = Column(String(50), nullable=False, default=JobStatus.PENDING, index=True)
    progress = Column(Integer, default=0)
    result = Column(Text, nullable=True)  # JSON object
    error = Column(Text, nullable=True)
    privacy = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    scheduled_time = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        Index("idx_user_status", "user_id", "status"),
        Index("idx_scheduled_status", "scheduled_time", "status"),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation of the job."""
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "drive_file_id": self.drive_file_id,
            "title": self.title,
            "description": self.description,
            "platforms": json.loads(self.platforms) if self.platforms else [],
            "youtube_account_id": self.youtube_account_id,
            "status": self.status,
            "progress": self.progress,
            "result": json.loads(self.result) if self.result else None,
            "error": self.error,
            "privacy": self.privacy,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "scheduled_time": (
                self.scheduled_time.isoformat() if self.scheduled_time else None
            ),
        }


def _parse_dt(value: Any) -> Optional[datetime]:
    """Convert an ISO string / ``datetime`` / ``None`` to a UTC‑naive datetime.

    Every datetime in the jobs table is stored **UTC‑naive** so that plain
    comparisons against :func:`datetime.utcnow` are always valid — mixing naive
    and aware values raises ``TypeError`` in Python. Timezone‑aware inputs (such
    as the ``Z``‑suffixed strings the browser sends) are converted to UTC before
    the tzinfo is stripped.

    Args:
        value: An ISO‑8601 string, a ``datetime``, or ``None``.

    Returns:
        datetime | None: The parsed UTC‑naive value, or ``None`` when the input
        was ``None`` or could not be parsed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class JobRepository:
    """Repository for job CRUD operations.

    Every method opens its own short-lived session, which keeps the repository
    safe to call from the scheduler thread and from request handler threads.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        """Initialize the repository and create the table if needed.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        logging.info(f"JobRepository initialized with database: {self.db_path}")

    def _get_session(self):
        """Return a new database session."""
        return self.SessionLocal()

    # ------------------------------------------------------------------
    # Create / read / update / delete
    # ------------------------------------------------------------------
    def create(self, job_data: Dict[str, Any]) -> str:
        """Create a new job.

        Args:
            job_data: Mapping with the job fields. ``job_id`` is optional; when
                omitted a UUID4 is generated.

        Returns:
            str: The created ``job_id``.
        """
        session = self._get_session()
        try:
            job_id = job_data.get("job_id") or str(uuid.uuid4())
            job = JobModel(
                job_id=job_id,
                user_id=job_data["user_id"],
                drive_file_id=job_data["drive_file_id"],
                title=job_data["title"],
                description=job_data.get("description"),
                platforms=json.dumps(job_data.get("platforms") or []),
                youtube_account_id=job_data.get("youtube_account_id"),
                status=job_data.get("status", JobStatus.PENDING),
                progress=job_data.get("progress", 0),
                result=(
                    json.dumps(job_data["result"])
                    if job_data.get("result") is not None
                    else None
                ),
                error=job_data.get("error"),
                privacy=job_data.get("privacy"),
                scheduled_time=_parse_dt(job_data.get("scheduled_time")),
            )
            session.add(job)
            session.commit()
            logging.info(f"Created job {job_id}")
            return job_id
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to create job: {e}")
            raise
        finally:
            session.close()

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a single job as a dict, or ``None`` when not found."""
        session = self._get_session()
        try:
            job = session.query(JobModel).filter(JobModel.job_id == job_id).first()
            return job.to_dict() if job else None
        finally:
            session.close()

    def update(self, job_id: str, updates: Dict[str, Any]) -> bool:
        """Update mutable fields on a job.

        Args:
            job_id: Identifier of the job to update.
            updates: Field/value pairs. ``platforms`` (list) and ``result``
                (dict) are JSON-encoded automatically.

        Returns:
            bool: ``True`` when a row was updated, ``False`` when not found.
        """
        session = self._get_session()
        try:
            job = session.query(JobModel).filter(JobModel.job_id == job_id).first()
            if not job:
                logging.warning(f"Cannot update unknown job {job_id}")
                return False
            for key, value in updates.items():
                if key == "platforms" and isinstance(value, list):
                    value = json.dumps(value)
                elif key == "result" and isinstance(value, (dict, list)):
                    value = json.dumps(value)
                elif key == "scheduled_time":
                    value = _parse_dt(value)
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = datetime.utcnow()
            session.commit()
            logging.info(f"Updated job {job_id}")
            return True
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to update job {job_id}: {e}")
            raise
        finally:
            session.close()

    def delete(self, job_id: str) -> bool:
        """Delete a job. Returns ``True`` when a row was removed."""
        session = self._get_session()
        try:
            job = session.query(JobModel).filter(JobModel.job_id == job_id).first()
            if not job:
                return False
            session.delete(job)
            session.commit()
            logging.info(f"Deleted job {job_id}")
            return True
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to delete job {job_id}: {e}")
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def list(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List jobs, newest first.

        Args:
            user_id: Restrict to a single user; ``None`` lists all users.
            status: Optional status filter.
            limit: Maximum number of rows to return.
        """
        session = self._get_session()
        try:
            query = session.query(JobModel)
            if user_id:
                query = query.filter(JobModel.user_id == user_id)
            if status:
                query = query.filter(JobModel.status == status)
            rows = query.order_by(JobModel.created_at.desc()).limit(limit).all()
            return [job.to_dict() for job in rows]
        finally:
            session.close()

    def claim(self, job_id: str) -> bool:
        """Atomically move a pending job to ``PROCESSING``.

        The update is conditional on the row still being ``PENDING``, so two
        callers racing for the same job (e.g. the scheduler loop and a manual
        "upload now" request) cannot both win: the loser sees ``False``.

        Args:
            job_id: Identifier of the job to claim.

        Returns:
            bool: ``True`` if this caller claimed the job, ``False`` if it was
            already claimed or does not exist.
        """
        session = self._get_session()
        try:
            claimed = (
                session.query(JobModel)
                .filter(
                    JobModel.job_id == job_id,
                    JobModel.status == JobStatus.PENDING,
                )
                .update(
                    {
                        "status": JobStatus.PROCESSING,
                        "progress": 0,
                        "updated_at": datetime.utcnow(),
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            return bool(claimed)
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to claim job {job_id}: {e}")
            raise
        finally:
            session.close()

    def cancel(self, job_id: str) -> bool:
        """Atomically cancel a job that is still ``PENDING``.

        Mirrors :meth:`claim`: the UPDATE is conditional on the row still being
        ``PENDING``, so a cancel that races the scheduler's claim cannot cancel a
        job that has already started running.

        Args:
            job_id: Identifier of the job to cancel.

        Returns:
            bool: ``True`` if this caller cancelled the job, ``False`` if it was
            no longer pending or does not exist.
        """
        session = self._get_session()
        try:
            cancelled = (
                session.query(JobModel)
                .filter(
                    JobModel.job_id == job_id,
                    JobModel.status == JobStatus.PENDING,
                )
                .update(
                    {
                        "status": JobStatus.CANCELLED,
                        "error": "Cancelled by user",
                        "updated_at": datetime.utcnow(),
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if cancelled:
                logging.info(f"Cancelled job {job_id}")
            return bool(cancelled)
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to cancel job {job_id}: {e}")
            raise
        finally:
            session.close()

    def get_pending_jobs(self) -> List[Dict[str, Any]]:
        """Return all ``PENDING`` jobs ordered by creation time."""
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel)
                .filter(JobModel.status == JobStatus.PENDING)
                .order_by(JobModel.created_at)
                .all()
            )
            return [job.to_dict() for job in rows]
        finally:
            session.close()

    def get_due_jobs(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Return pending jobs whose ``scheduled_time`` is due.

        Jobs with no ``scheduled_time`` are considered due immediately, which
        matches the old behaviour where approving a schedule queued it right
        away.
        """
        session = self._get_session()
        try:
            now = now or datetime.utcnow()
            rows = (
                session.query(JobModel)
                .filter(JobModel.status == JobStatus.PENDING)
                .all()
            )
            due = [
                job.to_dict()
                for job in rows
                if job.scheduled_time is None or job.scheduled_time <= now
            ]
            due.sort(key=lambda j: (j["scheduled_time"] or "", j["created_at"] or ""))
            return due
        finally:
            session.close()

    def get_running_jobs(self) -> List[Dict[str, Any]]:
        """Return jobs left in ``PROCESSING`` state (crashed mid-upload)."""
        session = self._get_session()
        try:
            rows = (
                session.query(JobModel)
                .filter(JobModel.status == JobStatus.PROCESSING)
                .all()
            )
            return [job.to_dict() for job in rows]
        finally:
            session.close()

    def cleanup_old_jobs(self, days: int = 30) -> int:
        """Delete completed/failed jobs older than ``days``.

        Args:
            days: Age threshold in days.

        Returns:
            int: Number of jobs removed.
        """
        session = self._get_session()
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            removed = (
                session.query(JobModel)
                .filter(
                    JobModel.status.in_([JobStatus.COMPLETED, JobStatus.FAILED]),
                    JobModel.created_at < cutoff,
                )
                .delete(synchronize_session=False)
            )
            session.commit()
            logging.info(f"Cleaned up {removed} old jobs")
            return removed
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to cleanup old jobs: {e}")
            raise
        finally:
            session.close()


def _self_test(db_path: str = ":memory:") -> None:
    """Exercise every repository method against a throwaway database."""
    repo = JobRepository(db_path)
    job_id = repo.create(
        {
            "user_id": "test_user",
            "drive_file_id": "file_123",
            "title": "Test Video",
            "platforms": ["youtube", "instagram"],
            "scheduled_time": datetime.utcnow().isoformat(),
        }
    )
    print("Created job:", job_id)

    fetched = repo.get(job_id)
    print("Platforms round-trip:", fetched["platforms"])
    assert fetched["platforms"] == ["youtube", "instagram"]

    repo.update(job_id, {"status": JobStatus.PROCESSING, "progress": 50})
    print("Progress after update:", repo.get(job_id)["progress"])

    repo.update(
        job_id,
        {"status": JobStatus.COMPLETED, "progress": 100, "result": {"youtube": "ok"}},
    )
    done = repo.get(job_id)
    print("Result round-trip:", done["result"], "status:", done["status"])

    print("Listed jobs:", len(repo.list("test_user")))
    print("Due jobs:", len(repo.get_due_jobs()))
    print("Deleted:", repo.delete(job_id))
    print("Self-test passed.")


def _claim_race_test(db_path: str | None = None) -> None:
    """Prove that exactly one of many concurrent claimers wins a pending job.

    Uses a temporary on-disk database because SQLite gives each thread its *own*
    ``:memory:`` database (SingletonThreadPool), which would make the threads
    contend on separate databases and hide the race entirely.
    """
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db", prefix="jobs_race_")
    os.close(fd)
    os.unlink(path)  # let SQLAlchemy create it
    repo = JobRepository(path)
    try:
        job_id = repo.create(
            {
                "user_id": "race_user",
                "drive_file_id": "file_race",
                "title": "Race Video",
                "platforms": ["youtube"],
                "status": JobStatus.PENDING,
            }
        )

        winners = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)
        errors = []

        def contend():
            barrier.wait()  # maximise the overlap between the 8 threads
            try:
                got = repo.claim(job_id)
            except Exception as e:  # surface, don't silently pass
                with lock:
                    errors.append(repr(e))
                return
            if got:
                with lock:
                    winners.append(threading.current_thread().name)

        threads = [
            threading.Thread(target=contend, name=f"claimer-{i}") for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_row = repo.get(job_id)
        final = final_row["status"] if final_row else None
        print(f"Claim race: {len(winners)} winner(s) out of 8 -> {winners}")
        print("Final status:", final)
        assert not errors, f"claim raised: {errors}"
        assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
        assert final == JobStatus.PROCESSING
        # A second claim on an already-claimed job must fail.
        assert repo.claim(job_id) is False
        print("Claim race test passed.\n")
    finally:
        repo.engine.dispose()
        for suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)


def _claim_sequential_test(db_path: str = ":memory:") -> None:
    """Two sequential claim attempts: the first wins, the second is a no-op."""
    repo = JobRepository(db_path)
    job_id = repo.create(
        {
            "user_id": "seq_user",
            "drive_file_id": "file_seq",
            "title": "Sequential Video",
            "platforms": ["youtube"],
            "status": JobStatus.PENDING,
        }
    )
    first = repo.claim(job_id)
    second = repo.claim(job_id)
    print("Sequential claims:", first, second)
    assert first is True and second is False
    print("Sequential claim test passed.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _self_test()
    print()
    _claim_sequential_test()
    _claim_race_test()