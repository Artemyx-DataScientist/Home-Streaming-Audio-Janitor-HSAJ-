from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from mutagen import File
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp3 import HeaderNotFoundError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db.models import File as FileModel

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FileMetadata:
    path: Path
    size_bytes: int
    format: str | None
    mtime: datetime
    artist: str | None
    album: str | None
    title: str | None
    track_number: int | None
    year: int | None
    duration_seconds: int | None


@dataclass(slots=True)
class ScanSummary:
    found_files: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0


def _parse_int(value: str | list[str] | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list) and value:
        return _parse_int(value[0])
    try:
        return int(str(value).split("/")[0])
    except (TypeError, ValueError):
        return None


def _extract_tags(path: Path) -> dict[str, str | list[str] | None]:
    try:
        audio = File(path, easy=True)
    except HeaderNotFoundError:
        # Файл с ID3 без аудиофреймов
        try:
            tags = EasyID3(path)
            return dict(tags)
        except ID3NoHeaderError:
            return {}
        except Exception as exc:  # pragma: no cover - защитный блок
            logger.warning("Не удалось прочитать теги ID3 у %s: %s", path, exc)
            return {}
    except Exception as exc:
        logger.warning("Ошибка чтения тегов %s: %s", path, exc)
        return {}

    if audio is None or not audio.tags:
        return {}

    return dict(audio.tags)


def _extract_metadata(path: Path) -> FileMetadata:
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    tags = _extract_tags(path)
    format_name = path.suffix.lower().lstrip(".") or None

    duration_seconds = None
    try:
        audio = File(path)
        if (
            audio is not None
            and getattr(audio, "info", None)
            and getattr(audio.info, "length", None)
        ):
            duration_seconds = int(audio.info.length)
    except Exception as exc:  # pragma: no cover - защитный блок
        logger.warning("Не удалось получить длительность %s: %s", path, exc)

    return FileMetadata(
        path=path.resolve(),
        size_bytes=stat.st_size,
        format=format_name,
        mtime=mtime,
        artist=_first_tag(tags, "artist"),
        album=_first_tag(tags, "album"),
        title=_first_tag(tags, "title"),
        track_number=_parse_int(tags.get("tracknumber")),
        year=_parse_int(tags.get("date") or tags.get("year")),
        duration_seconds=duration_seconds,
    )


def _first_tag(tags: dict[str, str | list[str] | None], key: str) -> str | None:
    value = tags.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _iter_files(roots: Sequence[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            logger.warning("Директория %s не существует, пропускаем", root)
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield resolved


def scan_library(
    engine: Engine,
    library_roots: Sequence[Path],
    dry_run: bool = False,
) -> ScanSummary:
    summary = ScanSummary()

    files_iter = list(_iter_files(library_roots))
    summary.found_files = len(files_iter)
    if dry_run:
        return summary

    with Session(engine) as session:
        for file_path in files_iter:
            metadata = _extract_metadata(file_path)
            summary = _upsert_file(session=session, metadata=metadata, summary=summary)
        session.commit()

    return summary


def _upsert_file(session: Session, metadata: FileMetadata, summary: ScanSummary) -> ScanSummary:
    existing = session.execute(
        select(FileModel).where(FileModel.path == str(metadata.path))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            FileModel(
                path=str(metadata.path),
                size_bytes=metadata.size_bytes,
                format=metadata.format,
                mtime=metadata.mtime,
                artist=metadata.artist,
                album=metadata.album,
                title=metadata.title,
                track_number=metadata.track_number,
                year=metadata.year,
                duration_seconds=metadata.duration_seconds,
            )
        )
        summary.created += 1
        return summary

    updated = False

    updated |= _assign_if_changed(existing, "size_bytes", metadata.size_bytes)
    updated |= _assign_if_changed(existing, "format", metadata.format)
    updated |= _assign_if_changed(existing, "mtime", metadata.mtime)
    updated |= _assign_if_changed(existing, "artist", metadata.artist)
    updated |= _assign_if_changed(existing, "album", metadata.album)
    updated |= _assign_if_changed(existing, "title", metadata.title)
    updated |= _assign_if_changed(existing, "track_number", metadata.track_number)
    updated |= _assign_if_changed(existing, "year", metadata.year)
    updated |= _assign_if_changed(existing, "duration_seconds", metadata.duration_seconds)

    if updated:
        summary.updated += 1
    else:
        summary.skipped += 1
    return summary


def _assign_if_changed(model: FileModel, attr: str, value: object) -> bool:
    if getattr(model, attr) != value:
        setattr(model, attr, value)
        return True
    return False
