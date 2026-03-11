from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from hsaj.config import DatabaseConfig, HsajConfig, PathsConfig
from hsaj.db import init_database
from hsaj.db.models import ActionLog, BlockCandidate, File, RoonItemCache
from hsaj.executor import apply_plan, restore_from_quarantine
from hsaj.planner import Plan, build_plan
from hsaj.roon import RoonTrack


def _base_config(tmp_path: Path) -> HsajConfig:
    return HsajConfig(
        database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
        paths=PathsConfig(
            library_roots=[tmp_path / "library"],
            quarantine_dir=tmp_path / "quarantine",
            atmos_dir=tmp_path / "atmos",
        ),
    )


def _create_file(session: Session, path: Path) -> File:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content")
    file_record = File(
        path=str(path),
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
    session.add(file_record)
    session.commit()
    return file_record


def _add_candidate(
    session: Session,
    *,
    object_type: str = "track",
    object_id: str = "track-1",
) -> BlockCandidate:
    candidate = BlockCandidate(
        object_type=object_type,
        object_id=object_id,
        label=None,
        reason=f"blocked_by_{object_type}",
        status="planned",
        first_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        last_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        planned_action_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    session.add(candidate)
    session.commit()
    return candidate


def _cache_track(session: Session, track: RoonTrack) -> None:
    cache = RoonItemCache(
        roon_track_id=track.roon_track_id,
        artist=track.artist,
        album=track.album,
        title=track.title,
        duration_ms=track.duration_ms,
        track_number=track.track_number,
    )
    session.add(cache)
    session.commit()


def _build_plan(session: Session, config: HsajConfig) -> Plan:
    return build_plan(
        session=session,
        config=config,
        now=datetime(2024, 1, 10, tzinfo=timezone.utc),
    )


def test_plan_contains_sections_and_file_ids(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        file_record = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.flac",
        )
        _cache_track(
            session,
            RoonTrack(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                duration_ms=300_000,
                track_number=1,
            ),
        )
        candidate = _add_candidate(session)

        plan = _build_plan(session, config)

    assert plan.blocked_quarantine_due
    move = plan.blocked_quarantine_due[0]
    assert move.file_id == file_record.id
    assert move.candidate_id == candidate.id
    assert move.destination == (
        config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
    )
    assert plan.to_dict()["blocked_quarantine_due"][0]["file_id"] == file_record.id


def test_apply_plan_moves_to_quarantine_and_logs(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        original_path = config.paths.library_roots[0] / "Artist/Album/track.flac"
        source_file = _create_file(session, path=original_path)
        _cache_track(
            session,
            RoonTrack(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                duration_ms=300_000,
                track_number=1,
            ),
        )
        candidate = _add_candidate(session)
        plan = _build_plan(session, config)
        result = apply_plan(session=session, config=config, plan=plan)

        assert result.quarantined
        destination = (
            config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
        )
        assert destination.exists()
        assert not original_path.exists()

        refreshed_candidate = session.get(BlockCandidate, candidate.id)
        assert refreshed_candidate is not None
        assert refreshed_candidate.status == "quarantined"

        refreshed_file = session.get(File, source_file.id)
        assert refreshed_file is not None
        assert refreshed_file.path == str(destination)

        plan_log = session.query(ActionLog).filter(ActionLog.action == "plan").one()
        quarantine_log = (
            session.query(ActionLog).filter(ActionLog.action == "quarantine_move").one()
        )
        assert '"blocked_quarantine_due"' in (plan_log.details or "")
        assert "candidate_id" in (quarantine_log.details or "")

        second_plan = _build_plan(session, config)
        second_result = apply_plan(session=session, config=config, plan=second_plan)
        assert not second_result.quarantined


def test_apply_plan_dry_run_logs_plan_and_dry_run(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        _create_file(
            session, path=config.paths.library_roots[0] / "Artist/Album/track.flac"
        )
        _cache_track(
            session,
            RoonTrack(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                duration_ms=300_000,
                track_number=1,
            ),
        )
        _add_candidate(session)
        plan = _build_plan(session, config)
        result = apply_plan(session=session, config=config, plan=plan, dry_run=True)
        assert result.dry_run is True
        assert session.query(ActionLog).filter(ActionLog.action == "plan").count() == 1
        assert session.query(ActionLog).filter(ActionLog.action == "dry_run").count() == 1
        destination = (
            config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
        )
        assert not destination.exists()


def test_plan_treats_stored_atmos_flag_as_immune(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        file_record = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.flac",
        )
        file_record.atmos_detected = True
        session.commit()
        _cache_track(
            session,
            RoonTrack(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                duration_ms=300_000,
                track_number=1,
            ),
        )
        candidate = _add_candidate(session)

        plan = _build_plan(session, config)

        assert plan.blocked_quarantine_due == []
        assert len(plan.low_confidence) == 1
        assert plan.low_confidence[0].candidate_id == candidate.id
        assert plan.low_confidence[0].reason.endswith(":atmos_immune")
        refreshed_file = session.get(File, file_record.id)
        assert refreshed_file is not None
        assert refreshed_file.atmos_detected is True


def test_restore_handles_conflicts(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        source_file = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.flac",
        )
        _cache_track(
            session,
            RoonTrack(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                duration_ms=300_000,
                track_number=1,
            ),
        )
        _add_candidate(session)
        plan = _build_plan(session, config)
        apply_plan(session=session, config=config, plan=plan)

        destination = (
            config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
        )
        assert destination.exists()

        original_path = config.paths.library_roots[0] / "Artist/Album/track.flac"
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_text("conflict")

        conflict_result = restore_from_quarantine(
            session=session, target=source_file.id
        )
        assert conflict_result.conflict is True

        original_path.unlink()
        success_result = restore_from_quarantine(session=session, target=source_file.id)
        assert success_result.conflict is False
        assert original_path.exists()
        refreshed_candidate = session.query(BlockCandidate).first()
        assert refreshed_candidate is not None
        assert refreshed_candidate.status == "restored"

