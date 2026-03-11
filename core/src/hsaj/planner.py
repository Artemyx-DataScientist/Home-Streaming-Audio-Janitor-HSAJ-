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
from .db.models import BlockCandidate, File, LibraryTrack, PlayHistory, RoonItemCache
from .exemptions import match_file_exemption
from .reviews import latest_soft_candidate_actions
from .roon import RoonTrack, match_track_by_metadata
from .timeutils import ensure_utc, utc_isoformat, utc_now

BLOCK_PRIORITY = {"track": 0, "album": 1, "artist": 2}
FORMAT_RANK = {
    "wav": 100,
    "aiff": 95,
    "flac": 90,
    "alac": 85,
    "dsf": 80,
    "m4a": 70,
    "aac": 65,
    "ogg": 60,
    "opus": 60,
    "mp3": 50,
    "wma": 40,
}


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
    explanation: dict[str, object]


@dataclass(slots=True)
class LowConfidencePlan:
    candidate_id: int
    object_type: str
    object_id: str
    planned_action_at: datetime | None
    reason: str
    matched_file_ids: list[int]
    explanation: dict[str, object]


@dataclass(slots=True)
class SoftCandidatePlan:
    file_id: int
    source: Path
    reason: str
    evidence: dict[str, object]


@dataclass(slots=True)
class Plan:
    atmos_moves: list[AtmosMovePlan]
    blocked_quarantine_due: list[QuarantineMovePlan]
    blocked_quarantine_future: list[QuarantineMovePlan]
    low_confidence: list[LowConfidencePlan]
    soft_candidates: list[SoftCandidatePlan]

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

        def _serialize_soft(item: SoftCandidatePlan) -> dict[str, object]:
            payload = asdict(item)
            payload["source"] = _serialize_path(item.source)
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
            "soft_candidates": [_serialize_soft(item) for item in self.soft_candidates],
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


def _normalized_lookup(value: str | None) -> str | None:
    text = _metadata_string(value)
    if text is None:
        return None
    return text.casefold()


