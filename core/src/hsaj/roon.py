from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import File, RoonItemCache

DEFAULT_BRIDGE_HTTP_URL = "http://localhost:8080"


class BridgeClientError(Exception):
    """Ошибка при обращении к HTTP-эндпоинтам bridge."""


@dataclass(frozen=True)
class RoonTrack:
    """Нормализованный ответ bridge для трека Roon."""

    roon_track_id: str
    artist: str | None
    album: str | None
    title: str | None
    duration_ms: int | None
    track_number: int | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RoonTrack":
        """Создаёт объект из JSON-ответа bridge."""

        try:
            roon_track_id = str(payload["roon_track_id"]).strip()
        except Exception as exc:
            raise BridgeClientError("Ответ bridge не содержит roon_track_id") from exc
        if not roon_track_id:
            raise BridgeClientError("Поле roon_track_id пустое")

        duration_ms = cls._parse_optional_int(payload.get("duration_ms"))
        track_number = cls._parse_optional_int(
            payload.get("trackno") or payload.get("track_number")
        )

        return cls(
            roon_track_id=roon_track_id,
            artist=_normalize_string(payload.get("artist")),
            album=_normalize_string(payload.get("album")),
            title=_normalize_string(payload.get("title")),
            duration_ms=duration_ms,
            track_number=track_number,
        )

    @staticmethod
    def _parse_optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise BridgeClientError(f"Невозможно привести значение к int: {value}") from exc


@dataclass(frozen=True)
class FileCandidate:
    """Подходящий файл из аудиотеки."""

    file_id: int
    path: str


@dataclass(frozen=True)
class MappingResult:
    """Результат сопоставления трека Roon с файлами аудиотеки."""

    confidence: Literal["high", "low"]
    candidates: Sequence[FileCandidate]


def fetch_track_from_bridge(
    roon_track_id: str,
    base_url: str | None = None,
    timeout: float = 5.0,
) -> RoonTrack:
    """Получает нормализованные данные трека из bridge по HTTP."""

    bridge_base = base_url or os.environ.get("HSAJ_BRIDGE_HTTP") or DEFAULT_BRIDGE_HTTP_URL
    url = f"{bridge_base.rstrip('/')}/track/{quote(roon_track_id)}"
    request = Request(url, headers={"Accept": "application/json"})

    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise BridgeClientError(
                    f"Bridge вернул статус {response.status} для {roon_track_id}"
                )
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise BridgeClientError(f"Не удалось получить трек {roon_track_id}: {exc}") from exc

    return RoonTrack.from_dict(payload)


def cache_roon_track(session: Session, track: RoonTrack) -> RoonItemCache:
    """Сохраняет трек Roon в кэш (insert или update)."""

    existing = session.execute(
        select(RoonItemCache).where(RoonItemCache.roon_track_id == track.roon_track_id)
    ).scalar_one_or_none()
    if existing:
        existing.artist = track.artist
        existing.album = track.album
        existing.title = track.title
        existing.track_number = track.track_number
        existing.duration_ms = track.duration_ms
        return existing

    cached = RoonItemCache(
        roon_track_id=track.roon_track_id,
        artist=track.artist,
        album=track.album,
        title=track.title,
        track_number=track.track_number,
        duration_ms=track.duration_ms,
    )
    session.add(cached)
    return cached


def match_track_by_metadata(
    session: Session,
    track: RoonTrack,
    duration_tolerance_seconds: int = 2,
) -> MappingResult:
    """Сопоставляет трек по artist/album/title/track_number и длительности."""

    filters = []

    normalized_artist = _normalize_string(track.artist)
    normalized_album = _normalize_string(track.album)
    normalized_title = _normalize_string(track.title)

    if normalized_artist is not None:
        filters.append(func.lower(File.artist) == normalized_artist.lower())
    if normalized_album is not None:
        filters.append(func.lower(File.album) == normalized_album.lower())
    if normalized_title is not None:
        filters.append(func.lower(File.title) == normalized_title.lower())
    if track.track_number is not None:
        filters.append(File.track_number == track.track_number)

    if track.duration_ms is not None:
        target_seconds = max(0, int(round(track.duration_ms / 1000)))
        min_seconds = max(0, target_seconds - duration_tolerance_seconds)
        max_seconds = target_seconds + duration_tolerance_seconds
        filters.append(File.duration_seconds.between(min_seconds, max_seconds))

    if not filters:
        return MappingResult(confidence="low", candidates=[])

    candidates = session.scalars(select(File).where(*filters)).all()
    file_candidates = [FileCandidate(file_id=item.id, path=item.path) for item in candidates]

    confidence: Literal["high", "low"] = "high" if len(file_candidates) == 1 else "low"
    return MappingResult(confidence=confidence, candidates=file_candidates)


def _normalize_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
