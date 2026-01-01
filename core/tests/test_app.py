from datetime import datetime, timezone

from hsaj.transport import TransportEvent


def test_transport_event_describe_contains_timestamp() -> None:
    first_seen = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    event = TransportEvent(
        event="track_start",
        track_id="track-id",
        timestamp=first_seen,
        source="test-source",
        quality="dolby-atmos",
    )

    description = event.describe()

    assert first_seen.isoformat() in description
    assert "dolby-atmos" in description
    assert "track-id" in description
