from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .atmos import AtmosMovePlan, is_atmos, plan_atmos_moves
from .config import HsajConfig
from .db.models import BlockCandidate, File, RoonItemCache
from .roon import RoonTrack, match_track_by_metadata
from .timeutils import ensure_utc, utc_isoformat, utc_now

BLOCK_PRIORITY = {"track": 0, "album": 1, "artist": 2}


@dataclass(slots=True)
class QuarantineMovePlan:
    candidate_id: int
    file_id: int
    source: Path
    destination: Path
    reason: str
    planned_action_at: datetime | None
    object_type: str
    object_id: str


@dataclass(slots=True)
class LowConfidencePlan:
    candidate_id: int
    object_type: str
    object_id: str
    planned_action_at: datetime | None
    reason: str
    matched_file_ids: list[int]


@dataclass(slots=True)
class Plan:
    atmos_moves: list[AtmosMovePlan]
    blocked_quarantine_due: list[QuarantineMovePlan]
    blocked_quarantine_future: list[QuarantineMovePlan]
    low_confidence: list[LowConfidencePlan]

    def to_dict(self) -> dict[str, object]:
        def _serialize_path(value: Path) -> str:
            return str(value)

        def _serialize_datetime(value: datetime | None) -> str | None:
            return utc_isoformat(value)

        def _serialize_move(move: AtmosMovePlan) -> dict[str, object]:
            payload = asdict(move)
            payload["source"] = _serialize_path(move.source)
            payload["destination"] = _serialize_path(move.destination)
            return payload

        def _serialize_quarantine(item: QuarantineMovePlan) -> dict[str, object]:
            payload = asdict(item)
            payload["source"] = _serialize_path(item.source)
            payload["destination"] = _serialize_path(item.destination)
            payload["planned_action_at"] = _serialize_datetime(item.planned_action_at)
            return payload

        def _serialize_low_conf(item: LowConfidencePlan) -> dict[str, object]:
            payload = asdict(item)
            payload["planned_action_at"] = _serialize_datetime(item.planned_action_at)
            return payload

        return {
            "atmos_moves": [_serialize_move(item) for item in self.atmos_moves],
            "blocked_quarantine_due": [
                _serialize_quarantine(item) for item in self.blocked_quarantine_due
            ],
            "blocked_quarantine_future": [
                _serialize_quarantine(item) for item in self.blocked_quarantine_future
            ],
            "low_confidence": [_serialize_low_conf(item) for item in self.low_confidence],
        }

    def to_json(self, *, ensure_ascii: bool = False, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, indent=indent)


