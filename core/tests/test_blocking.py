from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hsaj.blocking import BlockedObject, sync_blocked_objects, upsert_raw_block
from hsaj.db.models import Base, BlockCandidate, RoonBlockRaw


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


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
        assert record.first_seen_at == _naive(seen_at)
        assert record.last_seen_at == _naive(later)


def test_sync_blocked_creates_candidate_with_planned_action() -> None:
    seen_at = datetime(2024, 2, 1, tzinfo=timezone.utc)
    grace_days = 10
    blocked = [BlockedObject(object_type="album", object_id="a1", label="Album 1")]

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
        assert candidate.first_seen_at == _naive(seen_at)
        assert candidate.planned_action_at == _naive(seen_at + timedelta(days=grace_days))
        assert candidate.reason == "blocked_by_album"
        assert candidate.status == "planned"


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
        assert candidate.first_seen_at == _naive(first_seen)
        assert candidate.planned_action_at == _naive(first_seen + timedelta(days=7))
        assert candidate.last_seen_at == _naive(later_seen)



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
        assert candidate.restored_at == _naive(later)
        assert candidate.planned_action_at is None
        assert candidate.last_seen_at == _naive(later)
