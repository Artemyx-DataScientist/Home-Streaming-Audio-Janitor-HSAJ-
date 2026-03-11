from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import Exemption, File


@dataclass(frozen=True, slots=True)
class ExemptionMatch:
    exemption_id: int
    scope_type: str
    reason: str | None


def add_exemption(
    session: Session,
    *,
    scope_type: str,
    reason: str | None = None,
    file_id: int | None = None,
    path: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    title: str | None = None,
    track_number: int | None = None,
) -> Exemption:
    exemption = Exemption(
        scope_type=scope_type,
        file_id=file_id,
        path=path,
        artist=artist,
        album=album,
        title=title,
        track_number=track_number,
        reason=reason,
        active=True,
    )
    session.add(exemption)
    session.flush()
    return exemption


def list_exemptions(session: Session) -> list[Exemption]:
    return session.scalars(select(Exemption).order_by(Exemption.id.asc())).all()


def deactivate_exemption(session: Session, exemption_id: int) -> Exemption | None:
    exemption = session.get(Exemption, exemption_id)
    if exemption is None:
        return None
    exemption.active = False
    return exemption


def match_file_exemption(session: Session, file_record: File) -> ExemptionMatch | None:
    path = Path(file_record.path)
    exemptions = session.scalars(
        select(Exemption).where(Exemption.active.is_(True)).order_by(Exemption.id.asc())
    ).all()
    for exemption in exemptions:
        if _matches(exemption, file_record, path):
            return ExemptionMatch(
                exemption_id=exemption.id,
                scope_type=exemption.scope_type,
                reason=exemption.reason,
            )
    return None


def _matches(exemption: Exemption, file_record: File, path: Path) -> bool:
    scope_type = exemption.scope_type
    if scope_type == "file_id":
        return exemption.file_id == file_record.id
    if scope_type == "path":
        return exemption.path == file_record.path
    if scope_type == "path_prefix":
        return exemption.path is not None and str(path).startswith(exemption.path)
    if scope_type == "artist":
        return _lower(exemption.artist) == _lower(file_record.artist)
    if scope_type == "album":
        return _lower(exemption.artist) == _lower(file_record.artist) and _lower(
            exemption.album
        ) == _lower(file_record.album)
    if scope_type == "track":
        return (
            _lower(exemption.artist) == _lower(file_record.artist)
            and _lower(exemption.album) == _lower(file_record.album)
            and _lower(exemption.title) == _lower(file_record.title)
            and exemption.track_number == file_record.track_number
        )
    return False


def _lower(value: str | None) -> str | None:
    return value.casefold() if value is not None else None