def _candidate_metadata(candidate: BlockCandidate) -> dict[str, object]:
    if not candidate.metadata_json:
        return {}
    try:
        parsed = json.loads(candidate.metadata_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_candidate_track(session: Session, candidate: BlockCandidate) -> RoonTrack | None:
    if candidate.object_type != "track":
        return None

    metadata = _candidate_metadata(candidate)
    if metadata:
        return RoonTrack(
            roon_track_id=candidate.object_id,
            artist=_metadata_string(metadata.get("artist")),
            album=_metadata_string(metadata.get("album")),
            title=_metadata_string(metadata.get("title")),
            duration_ms=_metadata_int(metadata.get("duration_ms")),
            track_number=_metadata_int(metadata.get("track_number")),
        )

    cached = session.scalar(
        select(RoonItemCache).where(RoonItemCache.roon_track_id == candidate.object_id)
    )
    if cached is None:
        return None
    return RoonTrack(
        roon_track_id=cached.roon_track_id,
        artist=cached.artist,
        album=cached.album,
        title=cached.title,
        duration_ms=cached.duration_ms,
        track_number=cached.track_number,
    )


def _relative_to_roots(path: Path, roots: Sequence[Path]) -> Path:
    for root in roots:
        try:
            return path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
    return Path(path.name)


def _build_quarantine_destination(
    source: Path,
    quarantine_root: Path,
    library_roots: Sequence[Path],
    date_folder: str,
) -> Path:
    relative = _relative_to_roots(source, library_roots)
    return quarantine_root / date_folder / relative


def _safe_datetime(value: datetime | None) -> datetime | None:
    return ensure_utc(value)


def _metadata_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate_low_confidence(
    candidate: BlockCandidate,
    *,
    reason: str | None = None,
    matched_file_ids: list[int] | None = None,
) -> LowConfidencePlan:
    return LowConfidencePlan(
        candidate_id=candidate.id,
        object_type=candidate.object_type,
        object_id=candidate.object_id,
        planned_action_at=_safe_datetime(candidate.planned_action_at),
        reason=reason or candidate.reason,
        matched_file_ids=matched_file_ids or [],
    )


def _is_path_within(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _is_file_eligible(file_record: File, config: HsajConfig) -> bool:
    source_path = Path(file_record.path)
    if config.paths.quarantine_dir is not None and _is_path_within(
        source_path, config.paths.quarantine_dir
    ):
        return False
    return True


def _resolve_files_for_candidate(
    session: Session,
    candidate: BlockCandidate,
    config: HsajConfig,
) -> tuple[list[File], list[LowConfidencePlan]]:
    if candidate.object_type == "track":
        track = _load_candidate_track(session, candidate)
        if track is None:
            return ([], [_candidate_low_confidence(candidate)])

        mapping = match_track_by_metadata(session=session, track=track)
        if mapping.confidence == "low" or len(mapping.candidates) != 1:
            return (
                [],
                [
                    _candidate_low_confidence(
                        candidate,
                        matched_file_ids=[item.file_id for item in mapping.candidates],
                    )
                ],
            )

        file_record = session.get(File, mapping.candidates[0].file_id)
        if file_record is None or not _is_file_eligible(file_record, config):
            return (
                [],
                [
                    _candidate_low_confidence(
                        candidate,
                        reason=f"{candidate.reason}:no_eligible_file",
                        matched_file_ids=[mapping.candidates[0].file_id],
                    )
                ],
            )
        return ([file_record], [])

    metadata = _candidate_metadata(candidate)
    artist = _metadata_string(metadata.get("artist"))
    album = _metadata_string(metadata.get("album"))

    if candidate.object_type == "artist":
        artist = artist or _metadata_string(candidate.label)
        if artist is None:
            return ([], [_candidate_low_confidence(candidate)])
        matches = session.scalars(
            select(File).where(func.lower(File.artist) == artist.lower())
        ).all()
    elif candidate.object_type == "album":
        album = album or _metadata_string(candidate.label)
        if album is None:
            return ([], [_candidate_low_confidence(candidate)])
        filters = [func.lower(File.album) == album.lower()]
        if artist is not None:
            filters.append(func.lower(File.artist) == artist.lower())
        matches = session.scalars(select(File).where(*filters)).all()
    else:
        return (
            [],
            [
                _candidate_low_confidence(
                    candidate, reason=f"{candidate.reason}:unsupported_type"
                )
            ],
        )

    eligible_matches = [item for item in matches if _is_file_eligible(item, config)]
    if not eligible_matches:
        return ([], [_candidate_low_confidence(candidate)])
    return (eligible_matches, [])


def _build_move_for_file(
    candidate: BlockCandidate,
    file_record: File,
    config: HsajConfig,
    *,
    date_folder: str,
) -> tuple[QuarantineMovePlan | None, LowConfidencePlan | None]:
    source_path = Path(file_record.path)
    if file_record.atmos_detected:
        return (
            None,
            _candidate_low_confidence(
                candidate,
                reason=f"{candidate.reason}:atmos_immune",
                matched_file_ids=[file_record.id],
            ),
        )
    if config.paths.atmos_dir is not None and _is_path_within(
        source_path, config.paths.atmos_dir
    ):
        return (
            None,
            _candidate_low_confidence(
                candidate,
                reason=f"{candidate.reason}:atmos_immune",
                matched_file_ids=[file_record.id],
            ),
        )
    if not any(
        _is_path_within(source_path, library_root)
        for library_root in config.paths.library_roots
    ):
        return (
            None,
            _candidate_low_confidence(
                candidate,
                reason=f"{candidate.reason}:no_eligible_file",
                matched_file_ids=[file_record.id],
            ),
        )

    destination = _build_quarantine_destination(
        source_path,
        quarantine_root=config.paths.quarantine_dir,  # type: ignore[arg-type]
        library_roots=config.paths.library_roots,
        date_folder=date_folder,
    )
    return (
        QuarantineMovePlan(
            candidate_id=candidate.id,
            file_id=file_record.id,
            source=source_path,
            destination=destination,
            reason=candidate.reason,
            planned_action_at=_safe_datetime(candidate.planned_action_at),
            object_type=candidate.object_type,
            object_id=candidate.object_id,
        ),
        None,
    )


def _plan_for_candidate(
    session: Session,
    candidate: BlockCandidate,
    config: HsajConfig,
    *,
    now: datetime,
    date_folder: str,
) -> tuple[list[QuarantineMovePlan], list[LowConfidencePlan]]:
    del now

    resolved_files, low_confidence = _resolve_files_for_candidate(
        session=session,
        candidate=candidate,
        config=config,
    )
    if not resolved_files:
        return ([], low_confidence)

    moves: list[QuarantineMovePlan] = []
    for file_record in resolved_files:
        move, low_conf = _build_move_for_file(
            candidate,
            file_record,
            config,
            date_folder=date_folder,
        )
        if move is not None:
            moves.append(move)
        if low_conf is not None:
            low_confidence.append(low_conf)
    return (moves, low_confidence)


def build_plan(
    session: Session,
    config: HsajConfig,
    *,
    now: datetime | None = None,
    atmos_detection_fn: Callable[[Path], bool] | None = None,
) -> Plan:
    current_time = ensure_utc(now) or utc_now()
    if config.paths.quarantine_dir is None:
        raise ValueError("paths.quarantine_dir must be configured")

    date_folder = current_time.date().isoformat()
    atmos_moves: list[AtmosMovePlan] = []
    if config.paths.atmos_dir is not None:
        detection_fn = atmos_detection_fn or (
            lambda target: is_atmos(target, ffprobe_path=config.paths.ffprobe_path)
        )
        atmos_moves = plan_atmos_moves(
            session=session,
            atmos_root=config.paths.atmos_dir,
            detection_fn=detection_fn,
        )

    blocked_quarantine_due: list[QuarantineMovePlan] = []
    blocked_quarantine_future: list[QuarantineMovePlan] = []
    low_confidence: list[LowConfidencePlan] = []

    candidates = session.scalars(
        select(BlockCandidate).where(BlockCandidate.status == "planned")
    ).all()
    candidates.sort(
        key=lambda candidate: (
            BLOCK_PRIORITY.get(candidate.object_type, 99),
            candidate.id,
        )
    )
    planned_file_ids: set[int] = set()
    for candidate in candidates:
        planned_at = _safe_datetime(candidate.planned_action_at)
        candidate_moves, candidate_low_conf = _plan_for_candidate(
            session=session,
            candidate=candidate,
            config=config,
            now=current_time,
            date_folder=date_folder,
        )
        low_confidence.extend(candidate_low_conf)
        candidate_moves = [
            move for move in candidate_moves if move.file_id not in planned_file_ids
        ]
        if not candidate_moves:
            continue
        planned_file_ids.update(move.file_id for move in candidate_moves)

        target_bucket: list[QuarantineMovePlan]
        if planned_at is not None and planned_at <= current_time:
            target_bucket = blocked_quarantine_due
        else:
            target_bucket = blocked_quarantine_future
        target_bucket.extend(candidate_moves)

    return Plan(
        atmos_moves=atmos_moves,
        blocked_quarantine_due=blocked_quarantine_due,
        blocked_quarantine_future=blocked_quarantine_future,
        low_confidence=low_confidence,
    )
