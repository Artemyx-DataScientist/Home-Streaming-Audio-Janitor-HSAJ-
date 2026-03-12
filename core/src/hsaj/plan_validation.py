from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .atmos import AtmosMovePlan
from .config import HsajConfig
from .db.models import BlockCandidate, File
from .exemptions import match_file_exemption
from .planner import Plan, QuarantineMovePlan


@dataclass(slots=True)
class ValidationIssue:
    move_type: str
    file_id: int
    candidate_id: int | None
    code: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class PlanValidationResult:
    filtered_plan: Plan
    issues: list[ValidationIssue]

    @property
    def valid(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "issue_count": len(self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
            "counts": {
                "atmos_moves": len(self.filtered_plan.atmos_moves),
                "blocked_quarantine_due": len(self.filtered_plan.blocked_quarantine_due),
                "blocked_quarantine_future": len(self.filtered_plan.blocked_quarantine_future),
            },
        }


def validate_plan(session: Session, config: HsajConfig, plan: Plan) -> PlanValidationResult:
    valid_atmos_moves: list[AtmosMovePlan] = []
    valid_quarantine_due: list[QuarantineMovePlan] = []
    valid_quarantine_future: list[QuarantineMovePlan] = []
    issues: list[ValidationIssue] = []

    for move in plan.atmos_moves:
        issue = _validate_atmos_move(session, move)
        if issue is None:
            valid_atmos_moves.append(move)
        else:
            issues.append(issue)

    for move in plan.blocked_quarantine_due:
        issue = _validate_quarantine_move(session, config, move)
        if issue is None:
            valid_quarantine_due.append(move)
        else:
            issues.append(issue)

    for move in plan.blocked_quarantine_future:
        issue = _validate_quarantine_move(session, config, move)
        if issue is None:
            valid_quarantine_future.append(move)
        else:
            issues.append(issue)

    return PlanValidationResult(
        filtered_plan=Plan(
            atmos_moves=valid_atmos_moves,
            blocked_quarantine_due=valid_quarantine_due,
            blocked_quarantine_future=valid_quarantine_future,
            low_confidence=plan.low_confidence,
            soft_candidates=plan.soft_candidates,
        ),
        issues=issues,
    )


def _validate_atmos_move(session: Session, move: AtmosMovePlan) -> ValidationIssue | None:
    file_record = session.get(File, move.file_id)
    if file_record is None:
        return ValidationIssue(
            move_type="atmos",
            file_id=move.file_id,
            candidate_id=None,
            code="file_missing",
            detail="File record no longer exists",
        )
    if Path(file_record.path) != move.source:
        return ValidationIssue(
            move_type="atmos",
            file_id=move.file_id,
            candidate_id=None,
            code="file_path_changed",
            detail=f"Stored file path is now {file_record.path}",
        )
    if not move.source.exists():
        return ValidationIssue(
            move_type="atmos",
            file_id=move.file_id,
            candidate_id=None,
            code="source_missing",
            detail=f"Source path does not exist: {move.source}",
        )
    if move.destination.exists():
        return ValidationIssue(
            move_type="atmos",
            file_id=move.file_id,
            candidate_id=None,
            code="destination_exists",
            detail=f"Destination already exists: {move.destination}",
        )
    return None


def _validate_quarantine_move(
    session: Session,
    config: HsajConfig,
    move: QuarantineMovePlan,
) -> ValidationIssue | None:
    file_record = session.get(File, move.file_id)
    if file_record is None:
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="file_missing",
            detail="File record no longer exists",
        )
    if Path(file_record.path) != move.source:
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="file_path_changed",
            detail=f"Stored file path is now {file_record.path}",
        )
    if move.candidate_id > 0:
        candidate = session.get(BlockCandidate, move.candidate_id)
        if candidate is None:
            return ValidationIssue(
                move_type="quarantine",
                file_id=move.file_id,
                candidate_id=move.candidate_id,
                code="candidate_missing",
                detail="Candidate no longer exists",
            )
        if candidate.status != "planned":
            return ValidationIssue(
                move_type="quarantine",
                file_id=move.file_id,
                candidate_id=move.candidate_id,
                code="candidate_not_planned",
                detail=f"Candidate status is {candidate.status}",
            )
    if not move.source.exists():
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="source_missing",
            detail=f"Source path does not exist: {move.source}",
        )
    if move.destination.exists():
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="destination_exists",
            detail=f"Destination already exists: {move.destination}",
        )
    if file_record.atmos_detected:
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="atmos_immune",
            detail="File is marked as Atmos and cannot be quarantined",
        )
    if config.paths.atmos_dir is not None and _is_path_within(move.source, config.paths.atmos_dir):
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="atmos_immune",
            detail="File is already under atmos_dir",
        )
    if match_file_exemption(session, file_record) is not None:
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="exempt",
            detail="File matches an active exemption",
        )
    if config.paths.quarantine_dir is not None and _is_path_within(
        move.source, config.paths.quarantine_dir
    ):
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="already_quarantined",
            detail="File is already under quarantine_dir",
        )
    if not any(_is_path_within(move.source, root) for root in config.paths.library_roots):
        return ValidationIssue(
            move_type="quarantine",
            file_id=move.file_id,
            candidate_id=move.candidate_id or None,
            code="outside_library_roots",
            detail="File is no longer inside configured library roots",
        )
    return None


def _is_path_within(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False
