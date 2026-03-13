from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .blocking import (
    BridgeClientError,
    ensure_blocked_contract_version,
    fetch_blocked_snapshot_from_bridge,
    record_blocked_sync_failure,
    record_blocked_sync_success,
    sync_blocked_objects,
)
from .config import HsajConfig
from .db.models import RuntimeJobStatus
from .executor import cleanup_retention
from .timeutils import utc_now

logger = logging.getLogger(__name__)

JOB_BLOCKED_SYNC = "blocked_sync"
JOB_CLEANUP = "cleanup_retention"


def list_runtime_job_statuses(session: Session) -> list[RuntimeJobStatus]:
    return session.query(RuntimeJobStatus).order_by(RuntimeJobStatus.job_name.asc()).all()


def run_blocked_sync_job(session: Session, config: HsajConfig) -> dict[str, Any]:
    attempted_at = utc_now()
    try:
        snapshot = fetch_blocked_snapshot_from_bridge(base_url=config.bridge.http_url)
        ensure_blocked_contract_version(
            snapshot,
            expected_contract=config.bridge.contract_version,
        )
        record_blocked_sync_success(session, snapshot=snapshot, attempted_at=attempted_at)
        result = sync_blocked_objects(
            session=session,
            blocked_items=snapshot.items,
            grace_period_days=config.policy.block_grace_days,
            seen_at=attempted_at,
        )
        payload = {
            "job_name": JOB_BLOCKED_SYNC,
            "status": "ok",
            "contract_version": snapshot.contract_version or "legacy",
            "source_mode": snapshot.source_mode or "unknown",
            "item_count": snapshot.item_count,
            "raw_created": result.raw_created,
            "raw_updated": result.raw_updated,
            "candidates_created": result.candidates_created,
            "candidates_restored": result.candidates_restored,
        }
        _record_runtime_job_result(
            session,
            job_name=JOB_BLOCKED_SYNC,
            status="ok",
            attempted_at=attempted_at,
            result=payload,
        )
        session.commit()
        return payload
    except BridgeClientError as exc:
        record_blocked_sync_failure(session, error=str(exc), attempted_at=attempted_at)
        _record_runtime_job_result(
            session,
            job_name=JOB_BLOCKED_SYNC,
            status="error",
            attempted_at=attempted_at,
            error=str(exc),
        )
        session.commit()
        raise


def run_cleanup_job(session: Session, config: HsajConfig) -> dict[str, Any]:
    attempted_at = utc_now()
    result = cleanup_retention(session=session, config=config, request_id=f"runtime:{JOB_CLEANUP}")
    payload = {
        "job_name": JOB_CLEANUP,
        "status": "ok",
        "deleted_candidates": result.deleted_candidates,
        "expired_candidates": result.expired_candidates,
    }
    _record_runtime_job_result(
        session,
        job_name=JOB_CLEANUP,
        status="ok",
        attempted_at=attempted_at,
        result=payload,
    )
    session.commit()
    return payload


def _record_runtime_job_result(
    session: Session,
    *,
    job_name: str,
    status: str,
    attempted_at,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> RuntimeJobStatus:
    record = session.get(RuntimeJobStatus, job_name)
    if record is None:
        record = RuntimeJobStatus(job_name=job_name, status=status)
        session.add(record)

    record.status = status
    record.last_attempt_at = attempted_at
    record.last_error = error
    record.last_result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
    if status == "ok":
        record.last_success_at = attempted_at
    return record


class BackgroundScheduler:
    def __init__(self, *, engine: Engine, config: HsajConfig):
        self._engine = engine
        self._config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        now = utc_now()
        self._next_runs: dict[str, object] = {
            JOB_BLOCKED_SYNC: (
                now
                if config.runtime.blocked_sync_on_start
                else now + timedelta(minutes=config.runtime.blocked_sync_interval_minutes)
            ),
            JOB_CLEANUP: (
                now
                if config.runtime.cleanup_on_start
                else now + timedelta(minutes=config.runtime.cleanup_interval_minutes)
            ),
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="hsaj-runtime-jobs", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def run_job_now(self, job_name: str) -> dict[str, Any]:
        return self._run_job(job_name)

    def _run_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            now = utc_now()
            due_jobs: list[str] = []
            with self._lock:
                for job_name, next_run in self._next_runs.items():
                    if next_run <= now:
                        due_jobs.append(job_name)

            for job_name in due_jobs:
                try:
                    self._run_job(job_name)
                except Exception as exc:  # pragma: no cover - defensive scheduler loop
                    logger.warning("Runtime job %s failed: %s", job_name, exc)
                finally:
                    with self._lock:
                        self._next_runs[job_name] = self._next_due(job_name)

    def _run_job(self, job_name: str) -> dict[str, Any]:
        with Session(self._engine) as session:
            if job_name == JOB_BLOCKED_SYNC:
                payload = run_blocked_sync_job(session, self._config)
            elif job_name == JOB_CLEANUP:
                payload = run_cleanup_job(session, self._config)
            else:
                raise KeyError(job_name)
        with self._lock:
            self._next_runs[job_name] = self._next_due(job_name)
        return payload

    def _next_due(self, job_name: str):
        if job_name == JOB_BLOCKED_SYNC:
            return utc_now() + timedelta(minutes=self._config.runtime.blocked_sync_interval_minutes)
        if job_name == JOB_CLEANUP:
            return utc_now() + timedelta(minutes=self._config.runtime.cleanup_interval_minutes)
        raise KeyError(job_name)
