from datetime import datetime, timezone

from core.app import StreamingEvent


def test_streaming_event_describe_preserves_first_seen() -> None:
    first_seen = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    event = StreamingEvent(
        source="test-source",
        first_seen=first_seen,
        payload={"quality": "dolby-atmos"},
        atmos=True,
    )

    description = event.describe()

    assert first_seen.isoformat() in description
    assert "with Atmos" in description
    assert "dolby-atmos" in description
