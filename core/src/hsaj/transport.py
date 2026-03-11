from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

import websockets
from sqlalchemy import select
from sqlalchemy.orm import Session

from .bridge_auth import append_bridge_token
from .db.models import PlayHistory
from .timeutils import ensure_utc, utc_now

logger = logging.getLogger(__name__)
DEFAULT_BRIDGE_WS_URL = "ws://localhost:8080/events"


def _parse_timestamp(value: str | None) -> datetime:
    if value is None:
        return utc_now()

    parsed = datetime.fromisoformat(value)
    return ensure_utc(parsed) or utc_now()


@dataclass(frozen=True)
class TransportEvent:
    """Normalized transport event received from the bridge."""

    event: str
    track_id: str
    timestamp: datetime
    source: str
    user_id: str | None = None
    quality: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    raw_payload: Mapping[str, Any] | None = None

    def describe(self) -> str:
        """Return a concise string for logs."""

        quality = self.quality or "unknown"
        return (
            f"[{self.source}] {self.event} track={self.track_id} "
            f"at={self.timestamp.isoformat()} quality={quality}"
        )

    @classmethod
    def from_ws_message(cls, message: str | Mapping[str, Any]) -> "TransportEvent":
        """Parse a WebSocket payload into TransportEvent."""

        payload: Mapping[str, Any] = json.loads(message) if isinstance(message, str) else message
        if payload.get("type") != "transport_event":
            raise ValueError("Only type=transport_event messages are supported")

        event_payload = payload.get("event")
        if not isinstance(event_payload, Mapping):
            raise ValueError("event field is missing or has an invalid format")

        event = str(event_payload.get("event", "")).strip()
        track_id = str(event_payload.get("track_id", "")).strip()
        if not event or not track_id:
            raise ValueError("transport_event requires both event and track_id")

        duration_raw = event_payload.get("duration_ms")
        duration_ms = int(duration_raw) if duration_raw is not None else None

        return cls(
            event=event,
            track_id=track_id,
            timestamp=_parse_timestamp(str(event_payload.get("timestamp"))),
            source=str(event_payload.get("source", "bridge")),
            user_id=(str(event_payload.get("user_id")) if event_payload.get("user_id") else None),
            quality=(str(event_payload.get("quality")) if event_payload.get("quality") else None),
            title=(str(event_payload.get("title")) if event_payload.get("title") else None),
            artist=(str(event_payload.get("artist")) if event_payload.get("artist") else None),
            album=(str(event_payload.get("album")) if event_payload.get("album") else None),
            duration_ms=duration_ms,
            raw_payload=event_payload,
        )


class TransportEventProcessor:
    """Process transport events and write play history."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        logger_override: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._logger = logger_override or logger

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    def handle_event(self, event: TransportEvent) -> None:
        """Log an event and apply it to play_history."""

        self._logger.info("Received event: %s", event.describe())

        with self._session_factory() as session:
            self._close_previous_entry(session=session, ended_at=event.timestamp)
            if event.event == "track_start":
                self._start_new_entry(session=session, event=event)
            session.commit()

    def _close_previous_entry(self, session: Session, ended_at: datetime) -> None:
        open_entry = session.scalars(
            select(PlayHistory)
            .where(PlayHistory.ended_at.is_(None))
            .order_by(PlayHistory.started_at.desc())
        ).first()
        if open_entry is None:
            return

        normalized_start = ensure_utc(open_entry.started_at) or utc_now()
        normalized_end = ensure_utc(ended_at) or utc_now()

        open_entry.ended_at = normalized_end
        delta = normalized_end - normalized_start
        open_entry.played_ms = max(0, int(delta.total_seconds() * 1000))

    def _start_new_entry(self, session: Session, event: TransportEvent) -> None:
        metadata: dict[str, Any] = {}
        for key in ("title", "artist", "album", "quality", "user_id", "duration_ms"):
            value = getattr(event, key)
            if value is not None:
                metadata[key] = value

        metadata_json = json.dumps(metadata) if metadata else None

        session.add(
            PlayHistory(
                track_id=event.track_id,
                source=event.source,
                user_id=event.user_id,
                quality=event.quality,
                started_at=ensure_utc(event.timestamp) or utc_now(),
                title=event.title,
                artist=event.artist,
                album=event.album,
                metadata_json=metadata_json,
            )
        )


async def listen_to_bridge(
    ws_url: str,
    processor: TransportEventProcessor,
    stop_event: asyncio.Event | None = None,
    reconnect_delay: float = 2.0,
) -> None:
    """Connect to the bridge WebSocket and process events forever."""

    connect_url = append_bridge_token(ws_url)
    while stop_event is None or not stop_event.is_set():
        try:
            async with websockets.connect(connect_url) as websocket:
                processor.logger.info("Connected to bridge: %s", connect_url)
                async for message in websocket:
                    try:
                        event = TransportEvent.from_ws_message(message)
                    except ValueError as exc:
                        processor.logger.warning("Skipping message: %s", exc)
                        continue
                    processor.handle_event(event)
                    if stop_event is not None and stop_event.is_set():
                        break
        except Exception as exc:  # pragma: no cover - network errors in production
            processor.logger.warning("WebSocket connection dropped (%s), reconnecting...", exc)
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(reconnect_delay)
