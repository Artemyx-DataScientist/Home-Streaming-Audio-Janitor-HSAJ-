from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from mutagen import File
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp3 import HeaderNotFoundError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .atmos import is_atmos
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
    atmos_detected: bool


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
        try:
            tags = EasyID3(path)
            return dict(tags)
        except ID3NoHeaderError:
            return {}
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Could not read ID3 tags for %s: %s", path, exc)
            return {}
    except Exception as exc:
        logger.warning("Tag read error for %s: %s", path, exc)
        return {}

    if audio is None or not audio.tags:
        return {}

    return dict(audio.tags)


def _extract_metadata(
    path: Path,
    *,
    ffprobe_path: str = "ffprobe",
    atmos_detection_fn: Callable[[Path], bool] | None = None,
) -> FileMetadata:
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
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Could not read duration for %s: %s", path, exc)

    detection = atmos_detection_fn or (
        lambda target: is_atmos(target, ffprobe_path=ffprobe_path)
    )

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
        atmos_detected=detection(path.resolve()),
    )


def _first_tag(tags: dict[str, str | list[str] | None], key: str) -> str | None:
    value = tags.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _normalize_extensions(extensions: Sequence[str] | None) -> set[str] | None:
    if extensions is None:
        return None
    normalized = {
        f".{item.strip().lstrip('.').lower()}"
        for item in extensions
        if str(item).strip()
    }
    return normalized or None


def _should_skip_dir(path: Path, excluded_dirs: Sequence[Path]) -> bool:
    resolved = path.resolve()
    for excluded in excluded_dirs:
        try:
            if resolved.is_relative_to(excluded.resolve()):
                return True
        except ValueError:
            continue
    return False


def _iter_files(roots: Sequence[Path]) -> Iterable[Path]:
    yield from _iter_files_filtered(
        roots=roots,
        allowed_extensions=None,
        excluded_dirs=(),
    )


def _iter_files_filtered(
    roots: Sequence[Path],
    allowed_extensions: Sequence[str] | None,
    excluded_dirs: Sequence[Path],
) -> Iterable[Path]:
    normalized_extensions = _normalize_extensions(allowed_extensions)
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            logger.warning("Directory %s does not exist, skipping", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            current_dir = Path(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if not _should_skip_dir(current_dir / name, excluded_dirs)
            ]
            for filename in filenames:
                path = current_dir / filename
                if (
                    normalized_extensions is not None
                    and path.suffix.lower() not in normalized_extensions
                ):
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield resolved


def scan_library(
    engine: Engine,
    library_roots: Sequence[Path],
    allowed_extensions: Sequence[str] | None = None,
    excluded_dirs: Sequence[Path] = (),
    batch_size: int = 200,
    dry_run: bool = False,
    ffprobe_path: str = "ffprobe",
    atmos_detection_fn: Callable[[Path], bool] | None = None,
) -> ScanSummary:
    summary = ScanSummary()

    file_iter = _iter_files_filtered(
        roots=library_roots,
        allowed_extensions=allowed_extensions,
        excluded_dirs=excluded_dirs,
    )
    if dry_run:
        for _ in file_iter:
            summary.found_files += 1
        return summary

    pending = 0
    with Session(engine) as session:
        for file_path in file_iter:
            summary.found_files += 1
            metadata = _extract_metadata(
                file_path,
                ffprobe_path=ffprobe_path,
                atmos_detection_fn=atmos_detection_fn,
            )
            summary = _upsert_file(session=session, metadata=metadata, summary=summary)
            pending += 1
            if pending >= batch_size:
                session.commit()
                pending = 0
        session.commit()

    return summary


def _upsert_file(
    session: Session, metadata: FileMetadata, summary: ScanSummary
) -> ScanSummary:
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
                atmos_detected=metadata.atmos_detected,
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
    updated |= _assign_if_changed(
        existing, "duration_seconds", metadata.duration_seconds
    )
    updated |= _assign_if_changed(existing, "atmos_detected", metadata.atmos_detected)

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
