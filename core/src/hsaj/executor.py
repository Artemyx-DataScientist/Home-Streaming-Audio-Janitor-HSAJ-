from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .atmos import AtmosMovePlan
from .config import HsajConfig
from .db.models import ActionLog, BlockCandidate, File
from .planner import Plan, QuarantineMovePlan

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
) -> None:
    session.add(
        ActionLog(
            action=action,
            target_path=str(target_path),
            details=json.dumps(details),
        )
    )


def _already_quarantined(candidate: BlockCandidate) -> bool:
    return candidate.status == "quarantined"


def _apply_atmos_moves(
    session: Session,
    moves: Iterable[AtmosMovePlan],
    *,
    dry_run: bool,
) -> list[AtmosMovePlan]:
    applied: list[AtmosMovePlan] = []
    for move in moves:
        if not move.source.exists() and move.destination.exists():
            continue
        if move.destination.exists():
            continue
        if not move.source.exists():
            logger.warning("Источник отсутствует, пропускаем перемещение Atmos: %s", move.source)
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
            )
        applied.append(move)
    return applied


def _apply_quarantine_moves(
    session: Session,
    moves: Iterable[QuarantineMovePlan],
    *,
    dry_run: bool,
) -> list[QuarantineMovePlan]:
    quarantined: list[QuarantineMovePlan] = []
    for move in moves:
        candidate = session.get(BlockCandidate, move.candidate_id)
        if candidate is None:
            continue
        if _already_quarantined(candidate):
            continue
        if move.destination.exists() and not move.source.exists():
            continue
        if not move.source.exists():
            logger.warning("Источник отсутствует, пропускаем перенос в карантин: %s", move.source)
            continue

        if not dry_run:
            move.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), move.destination)
            file_record = session.get(File, move.file_id)
            if file_record is not None:
                file_record.path = str(move.destination)
            candidate.status = "quarantined"
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
                },
            )
        quarantined.append(move)
    return quarantined


def apply_plan(
    session: Session,
    config: HsajConfig,
    plan: Plan,
    *,
    dry_run: bool = False,
) -> ApplyResult:
    if dry_run:
        _log_action(
            session=session,
            action="dry_run",
            target_path=Path("."),
            details={"command": "apply"},
        )
        session.commit()
        return ApplyResult(applied_atmos=[], quarantined=[], skipped=[], dry_run=True)

    if config.paths.quarantine_dir is None:
        raise ValueError("В конфиге не задан paths.quarantine_dir")

    config.paths.quarantine_dir.mkdir(parents=True, exist_ok=True)

    applied_atmos = _apply_atmos_moves(session=session, moves=plan.atmos_moves, dry_run=dry_run)
    quarantined = _apply_quarantine_moves(
        session=session,
        moves=plan.blocked_quarantine_due,
        dry_run=dry_run,
    )

    session.commit()
    return ApplyResult(
        applied_atmos=applied_atmos,
        quarantined=quarantined,
        skipped=[],
        dry_run=False,
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
            candidate.restored_at = datetime.now(timezone.utc)

    _log_action(
        session=session,
        action="restore_from_quarantine",
        target_path=original_path,
        details={"from": str(target_path), "file_id": file_record.id if file_record else None},
    )
    session.commit()
    return RestoreResult(
        restored_path=target_path,
        original_path=original_path,
        conflict=False,
        logged=True,
    )
