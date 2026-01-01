from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import websockets
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import PlayHistory

logger = logging.getLogger(__name__)
DEFAULT_BRIDGE_WS_URL = "ws://localhost:8080/events"


def _parse_timestamp(value: str | None) -> datetime:
    if value is None:
        return datetime.now(tz=timezone.utc)

    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass(frozen=True)
class TransportEvent:
    """Нормализованное событие транспортного уровня из bridge."""

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
        """Строковое описание события для логов."""

        quality = self.quality or "unknown"
        return (
            f"[{self.source}] {self.event} track={self.track_id} "
            f"at={self.timestamp.isoformat()} quality={quality}"
        )

    @classmethod
    def from_ws_message(cls, message: str | Mapping[str, Any]) -> "TransportEvent":
        """Парсит сообщение WebSocket в TransportEvent."""

        payload: Mapping[str, Any] = json.loads(message) if isinstance(message, str) else message
        if payload.get("type") != "transport_event":
            raise ValueError("Поддерживаются только сообщения type=transport_event")

        event_payload = payload.get("event")
        if not isinstance(event_payload, Mapping):
            raise ValueError("Поле event отсутствует или имеет неверный формат")

        event = str(event_payload.get("event", "")).strip()
        track_id = str(event_payload.get("track_id", "")).strip()
        if not event or not track_id:
            raise ValueError("transport_event требует поля event и track_id")

        duration_raw = event_payload.get("duration_ms")
        duration_ms = int(duration_raw) if duration_raw is not None else None

        return cls(
            event=event,
            track_id=track_id,
            timestamp=_parse_timestamp(str(event_payload.get("timestamp"))),
            source=str(event_payload.get("source", "bridge")),
            user_id=str(event_payload.get("user_id")) if event_payload.get("user_id") else None,
            quality=str(event_payload.get("quality")) if event_payload.get("quality") else None,
            title=str(event_payload.get("title")) if event_payload.get("title") else None,
            artist=str(event_payload.get("artist")) if event_payload.get("artist") else None,
            album=str(event_payload.get("album")) if event_payload.get("album") else None,
            duration_ms=duration_ms,
            raw_payload=event_payload,
        )


class TransportEventProcessor:
    """Обработчик транспортных событий (лог + запись в play_history)."""

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
        """Логирует событие и применяет его к истории воспроизведений."""

        self._logger.info("Получено событие: %s", event.describe())

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

        normalized_start = _normalize_to_utc_naive(open_entry.started_at)
        normalized_end = _normalize_to_utc_naive(ended_at)

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
                started_at=_normalize_to_utc_naive(event.timestamp),
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
    """Подключается к WebSocket bridge и непрерывно обрабатывает события."""

    while stop_event is None or not stop_event.is_set():
        try:
            async with websockets.connect(ws_url) as websocket:
                processor.logger.info("Подключение к bridge установлено: %s", ws_url)
                async for message in websocket:
                    try:
                        event = TransportEvent.from_ws_message(message)
                    except ValueError as exc:
                        processor.logger.warning("Пропуск сообщения: %s", exc)
                        continue
                    processor.handle_event(event)
                    if stop_event is not None and stop_event.is_set():
                        break
        except Exception as exc:  # pragma: no cover - сетевые ошибки в проде
            processor.logger.warning("WS соединение разорвано (%s), переподключение...", exc)
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(reconnect_delay)
