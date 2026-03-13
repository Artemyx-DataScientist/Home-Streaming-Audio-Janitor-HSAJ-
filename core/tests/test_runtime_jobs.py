from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from hsaj.blocking import BlockedObject
from hsaj.config import (
    BridgeConfig,
    DatabaseConfig,
    HsajConfig,
    PathsConfig,
    RuntimeConfig,
)
from hsaj.db import init_database
from hsaj.db.models import BlockCandidate, RuntimeJobStatus
from hsaj.roon import BridgeClientError
from hsaj.runtime_jobs import JOB_BLOCKED_SYNC, JOB_CLEANUP, run_blocked_sync_job, run_cleanup_job


def _config(tmp_path: Path) -> HsajConfig:
    return HsajConfig(
        database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
        paths=PathsConfig(
            library_roots=[tmp_path / "library"],
            quarantine_dir=tmp_path / "quarantine",
            atmos_dir=tmp_path / "atmos",
        ),
        bridge=BridgeConfig(http_url="http://bridge.invalid"),
        runtime=RuntimeConfig(enable_background_jobs=True),
    )


def test_run_blocked_sync_job_updates_candidates_and_runtime_status(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    engine, _ = init_database(config.database)

    monkeypatch.setattr(
        "hsaj.runtime_jobs.fetch_blocked_snapshot_from_bridge",
        lambda base_url=None: type(
            "Snapshot",
            (),
            {
                "items": [
                    BlockedObject(object_type="artist", object_id="artist-1", artist="Artist")
                ],
                "contract_version": "v2",
                "generated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "source_mode": "inline_json",
                "item_count": 1,
            },
        )(),
    )

    with Session(engine) as session:
        payload = run_blocked_sync_job(session, config)

        assert payload["status"] == "ok"
        assert payload["item_count"] == 1
        assert session.query(BlockCandidate).count() == 1
        job_status = session.get(RuntimeJobStatus, JOB_BLOCKED_SYNC)
        assert job_status is not None
        assert job_status.status == "ok"


def test_run_cleanup_job_records_runtime_status(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        payload = run_cleanup_job(session, config)

        assert payload["status"] == "ok"
        job_status = session.get(RuntimeJobStatus, JOB_CLEANUP)
        assert job_status is not None
        assert job_status.status == "ok"


def test_run_blocked_sync_job_rejects_contract_mismatch(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    engine, _ = init_database(config.database)

    monkeypatch.setattr(
        "hsaj.runtime_jobs.fetch_blocked_snapshot_from_bridge",
        lambda base_url=None: type(
            "Snapshot",
            (),
            {
                "items": [
                    BlockedObject(object_type="artist", object_id="artist-1", artist="Artist")
                ],
                "contract_version": "legacy-v1",
                "generated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "source_mode": "inline_json",
                "item_count": 1,
            },
        )(),
    )

    with Session(engine) as session:
        try:
            run_blocked_sync_job(session, config)
            raise AssertionError("Expected blocked contract mismatch")
        except BridgeClientError as exc:
            assert "Blocked contract mismatch" in str(exc)

        job_status = session.get(RuntimeJobStatus, JOB_BLOCKED_SYNC)
        assert job_status is not None
        assert job_status.status == "error"
        assert "Blocked contract mismatch" in (job_status.last_error or "")
