from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from hsaj.config import DatabaseConfig, HsajConfig, PathsConfig
from hsaj.db import init_database
from hsaj.db.models import ActionLog, BlockCandidate, File, LibraryTrack, RoonItemCache
from hsaj.executor import apply_plan, cleanup_retention, restore_from_quarantine
from hsaj.exemptions import add_exemption
from hsaj.planner import Plan, build_plan
from hsaj.roon import RoonTrack
from hsaj.scanner import sync_library_graph


def _base_config(tmp_path: Path) -> HsajConfig:
    return HsajConfig(
        database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
        paths=PathsConfig(
            library_roots=[tmp_path / "library"],
            quarantine_dir=tmp_path / "quarantine",
            atmos_dir=tmp_path / "atmos",
        ),
    )


def _create_file(
    session: Session,
    path: Path,
    *,
    artist: str = "Artist",
    album: str = "Album",
    title: str = "Title",
    track_number: int = 1,
) -> File:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content")
    file_record = File(
        path=str(path),
        size_bytes=1,
        format="flac",
        mtime=datetime.now(timezone.utc),
        artist=artist,
        album=album,
        title=title,
        track_number=track_number,
        year=2024,
        duration_seconds=300,
    )
    session.add(file_record)
    session.commit()
    sync_library_graph(session)
    session.commit()
    return file_record


def _add_candidate(
    session: Session,
    *,
    object_type: str = "track",
    object_id: str = "track-1",
    label: str | None = None,
    metadata_json: str | None = None,
) -> BlockCandidate:
    candidate = BlockCandidate(
        object_type=object_type,
        object_id=object_id,
        label=label,
        metadata_json=metadata_json,
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
        destination = config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
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
        _create_file(session, path=config.paths.library_roots[0] / "Artist/Album/track.flac")
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
        destination = config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
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


def test_plan_resolves_album_candidate_to_multiple_files(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        first = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track-1.flac",
            artist="Artist",
            album="Album",
            title="Track 1",
            track_number=1,
        )
        second = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track-2.flac",
            artist="Artist",
            album="Album",
            title="Track 2",
            track_number=2,
        )
        _create_file(
            session,
            path=config.paths.library_roots[0] / "Other/Album/other.flac",
            artist="Other",
            album="Album",
            title="Other Track",
            track_number=1,
        )
        _add_candidate(
            session,
            object_type="album",
            object_id="album-1",
            label="Artist - Album",
            metadata_json=json.dumps({"artist": "Artist", "album": "Album"}),
        )

        plan = _build_plan(session, config)

    assert sorted(move.file_id for move in plan.blocked_quarantine_due) == [
        first.id,
        second.id,
    ]
    assert plan.low_confidence == []


def test_plan_resolves_artist_candidate_via_normalized_library_graph(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        first = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track-1.flac",
            artist="Artist",
            album="Album",
            title="Track 1",
            track_number=1,
        )
        second = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Elsewhere/track-2.flac",
            artist="Artist",
            album="Elsewhere",
            title="Track 2",
            track_number=2,
        )
        _create_file(
            session,
            path=config.paths.library_roots[0] / "Other/Album/other.flac",
            artist="Other",
            album="Album",
            title="Other Track",
            track_number=1,
        )
        _add_candidate(
            session,
            object_type="artist",
            object_id="artist-1",
            label="Artist",
            metadata_json=json.dumps({"artist": "Artist"}),
        )

        graph_tracks = session.scalars(select(LibraryTrack)).all()
        assert len(graph_tracks) == 3

        plan = _build_plan(session, config)

    assert sorted(move.file_id for move in plan.blocked_quarantine_due) == [
        first.id,
        second.id,
    ]


def test_plan_prefers_track_over_album_and_artist(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        file_record = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.flac",
        )
        _add_candidate(
            session,
            object_type="artist",
            object_id="artist-1",
            label="Artist",
            metadata_json=json.dumps({"artist": "Artist"}),
        )
        album_candidate = _add_candidate(
            session,
            object_type="album",
            object_id="album-1",
            label="Artist - Album",
            metadata_json=json.dumps({"artist": "Artist", "album": "Album"}),
        )
        track_candidate = _add_candidate(
            session,
            object_type="track",
            object_id="track-1",
            metadata_json=json.dumps(
                {
                    "artist": "Artist",
                    "album": "Album",
                    "title": "Title",
                    "track_number": 1,
                    "duration_ms": 300000,
                }
            ),
        )

        plan = _build_plan(session, config)

    assert len(plan.blocked_quarantine_due) == 1
    assert plan.blocked_quarantine_due[0].file_id == file_record.id
    assert plan.blocked_quarantine_due[0].candidate_id == track_candidate.id
    assert plan.blocked_quarantine_due[0].candidate_id != album_candidate.id


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

        destination = config.paths.quarantine_dir / "2024-01-10" / "Artist/Album/track.flac"
        assert destination.exists()

        original_path = config.paths.library_roots[0] / "Artist/Album/track.flac"
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_text("conflict")

        conflict_result = restore_from_quarantine(session=session, target=source_file.id)
        assert conflict_result.conflict is True

        original_path.unlink()
        success_result = restore_from_quarantine(session=session, target=source_file.id)
        assert success_result.conflict is False
        assert original_path.exists()
        refreshed_candidate = session.query(BlockCandidate).first()
        assert refreshed_candidate is not None
        assert refreshed_candidate.status == "restored"


def test_plan_skips_exempt_artist_file(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        file_record = _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.flac",
        )
        add_exemption(
            session,
            scope_type="artist",
            artist="Artist",
            reason="keep favorite artist",
        )
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
        _add_candidate(session)

        plan = _build_plan(session, config)

        assert plan.blocked_quarantine_due == []
        assert len(plan.low_confidence) == 1
        assert plan.low_confidence[0].matched_file_ids == [file_record.id]
        assert plan.low_confidence[0].reason.endswith(":exempt")


def test_plan_builds_advisory_duplicate_soft_candidate(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.flac",
            artist="Artist",
            album="Album",
            title="Title",
            track_number=1,
        )
        _create_file(
            session,
            path=config.paths.library_roots[0] / "Artist/Album/track.mp3",
            artist="Artist",
            album="Album",
            title="Title",
            track_number=1,
        )

        plan = _build_plan(session, config)

        assert len(plan.soft_candidates) == 1
        assert plan.soft_candidates[0].reason == "duplicate_lower_quality"
        assert plan.soft_candidates[0].source.suffix == ".mp3"


def test_cleanup_retention_marks_expired_without_auto_delete(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config.policy.quarantine_delete_days = 1
    engine, _ = init_database(config.database)

    with Session(engine) as session:
        original_path = config.paths.library_roots[0] / "Artist/Album/track.flac"
        _create_file(session, path=original_path)
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
        apply_plan(session=session, config=config, plan=plan)

        candidate = session.get(BlockCandidate, candidate.id)
        assert candidate is not None
        assert candidate.delete_after is not None
        result = cleanup_retention(
            session=session,
            config=config,
            now=candidate.delete_after,
        )

        refreshed_candidate = session.get(BlockCandidate, candidate.id)
        assert result.expired_candidates == [candidate.id]
        assert refreshed_candidate is not None
        assert refreshed_candidate.status == "expired"
