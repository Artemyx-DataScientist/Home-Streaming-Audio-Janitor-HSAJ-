from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .atmos import AtmosMovePlan
from .config import HsajConfig
from .db.models import ActionLog, BlockCandidate, File
from .exemptions import match_file_exemption
from .planner import Plan, QuarantineMovePlan
from .timeutils import utc_isoformat, utc_now

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplyResult:
    applied_atmos: list[AtmosMovePlan]
    quarantined: list[QuarantineMovePlan]
    skipped: list[str]
    dry_run: bool


def _log_action(
    session: Session,
    action: str,
    target_path: Path,
    details: dict[str, object],
    *,
    request_id: str | None = None,
    plan_id: str | None = None,
) -> None:
    session.add(
        ActionLog(
            action=action,
            target_path=str(target_path),
            details=json.dumps(details),
            request_id=request_id,
            plan_id=plan_id,
        )
    )


def _log_plan(
    session: Session,
    plan: Plan,
    *,
    dry_run: bool,
    request_id: str | None = None,
    plan_id: str | None = None,
) -> None:
    payload = plan.to_dict()
    payload.update(
        {
            "command": "apply",
            "dry_run": dry_run,
            "generated_at": utc_isoformat(utc_now()),
            "counts": {
                "atmos_moves": len(plan.atmos_moves),
                "blocked_quarantine_due": len(plan.blocked_quarantine_due),
                "blocked_quarantine_future": len(plan.blocked_quarantine_future),
                "low_confidence": len(plan.low_confidence),
                "soft_candidates": len(plan.soft_candidates),
            },
        }
    )
    _log_action(
        session=session,
        action="plan",
        target_path=Path("."),
        details=payload,
        request_id=request_id,
        plan_id=plan_id,
    )


def _apply_atmos_moves(
    session: Session,
    moves: Iterable[AtmosMovePlan],
    *,
    dry_run: bool,
    request_id: str | None = None,
    plan_id: str | None = None,
) -> list[AtmosMovePlan]:
    applied: list[AtmosMovePlan] = []
    for move in moves:
        if not move.source.exists() and move.destination.exists():
            continue
        if move.destination.exists():
            continue
        if not move.source.exists():
            logger.warning("Source is missing, skipping Atmos move: %s", move.source)
            continue

        if not dry_run:
            move.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), move.destination)
            file_record = session.get(File, move.file_id)
            if file_record is not None:
                file_record.path = str(move.destination)
            _log_action(
                session=session,
                action="move_to_atmos",
                target_path=move.destination,
                details={"from": str(move.source), "file_id": move.file_id},
                request_id=request_id,
                plan_id=plan_id,
            )
        applied.append(move)
    return applied


def _apply_quarantine_moves(
    session: Session,
    moves: Iterable[QuarantineMovePlan],
    *,
    dry_run: bool,
    config: HsajConfig,
    request_id: str | None = None,
    plan_id: str | None = None,
) -> list[QuarantineMovePlan]:
    quarantined: list[QuarantineMovePlan] = []
    for move in moves:
        candidate = (
            session.get(BlockCandidate, move.candidate_id) if move.candidate_id > 0 else None
        )
        if move.candidate_id > 0 and candidate is None:
            continue
        if move.destination.exists() and not move.source.exists():
            continue
        if not move.source.exists():
            logger.warning("Source is missing, skipping quarantine move: %s", move.source)
            continue
        file_record = session.get(File, move.file_id)
        if file_record is None:
            continue
        if match_file_exemption(session, file_record) is not None:
            logger.info("Skipping exempt file during apply: %s", file_record.path)
            continue

        if not dry_run:
            move.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), move.destination)
            file_record.path = str(move.destination)
            if candidate is not None:
                candidate.status = "quarantined"
                candidate.last_transition_at = utc_now()
            if candidate is not None and config.policy.quarantine_delete_days > 0:
                candidate.delete_after = utc_now() + timedelta(
                    days=config.policy.quarantine_delete_days
                )
            _log_action(
                session=session,
                action="quarantine_move",
                target_path=move.destination,
                details={
                    "from": str(move.source),
                    "file_id": move.file_id,
                    "candidate_id": move.candidate_id,
                    "reason": move.reason,
                    "object_type": move.object_type,
                    "object_id": move.object_id,
                    "explanation": move.explanation,
                },
                request_id=request_id,
                plan_id=plan_id,
            )
        quarantined.append(move)
    return quarantined


def apply_plan(
    session: Session,
    config: HsajConfig,
    plan: Plan,
    *,
    dry_run: bool = False,
    request_id: str | None = None,
    plan_id: str | None = None,
) -> ApplyResult:
    _log_plan(
        session=session,
        plan=plan,
        dry_run=dry_run,
        request_id=request_id,
        plan_id=plan_id,
    )

    if dry_run:
        _log_action(
            session=session,
            action="dry_run",
            target_path=Path("."),
            details={"command": "apply"},
            request_id=request_id,
            plan_id=plan_id,
        )
        session.commit()
        return ApplyResult(applied_atmos=[], quarantined=[], skipped=[], dry_run=True)

    if config.paths.quarantine_dir is None:
        raise ValueError("paths.quarantine_dir must be configured")

    config.paths.quarantine_dir.mkdir(parents=True, exist_ok=True)

    applied_atmos = _apply_atmos_moves(
        session=session,
        moves=plan.atmos_moves,
        dry_run=dry_run,
        request_id=request_id,
        plan_id=plan_id,
    )
    quarantined = _apply_quarantine_moves(
        session=session,
        moves=plan.blocked_quarantine_due,
        dry_run=dry_run,
        config=config,
        request_id=request_id,
        plan_id=plan_id,
    )

    session.commit()
    return ApplyResult(
        applied_atmos=applied_atmos,
        quarantined=quarantined,
        skipped=[],
        dry_run=False,
    )


