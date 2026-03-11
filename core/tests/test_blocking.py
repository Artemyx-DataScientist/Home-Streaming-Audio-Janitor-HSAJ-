from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hsaj.blocking import (
    BlockedObject,
    fetch_blocked_snapshot_from_bridge,
    record_blocked_sync_failure,
    record_blocked_sync_success,
    sync_blocked_objects,
    upsert_raw_block,
)
from hsaj.db.models import Base, BlockCandidate, BridgeSyncStatus, RoonBlockRaw


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_upsert_raw_block_preserves_first_seen() -> None:
    seen_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    later = seen_at + timedelta(days=2)
    blocked = BlockedObject(object_type="track", object_id="t1", label="Track 1")

    with _session() as session:
        _, created_first = upsert_raw_block(session=session, blocked=blocked, seen_at=seen_at)
        session.commit()
        assert created_first is True

        _, created_second = upsert_raw_block(session=session, blocked=blocked, seen_at=later)
        session.commit()
        assert created_second is False

        record = session.scalars(select(RoonBlockRaw)).one()
        assert record.first_seen_at == seen_at
        assert record.last_seen_at == later


def test_sync_blocked_creates_candidate_with_planned_action() -> None:
    seen_at = datetime(2024, 2, 1, tzinfo=timezone.utc)
    grace_days = 10
    blocked = [
        BlockedObject(
            object_type="album",
            object_id="a1",
            label="Album 1",
            artist="Artist 1",
            album="Album 1",
        )
    ]

    with _session() as session:
        result = sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            grace_period_days=grace_days,
            seen_at=seen_at,
        )
        session.commit()

        assert result.candidates_created == 1
        candidate = session.scalars(select(BlockCandidate)).one()
        assert candidate.first_seen_at == seen_at
        assert candidate.planned_action_at == seen_at + timedelta(days=grace_days)
        assert candidate.reason == "blocked_by_album"
        assert candidate.status == "planned"
        assert candidate.metadata_json == (
            '{"album": "Album 1", "artist": "Artist 1", '
            '"duration_ms": null, "title": null, "track_number": null}'
        )


def test_sync_blocked_does_not_shift_planned_on_repeat_sync() -> None:
    first_seen = datetime(2024, 3, 1, tzinfo=timezone.utc)
    later_seen = first_seen + timedelta(days=5)
    blocked = [BlockedObject(object_type="artist", object_id="artist-1", label="Artist 1")]

    with _session() as session:
        sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            grace_period_days=7,
            seen_at=first_seen,
        )
        session.commit()

        sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            grace_period_days=30,
            seen_at=later_seen,
        )
        session.commit()

        candidate = session.scalars(select(BlockCandidate)).one()
        assert candidate.first_seen_at == first_seen
        assert candidate.planned_action_at == first_seen + timedelta(days=7)
        assert candidate.last_seen_at == later_seen


def test_sync_blocked_marks_restored_when_missing() -> None:
    seen_at = datetime(2024, 4, 1, tzinfo=timezone.utc)
    later = seen_at + timedelta(days=3)
    blocked = [BlockedObject(object_type="track", object_id="t-restored", label="Restored Track")]

    with _session() as session:
        sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            grace_period_days=5,
            seen_at=seen_at,
        )
        session.commit()

        sync_blocked_objects(session=session, blocked_items=[], grace_period_days=5, seen_at=later)
        session.commit()

        candidate = session.scalars(select(BlockCandidate)).one()
        assert candidate.status == "restored"
        assert candidate.restored_at == later
        assert candidate.planned_action_at is None
        assert candidate.last_seen_at == later


def test_record_blocked_sync_status_tracks_success_and_failure() -> None:
    seen_at = datetime(2024, 5, 1, tzinfo=timezone.utc)
    snapshot = fetch_blocked_snapshot_from_bridge_from_payload(
        {
            "contract_version": "v2",
            "generated_at": "2024-05-01T00:00:00+00:00",
            "source": {"configured": True, "mode": "file"},
            "item_count": 1,
            "items": [{"type": "artist", "id": "artist-1", "artist": "Artist"}],
        }
    )

    with _session() as session:
        record_blocked_sync_success(session, snapshot=snapshot, attempted_at=seen_at)
        session.commit()

        status = session.get(BridgeSyncStatus, "blocked")
        assert status is not None
        assert status.status == "ok"
        assert status.contract_version == "v2"
        assert status.source_mode == "file"
        assert status.item_count == 1
        assert status.last_success_at == seen_at

        failure_at = seen_at + timedelta(hours=1)
        record_blocked_sync_failure(session, error="bridge unavailable", attempted_at=failure_at)
        session.commit()

        status = session.get(BridgeSyncStatus, "blocked")
        assert status is not None
        assert status.status == "error"
        assert status.last_error == "bridge unavailable"
        assert status.last_success_at == seen_at
        assert status.last_attempt_at == failure_at


def test_fetch_blocked_snapshot_parses_envelope() -> None:
    snapshot = fetch_blocked_snapshot_from_bridge_from_payload(
        {
            "contract_version": "v2",
            "generated_at": "2024-05-01T00:00:00+00:00",
            "source": {"configured": True, "mode": "inline_json"},
            "item_count": 2,
            "items": [
                {"type": "artist", "id": "artist-1", "artist": "Artist"},
                {"type": "album", "id": "album-1", "artist": "Artist", "album": "Album"},
            ],
        }
    )

    assert snapshot.contract_version == "v2"
    assert snapshot.source_mode == "inline_json"
    assert snapshot.item_count == 2
    assert len(snapshot.items) == 2
    assert snapshot.items[1].object_type == "album"


def fetch_blocked_snapshot_from_bridge_from_payload(payload: dict) -> object:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return fetch_blocked_snapshot_from_bridge(
            base_url=f"http://127.0.0.1:{server.server_address[1]}"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
