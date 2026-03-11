from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hsaj.blocking import BlockedObject, sync_blocked_objects
from hsaj.cli import _warm_track_cache
from hsaj.db.models import Base, BlockCandidate, RoonItemCache
from hsaj.roon import BridgeClientError, RoonTrack


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_warm_track_cache_warms_cache_and_survives_partial_failures(monkeypatch) -> None:
    blocked = [
        BlockedObject(object_type="track", object_id="track-1", label="Track 1"),
        BlockedObject(object_type="track", object_id="track-2", label="Track 2"),
        BlockedObject(object_type="artist", object_id="artist-1", label="Artist 1"),
    ]

    def fake_fetch_track_from_bridge(roon_track_id: str, base_url: str | None = None):
        if roon_track_id == "track-2":
            raise BridgeClientError("missing")
        return RoonTrack(
            roon_track_id=roon_track_id,
            artist="Artist",
            album="Album",
            title="Title",
            duration_ms=300_000,
            track_number=1,
        )

    monkeypatch.setattr("hsaj.cli.fetch_track_from_bridge", fake_fetch_track_from_bridge)

    with _session() as session:
        sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        session.commit()

        result = _warm_track_cache(session=session, bridge_url=None)
        session.commit()

        assert result.total == 2
        assert result.created == 1
        assert result.updated == 0
        assert result.failed == 1

        cached_ids = session.scalars(select(RoonItemCache.roon_track_id)).all()
        candidate_ids = session.scalars(
            select(BlockCandidate.object_id).where(BlockCandidate.object_type == "track")
        ).all()
        assert cached_ids == ["track-1"]
        assert candidate_ids == ["track-1", "track-2"]