@dataclass(slots=True)
class CleanupResult:
    deleted_candidates: list[int]
    expired_candidates: list[int]


def cleanup_retention(
    session: Session,
    config: HsajConfig,
    *,
    now=None,
    request_id: str | None = None,
) -> CleanupResult:
    current_time = now or utc_now()
    deleted_candidates: list[int] = []
    expired_candidates: list[int] = []
    candidates = session.scalars(
        select(BlockCandidate).where(BlockCandidate.status == "quarantined")
    ).all()
    for candidate in candidates:
        if candidate.delete_after is None or candidate.delete_after > current_time:
            continue
        quarantine_logs = session.scalars(
            select(ActionLog)
            .where(ActionLog.action == "quarantine_move")
            .order_by(ActionLog.id.asc())
        ).all()
        matching_file_ids: list[int] = []
        for log_entry in quarantine_logs:
            try:
                details = json.loads(log_entry.details or "{}")
            except json.JSONDecodeError:
                continue
            if int(details.get("candidate_id", -1)) != candidate.id:
                continue
            file_id = details.get("file_id")
            if isinstance(file_id, int):
                matching_file_ids.append(file_id)

        matched_files = [session.get(File, file_id) for file_id in matching_file_ids]
        matched_files = [file_record for file_record in matched_files if file_record is not None]
        if not matched_files:
            candidate.status = "expired"
            candidate.last_transition_at = current_time
            expired_candidates.append(candidate.id)
            continue

        for file_record in matched_files:
            target_path = Path(file_record.path)
            if config.policy.auto_delete:
                if target_path.exists():
                    target_path.unlink()
                session.delete(file_record)
                deleted_candidates.append(candidate.id)
                _log_action(
                    session=session,
                    action="quarantine_delete",
                    target_path=target_path,
                    details={"candidate_id": candidate.id, "file_id": file_record.id},
                    request_id=request_id,
                )
            else:
                expired_candidates.append(candidate.id)
                _log_action(
                    session=session,
                    action="quarantine_expired",
                    target_path=target_path,
                    details={"candidate_id": candidate.id, "file_id": file_record.id},
                    request_id=request_id,
                )
        candidate.status = "deleted" if config.policy.auto_delete else "expired"
        candidate.last_transition_at = current_time
    session.commit()
    return CleanupResult(
        deleted_candidates=deleted_candidates,
        expired_candidates=expired_candidates,
    )


@dataclass(slots=True)
class RestoreResult:
    restored_path: Path | None
    original_path: Path | None
    conflict: bool
    logged: bool


def _find_quarantine_log(session: Session, target: Path) -> ActionLog | None:
    return session.scalars(
        select(ActionLog)
        .where(ActionLog.action == "quarantine_move", ActionLog.target_path == str(target))
        .order_by(ActionLog.id.desc())
    ).first()


def restore_from_quarantine(session: Session, target: Path | int) -> RestoreResult:
    file_record: File | None = None
    target_path: Path
    if isinstance(target, int):
        file_record = session.get(File, target)
        if file_record is None:
            return RestoreResult(
                restored_path=None,
                original_path=None,
                conflict=False,
                logged=False,
            )
        target_path = Path(file_record.path)
    else:
        target_path = target

    log_entry = _find_quarantine_log(session=session, target=target_path)
    if log_entry is None:
        return RestoreResult(restored_path=None, original_path=None, conflict=False, logged=False)

    try:
        details = json.loads(log_entry.details or "{}")
    except json.JSONDecodeError:
        details = {}
    original_path_str = details.get("from")
    if not original_path_str:
        return RestoreResult(restored_path=None, original_path=None, conflict=False, logged=False)

    original_path = Path(original_path_str)
    if original_path.exists():
        _log_action(
            session=session,
            action="restore_conflict",
            target_path=target_path,
            details={"conflict_with": str(original_path)},
        )
        session.commit()
        return RestoreResult(
            restored_path=None,
            original_path=original_path,
            conflict=True,
            logged=True,
        )

    if not target_path.exists():
        return RestoreResult(
            restored_path=None,
            original_path=original_path,
            conflict=False,
            logged=False,
        )

    original_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target_path), original_path)

    file_record = file_record or session.scalar(select(File).where(File.path == str(target_path)))
    if file_record is not None:
        file_record.path = str(original_path)

    candidate_id = details.get("candidate_id")
    if candidate_id is not None:
        candidate = session.get(BlockCandidate, int(candidate_id))
        if candidate is not None:
            candidate.status = "restored"
            candidate.restored_at = utc_now()
            candidate.delete_after = None
            candidate.last_transition_at = utc_now()

    _log_action(
        session=session,
        action="restore_from_quarantine",
        target_path=original_path,
        details={
            "from": str(target_path),
            "file_id": file_record.id if file_record else None,
        },
    )
    session.commit()
    return RestoreResult(
        restored_path=target_path,
        original_path=original_path,
        conflict=False,
        logged=True,
    )
