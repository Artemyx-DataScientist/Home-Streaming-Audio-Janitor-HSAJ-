from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hsaj.db.models import Base, PlayHistory
from hsaj.transport import TransportEvent, TransportEventProcessor


def _session_factory(engine: Engine) -> Callable[[], Session]:
    def _factory() -> Session:
        return Session(engine)

    return _factory


def test_transport_event_parses_payload() -> None:
    payload = {
        "type": "transport_event",
        "event": {
            "event": "track_start",
            "track_id": "track-1",
            "timestamp": "2024-01-01T12:00:00+00:00",
            "source": "bridge-dev",
            "quality": "lossless",
            "title": "Test Track",
            "album": "Album",
            "artist": "Artist",
            "duration_ms": 180_000,
        },
    }

    event = TransportEvent.from_ws_message(json.dumps(payload))

    assert event.event == "track_start"
    assert event.track_id == "track-1"
    assert event.timestamp == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert "lossless" in event.describe()
    assert event.raw_payload is not None


def test_two_track_changes_close_previous_entry() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    processor = TransportEventProcessor(session_factory=_session_factory(engine))

    first_started = datetime(2024, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    second_started = first_started + timedelta(seconds=42)

    processor.handle_event(
        TransportEvent(
            event="track_start",
            track_id="track-a",
            timestamp=first_started,
            source="bridge",
            quality="lossless",
        )
    )
    processor.handle_event(
        TransportEvent(
            event="track_start",
            track_id="track-b",
            timestamp=second_started,
            source="bridge",
            quality="lossless",
        )
    )

    with Session(engine) as session:
        history = session.scalars(select(PlayHistory).order_by(PlayHistory.started_at)).all()

        assert len(history) == 2
        assert history[0].ended_at == second_started.replace(tzinfo=None)
        assert history[0].played_ms == 42_000
        assert history[1].ended_at is None


def test_track_stop_closes_open_entry() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    processor = TransportEventProcessor(session_factory=_session_factory(engine))

    started_at = datetime(2024, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    stopped_at = started_at + timedelta(seconds=10)

    processor.handle_event(
        TransportEvent(
            event="track_start",
            track_id="track-a",
            timestamp=started_at,
            source="bridge",
        )
    )
    processor.handle_event(
        TransportEvent(
            event="track_stop",
            track_id="track-a",
            timestamp=stopped_at,
            source="bridge",
        )
    )

    with Session(engine) as session:
        history = session.scalars(select(PlayHistory)).all()

        assert len(history) == 1
        assert history[0].ended_at == stopped_at.replace(tzinfo=None)
        assert history[0].played_ms == 10_000