def _candidate_low_confidence(
    candidate: BlockCandidate,
    *,
    reason: str | None = None,
    matched_file_ids: list[int] | None = None,
) -> LowConfidencePlan:
    explanation = {
        "reason": reason or candidate.reason,
        "rule_id": candidate.rule_id,
        "source": candidate.source,
        "object_type": candidate.object_type,
        "object_id": candidate.object_id,
    }
    return LowConfidencePlan(
        candidate_id=candidate.id,
        object_type=candidate.object_type,
        object_id=candidate.object_id,
        planned_action_at=_safe_datetime(candidate.planned_action_at),
        reason=reason or candidate.reason,
        matched_file_ids=matched_file_ids or [],
        explanation=explanation,
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
    graph_size = session.scalar(select(func.count()).select_from(LibraryTrack)) or 0

    if candidate.object_type == "artist":
        artist = artist or _metadata_string(candidate.label)
        if artist is None:
            return ([], [_candidate_low_confidence(candidate)])
        if graph_size > 0:
            normalized_artist = _normalized_lookup(artist)
            matches = session.scalars(
                select(File)
                .join(LibraryTrack, LibraryTrack.file_id == File.id)
                .where(LibraryTrack.normalized_artist_name == normalized_artist)
                .order_by(File.id.asc())
            ).all()
        else:
            matches = session.scalars(
                select(File).where(func.lower(File.artist) == artist.lower())
            ).all()
    elif candidate.object_type == "album":
        album = album or _metadata_string(candidate.label)
        if album is None:
            return ([], [_candidate_low_confidence(candidate)])
        if graph_size > 0:
            normalized_album = _normalized_lookup(album)
            filters = [LibraryTrack.normalized_album_title == normalized_album]
            if artist is not None:
                filters.append(LibraryTrack.normalized_artist_name == _normalized_lookup(artist))
            matches = session.scalars(
                select(File)
                .join(LibraryTrack, LibraryTrack.file_id == File.id)
                .where(*filters)
                .order_by(File.id.asc())
            ).all()
        else:
            filters = [func.lower(File.album) == album.lower()]
            if artist is not None:
                filters.append(func.lower(File.artist) == artist.lower())
            matches = session.scalars(select(File).where(*filters)).all()
    else:
        return (
            [],
            [_candidate_low_confidence(candidate, reason=f"{candidate.reason}:unsupported_type")],
        )

    eligible_matches = [item for item in matches if _is_file_eligible(item, config)]
    if not eligible_matches:
        return ([], [_candidate_low_confidence(candidate)])
    return (eligible_matches, [])


def _build_move_for_file(
    session: Session,
    candidate: BlockCandidate,
    file_record: File,
    config: HsajConfig,
    *,
    date_folder: str,
) -> tuple[QuarantineMovePlan | None, LowConfidencePlan | None]:
    source_path = Path(file_record.path)
    exemption = match_file_exemption(session, file_record)
    if exemption is not None:
        return (
            None,
            _candidate_low_confidence(
                candidate,
                reason=f"{candidate.reason}:exempt",
                matched_file_ids=[file_record.id],
            ),
        )
    if file_record.atmos_detected:
        return (
            None,
            _candidate_low_confidence(
                candidate,
                reason=f"{candidate.reason}:atmos_immune",
                matched_file_ids=[file_record.id],
            ),
        )
    if config.paths.atmos_dir is not None and _is_path_within(source_path, config.paths.atmos_dir):
        return (
            None,
            _candidate_low_confidence(
                candidate,
                reason=f"{candidate.reason}:atmos_immune",
                matched_file_ids=[file_record.id],
            ),
        )
    if not any(
        _is_path_within(source_path, library_root) for library_root in config.paths.library_roots
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
            explanation={
                "rule_id": candidate.rule_id,
                "source": candidate.source,
                "candidate_reason": candidate.reason,
                "matched_artist": file_record.artist,
                "matched_album": file_record.album,
                "matched_title": file_record.title,
                "matched_track_number": file_record.track_number,
            },
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
            session,
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
        candidate_moves = [move for move in candidate_moves if move.file_id not in planned_file_ids]
        if not candidate_moves:
            continue
        planned_file_ids.update(move.file_id for move in candidate_moves)

        target_bucket: list[QuarantineMovePlan]
        if planned_at is not None and planned_at <= current_time:
            target_bucket = blocked_quarantine_due
        else:
            target_bucket = blocked_quarantine_future
        target_bucket.extend(candidate_moves)

    soft_candidates = _build_soft_candidates(
        session=session,
        config=config,
        now=current_time,
        planned_file_ids=planned_file_ids,
    )

    return Plan(
        atmos_moves=atmos_moves,
        blocked_quarantine_due=blocked_quarantine_due,
        blocked_quarantine_future=blocked_quarantine_future,
        low_confidence=low_confidence,
        soft_candidates=soft_candidates,
    )


def _build_soft_candidates(
    session: Session,
    config: HsajConfig,
    *,
    now: datetime,
    planned_file_ids: set[int],
) -> list[SoftCandidatePlan]:
    if not config.policy.enable_behavior_scoring:
        return []

    reviewed_actions = latest_soft_candidate_actions(session)

    files = session.scalars(select(File).order_by(File.id.asc())).all()
    play_signatures = {
        (
            (entry.artist or "").casefold(),
            (entry.album or "").casefold(),
            (entry.title or "").casefold(),
        )
        for entry in session.scalars(select(PlayHistory)).all()
    }

    duplicate_groups: dict[tuple[str, str, str, int | None], list[File]] = {}
    for file_record in files:
        key = (
            (file_record.artist or "").casefold(),
            (file_record.album or "").casefold(),
            (file_record.title or "").casefold(),
            file_record.track_number,
        )
        duplicate_groups.setdefault(key, []).append(file_record)

    duplicate_losers: dict[int, dict[str, object]] = {}
    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        ranked = sorted(
            group,
            key=lambda item: (
                -FORMAT_RANK.get((item.format or "").casefold(), 0),
                -(item.size_bytes or 0),
                item.id,
            ),
        )
        winner = ranked[0]
        for loser in ranked[1:]:
            duplicate_losers[loser.id] = {
                "winner_file_id": winner.id,
                "winner_path": winner.path,
                "winner_format": winner.format,
                "loser_format": loser.format,
            }

    soft_candidates: list[SoftCandidatePlan] = []
    for file_record in files:
        if file_record.id in planned_file_ids:
            continue
        source_path = Path(file_record.path)
        if not source_path.exists():
            continue
        if file_record.atmos_detected:
            continue
        if config.paths.quarantine_dir is not None and _is_path_within(
            source_path, config.paths.quarantine_dir
        ):
            continue
        if match_file_exemption(session, file_record) is not None:
            continue

        duplicate_evidence = duplicate_losers.get(file_record.id)
        if duplicate_evidence is not None:
            if _is_soft_candidate_suppressed(
                reviewed_actions, file_record.id, "duplicate_lower_quality"
            ):
                continue
            soft_candidates.append(
                SoftCandidatePlan(
                    file_id=file_record.id,
                    source=source_path,
                    reason="duplicate_lower_quality",
                    evidence=duplicate_evidence,
                )
            )
            continue

        signature = (
            (file_record.artist or "").casefold(),
            (file_record.album or "").casefold(),
            (file_record.title or "").casefold(),
        )
        if (
            file_record.mtime is not None
            and signature not in play_signatures
            and (now - file_record.mtime).days >= config.policy.soft_never_played_days
        ):
            if _is_soft_candidate_suppressed(reviewed_actions, file_record.id, "never_played_old"):
                continue
            soft_candidates.append(
                SoftCandidatePlan(
                    file_id=file_record.id,
                    source=source_path,
                    reason="never_played_old",
                    evidence={"age_days": (now - file_record.mtime).days},
                )
            )
            continue

        if config.paths.inbox_dir is not None and _is_path_within(
            source_path, config.paths.inbox_dir
        ):
            age_days = (now - (file_record.mtime or now)).days
            if age_days >= config.policy.soft_inbox_days:
                if _is_soft_candidate_suppressed(reviewed_actions, file_record.id, "stale_inbox"):
                    continue
                soft_candidates.append(
                    SoftCandidatePlan(
                        file_id=file_record.id,
                        source=source_path,
                        reason="stale_inbox",
                        evidence={"age_days": age_days},
                    )
                )

    return soft_candidates


def _is_soft_candidate_suppressed(
    reviewed_actions: dict[tuple[int, str], object],
    file_id: int,
    reason: str,
) -> bool:
    decision = reviewed_actions.get((file_id, reason))
    if decision is None:
        return False
    action = getattr(decision, "action", None)
    return action in {"dismissed", "exempted"}


def build_soft_review_plan(
    session: Session,
    config: HsajConfig,
    *,
    selections: Sequence[tuple[int, str]],
    now: datetime | None = None,
) -> Plan:
    current_time = ensure_utc(now) or utc_now()
    if config.paths.quarantine_dir is None:
        raise ValueError("paths.quarantine_dir must be configured")

    current_plan = build_plan(session=session, config=config, now=current_time)
    soft_candidates_by_key = {
        (candidate.file_id, candidate.reason): candidate
        for candidate in current_plan.soft_candidates
    }
    date_folder = current_time.date().isoformat()
    review_moves: list[QuarantineMovePlan] = []

    for selection in selections:
        soft_candidate = soft_candidates_by_key.get(selection)
        if soft_candidate is None:
            raise KeyError(f"soft_candidate:{selection[0]}:{selection[1]}")
        file_record = session.get(File, soft_candidate.file_id)
        if file_record is None or not _is_file_eligible(file_record, config):
            raise KeyError(f"soft_candidate_file:{selection[0]}:{selection[1]}")

        destination = _build_quarantine_destination(
            Path(file_record.path),
            quarantine_root=config.paths.quarantine_dir,
            library_roots=config.paths.library_roots,
            date_folder=date_folder,
        )
        review_moves.append(
            QuarantineMovePlan(
                candidate_id=0,
                file_id=file_record.id,
                source=Path(file_record.path),
                destination=destination,
                reason=f"soft_review:{soft_candidate.reason}",
                planned_action_at=current_time,
                object_type="soft_candidate",
                object_id=f"file:{file_record.id}",
                explanation={
                    "source": "operator.review.v1",
                    "review_type": "soft_candidate",
                    "review_reason": soft_candidate.reason,
                    "evidence": soft_candidate.evidence,
                    "matched_artist": file_record.artist,
                    "matched_album": file_record.album,
                    "matched_title": file_record.title,
                    "matched_track_number": file_record.track_number,
                },
            )
        )

    return Plan(
        atmos_moves=[],
        blocked_quarantine_due=review_moves,
        blocked_quarantine_future=[],
        low_confidence=[],
        soft_candidates=current_plan.soft_candidates,
    )


def plan_from_dict(payload: dict[str, object]) -> Plan:
    return Plan(
        atmos_moves=[
            AtmosMovePlan(
                file_id=int(item["file_id"]),
                source=Path(str(item["source"])),
                destination=Path(str(item["destination"])),
                artist=item.get("artist"),
                album=item.get("album"),
            )
            for item in payload.get("atmos_moves", [])
        ],
        blocked_quarantine_due=[
            QuarantineMovePlan(
                candidate_id=int(item["candidate_id"]),
                file_id=int(item["file_id"]),
                source=Path(str(item["source"])),
                destination=Path(str(item["destination"])),
                reason=str(item["reason"]),
                planned_action_at=(
                    ensure_utc(datetime.fromisoformat(item["planned_action_at"]))
                    if item.get("planned_action_at")
                    else None
                ),
                object_type=str(item["object_type"]),
                object_id=str(item["object_id"]),
                explanation=dict(item.get("explanation", {})),
            )
            for item in payload.get("blocked_quarantine_due", [])
        ],
        blocked_quarantine_future=[
            QuarantineMovePlan(
                candidate_id=int(item["candidate_id"]),
                file_id=int(item["file_id"]),
                source=Path(str(item["source"])),
                destination=Path(str(item["destination"])),
                reason=str(item["reason"]),
                planned_action_at=(
                    ensure_utc(datetime.fromisoformat(item["planned_action_at"]))
                    if item.get("planned_action_at")
                    else None
                ),
                object_type=str(item["object_type"]),
                object_id=str(item["object_id"]),
                explanation=dict(item.get("explanation", {})),
            )
            for item in payload.get("blocked_quarantine_future", [])
        ],
        low_confidence=[
            LowConfidencePlan(
                candidate_id=int(item["candidate_id"]),
                object_type=str(item["object_type"]),
                object_id=str(item["object_id"]),
                planned_action_at=(
                    ensure_utc(datetime.fromisoformat(item["planned_action_at"]))
                    if item.get("planned_action_at")
                    else None
                ),
                reason=str(item["reason"]),
                matched_file_ids=[int(value) for value in item.get("matched_file_ids", [])],
                explanation=dict(item.get("explanation", {})),
            )
            for item in payload.get("low_confidence", [])
        ],
        soft_candidates=[
            SoftCandidatePlan(
                file_id=int(item["file_id"]),
                source=Path(str(item["source"])),
                reason=str(item["reason"]),
                evidence=dict(item.get("evidence", {})),
            )
            for item in payload.get("soft_candidates", [])
        ],
    )
