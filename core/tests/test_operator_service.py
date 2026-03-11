from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from hsaj.config import DatabaseConfig, HsajConfig, PathsConfig
from hsaj.db import init_database
from hsaj.db.models import RoonItemCache
from hsaj.operator_service import apply_preview_payload, plan_preview_payload, stats_payload


def _base_config(tmp_path: Path) -> HsajConfig:
    return HsajConfig(
        database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
        paths=PathsConfig(
            library_roots=[tmp_path / "library"],
            quarantine_dir=tmp_path / "quarantine",
            atmos_dir=tmp_path / "atmos",
        ),
    )


def test_preview_and_apply_payload_round_trip(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    library_file = config.paths.library_roots[0] / "Artist/Album/track.flac"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("content")

    with Session(engine) as session:
        from hsaj.db.models import BlockCandidate, File

        session.add(
            File(
                path=str(library_file),
                size_bytes=1,
                format="flac",
                mtime=datetime.now(timezone.utc),
                artist="Artist",
                album="Album",
                title="Title",
                track_number=1,
                year=2024,
                duration_seconds=300,
            )
        )
        session.add(
            BlockCandidate(
                object_type="track",
                object_id="track-1",
                label="Track",
                metadata_json='{"artist":"Artist","album":"Album","title":"Title","track_number":1,"duration_ms":300000}',
                reason="blocked_by_track",
                status="planned",
                source="bridge.blocked.v1",
                rule_id="blocked_by_track",
                first_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                last_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                planned_action_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                last_transition_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            RoonItemCache(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                track_number=1,
                duration_ms=300_000,
            )
        )
        session.commit()

        preview = plan_preview_payload(session, config)
        assert preview["preview_id"]
        assert preview["plan"]["blocked_quarantine_due"]

        apply_result = apply_preview_payload(
            session,
            config,
            preview_id=preview["preview_id"],
            dry_run=False,
        )
        assert apply_result["preview_id"] == preview["preview_id"]

        stats = stats_payload(session, config)
        assert stats["candidates"]["quarantined"] == 1
