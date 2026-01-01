from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .atmos import AtmosMovePlan, plan_atmos_moves
from .config import HsajConfig
from .db.models import BlockCandidate, File, RoonItemCache
from .roon import RoonTrack, match_track_by_metadata


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
            if value is None:
                return None
            return value.astimezone(timezone.utc).isoformat()

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


def _load_cached_track(session: Session, candidate: BlockCandidate) -> RoonTrack | None:
    if candidate.object_type != "track":
        return None
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
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _plan_for_candidate(
    session: Session,
    candidate: BlockCandidate,
    config: HsajConfig,
    *,
    now: datetime,
    date_folder: str,
) -> tuple[list[QuarantineMovePlan], list[LowConfidencePlan]]:
    track = _load_cached_track(session, candidate)
    if track is None:
        return (
            [],
            [
                LowConfidencePlan(
                    candidate_id=candidate.id,
                    object_type=candidate.object_type,
                    object_id=candidate.object_id,
                    planned_action_at=_safe_datetime(candidate.planned_action_at),
                    reason=candidate.reason,
                    matched_file_ids=[],
                )
            ],
        )

    mapping = match_track_by_metadata(session=session, track=track)
    if mapping.confidence == "low" or len(mapping.candidates) != 1:
        return (
            [],
            [
                LowConfidencePlan(
                    candidate_id=candidate.id,
                    object_type=candidate.object_type,
                    object_id=candidate.object_id,
                    planned_action_at=_safe_datetime(candidate.planned_action_at),
                    reason=candidate.reason,
                    matched_file_ids=[item.file_id for item in mapping.candidates],
                )
            ],
        )

    matched_file = mapping.candidates[0]
    file_record = session.get(File, matched_file.file_id)
    if file_record is None:
        return ([], [])

    source_path = Path(file_record.path)
    if config.paths.atmos_dir is not None and source_path.resolve().is_relative_to(
        config.paths.atmos_dir.resolve()
    ):
        return (
            [],
            [
                LowConfidencePlan(
                    candidate_id=candidate.id,
                    object_type=candidate.object_type,
                    object_id=candidate.object_id,
                    planned_action_at=_safe_datetime(candidate.planned_action_at),
                    reason=f"{candidate.reason}:atmos_immune",
                    matched_file_ids=[matched_file.file_id],
                )
            ],
        )

    destination = _build_quarantine_destination(
        source_path,
        quarantine_root=config.paths.quarantine_dir,  # type: ignore[arg-type]
        library_roots=config.paths.library_roots,
        date_folder=date_folder,
    )

    return (
        [
            QuarantineMovePlan(
                candidate_id=candidate.id,
                file_id=matched_file.file_id,
                source=source_path,
                destination=destination,
                reason=candidate.reason,
                planned_action_at=_safe_datetime(candidate.planned_action_at),
                object_type=candidate.object_type,
                object_id=candidate.object_id,
            )
        ],
        [],
    )


def build_plan(
    session: Session,
    config: HsajConfig,
    *,
    now: datetime | None = None,
    atmos_detection_fn: Callable[[Path], bool] | None = None,
) -> Plan:
    current_time = now or datetime.now(timezone.utc)
    if config.paths.quarantine_dir is None:
        raise ValueError("В конфиге не задан paths.quarantine_dir")

    date_folder = current_time.date().isoformat()
    atmos_moves: list[AtmosMovePlan] = []
    if config.paths.atmos_dir is not None:
        if atmos_detection_fn is not None:
            atmos_moves = plan_atmos_moves(
                session=session,
                atmos_root=config.paths.atmos_dir,
                detection_fn=atmos_detection_fn,
            )
        else:
            atmos_moves = plan_atmos_moves(session=session, atmos_root=config.paths.atmos_dir)

    blocked_quarantine_due: list[QuarantineMovePlan] = []
    blocked_quarantine_future: list[QuarantineMovePlan] = []
    low_confidence: list[LowConfidencePlan] = []

    candidates = session.scalars(
        select(BlockCandidate).where(BlockCandidate.status == "planned")
    ).all()
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
        if not candidate_moves:
            continue

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
