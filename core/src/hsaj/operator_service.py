from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import HsajConfig
from .db.models import (
    ActionLog,
    BlockCandidate,
    BridgeSyncStatus,
    File,
    PlayHistory,
    ReviewDecision,
    RuntimeJobStatus,
)
from .executor import apply_plan, cleanup_retention, restore_from_quarantine
from .exemptions import add_exemption, deactivate_exemption, list_exemptions
from .plan_runs import create_plan_run, load_plan_run, mark_plan_applied
from .plan_validation import validate_plan
from .planner import Plan, build_plan, build_soft_review_plan
from .reviews import add_review_decision, latest_soft_candidate_actions, list_review_decisions
from .runtime_jobs import JOB_BLOCKED_SYNC, JOB_CLEANUP, list_runtime_job_statuses
from .timeutils import utc_now


def _readiness_check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def _blocked_sync_check(session: Session, config: HsajConfig) -> dict[str, object]:
    blocked_sync = session.get(BridgeSyncStatus, "blocked")
    if blocked_sync is None:
        if config.runtime.enable_background_jobs:
            return _readiness_check(
                "blocked_sync",
                False,
                "Background jobs are enabled, but blocked sync has never run",
            )
        return _readiness_check("blocked_sync", True, "Manual blocked sync mode")

    if blocked_sync.status != "ok":
        return _readiness_check(
            "blocked_sync",
            False,
            f"Last blocked sync status is {blocked_sync.status}",
        )

    expected_contract = (config.bridge.contract_version or "").strip() or None
    actual_contract = (blocked_sync.contract_version or "").strip() or None
    if expected_contract is not None and actual_contract != expected_contract:
        return _readiness_check(
            "blocked_contract",
            False,
            f"Expected blocked contract {expected_contract}, got {actual_contract or 'legacy'}",
        )

    if config.runtime.enable_background_jobs:
        if blocked_sync.last_success_at is None:
            return _readiness_check(
                "blocked_sync_fresh",
                False,
                "Blocked sync has no success timestamp",
            )
        max_age = timedelta(minutes=max(config.runtime.blocked_sync_interval_minutes * 2, 5))
        age = utc_now() - blocked_sync.last_success_at
        if age > max_age:
            return _readiness_check(
                "blocked_sync_fresh",
                False,
                f"Blocked sync is stale by {int(age.total_seconds())}s",
            )

    return _readiness_check("blocked_sync", True, "Blocked sync is healthy")


def readiness_payload(session: Session, config: HsajConfig) -> dict[str, Any]:
    stats = stats_payload(session, config)
    ffprobe_path = config.ffprobe_resolved_path()
    checks = [
        _readiness_check(
            "library_roots",
            bool(config.paths.library_roots)
            and all(root.exists() and root.is_dir() for root in config.paths.library_roots),
            "All configured library roots exist"
            if config.paths.library_roots
            and all(root.exists() and root.is_dir() for root in config.paths.library_roots)
            else "One or more library roots are missing",
        ),
        _readiness_check(
            "quarantine_dir",
            config.paths.quarantine_dir is not None,
            "Quarantine directory is configured"
            if config.paths.quarantine_dir is not None
            else "paths.quarantine_dir is not configured",
        ),
        _readiness_check(
            "ffprobe",
            ffprobe_path is not None and ffprobe_path.exists(),
            f"ffprobe resolved to {ffprobe_path}"
            if ffprobe_path is not None and ffprobe_path.exists()
            else f"Could not resolve ffprobe from {config.paths.ffprobe_path}",
        ),
        _blocked_sync_check(session, config),
    ]
    ready = all(bool(check["ok"]) for check in checks)
    return {
        "status": "ready" if ready else "not_ready",
        "bridge_contract_version": config.bridge.contract_version,
        "counts": stats,
        "checks": checks,
    }


def health_payload(
    session: Session,
    config: HsajConfig,
    *,
    schema_version: str | None,
) -> dict[str, Any]:
    blocked_sync = session.get(BridgeSyncStatus, "blocked")
    return {
        "status": "ok",
        "schema_version": schema_version,
        "bridge_contract_version": config.bridge.contract_version,
        "auth_required": bool(config.security.operator_token),
        "runtime": {
            "background_jobs_enabled": config.runtime.enable_background_jobs,
        },
        "blocked_sync": (
            {
                "status": blocked_sync.status,
                "contract_version": blocked_sync.contract_version,
                "source_mode": blocked_sync.source_mode,
                "item_count": blocked_sync.item_count,
                "snapshot_generated_at": (
                    blocked_sync.snapshot_generated_at.isoformat()
                    if blocked_sync.snapshot_generated_at
                    else None
                ),
                "last_attempt_at": (
                    blocked_sync.last_attempt_at.isoformat()
                    if blocked_sync.last_attempt_at
                    else None
                ),
                "last_success_at": (
                    blocked_sync.last_success_at.isoformat()
                    if blocked_sync.last_success_at
                    else None
                ),
                "last_error": blocked_sync.last_error,
            }
            if blocked_sync is not None
            else None
        ),
        "operator": {
            "host": config.security.operator_host,
            "port": config.security.operator_port,
        },
    }


def liveness_payload(session: Session, config: HsajConfig) -> dict[str, Any]:
    del session, config
    return {"status": "live"}


def stats_payload(session: Session, config: HsajConfig) -> dict[str, Any]:
    del config
    quarantined = session.scalar(
        select(func.count())
        .select_from(BlockCandidate)
        .where(BlockCandidate.status == "quarantined")
    )
    planned = session.scalar(
        select(func.count()).select_from(BlockCandidate).where(BlockCandidate.status == "planned")
    )
    restored = session.scalar(
        select(func.count()).select_from(BlockCandidate).where(BlockCandidate.status == "restored")
    )
    deleted = session.scalar(
        select(func.count()).select_from(BlockCandidate).where(BlockCandidate.status == "deleted")
    )
    expired = session.scalar(
        select(func.count()).select_from(BlockCandidate).where(BlockCandidate.status == "expired")
    )
    reviews_count = session.scalar(select(func.count()).select_from(ReviewDecision))
    blocked_sync = session.get(BridgeSyncStatus, "blocked")
    runtime_jobs = session.scalar(select(func.count()).select_from(RuntimeJobStatus))
    files_count = session.scalar(select(func.count()).select_from(File))
    play_history_count = session.scalar(select(func.count()).select_from(PlayHistory))
    return {
        "files": files_count or 0,
        "play_history": play_history_count or 0,
        "reviews": reviews_count or 0,
        "runtime_jobs": runtime_jobs or 0,
        "blocked_sync": {
            "status": blocked_sync.status if blocked_sync is not None else "never_run",
            "item_count": blocked_sync.item_count if blocked_sync is not None else 0,
        },
        "candidates": {
            "planned": planned or 0,
            "quarantined": quarantined or 0,
            "restored": restored or 0,
            "deleted": deleted or 0,
            "expired": expired or 0,
        },
    }


def metrics_payload(session: Session, config: HsajConfig) -> str:
    stats = stats_payload(session, config)
    metric_prefix = "hsaj_core"
    candidates = stats["candidates"]
    lines = [
        f'{metric_prefix}_files_total {stats["files"]}',
        f'{metric_prefix}_play_history_total {stats["play_history"]}',
        f'{metric_prefix}_review_decisions_total {stats["reviews"]}',
        f'{metric_prefix}_runtime_jobs_total {stats["runtime_jobs"]}',
        f'{metric_prefix}_blocked_sync_item_count {stats["blocked_sync"]["item_count"]}',
        f'{metric_prefix}_candidates_planned {candidates["planned"]}',
        f'{metric_prefix}_candidates_quarantined {candidates["quarantined"]}',
        f'{metric_prefix}_candidates_restored {candidates["restored"]}',
        f'{metric_prefix}_candidates_deleted {candidates["deleted"]}',
        f'{metric_prefix}_candidates_expired {candidates["expired"]}',
        f"{metric_prefix}_operator_auth_required {1 if config.security.operator_token else 0}",
        f"{metric_prefix}_blocked_sync_ok {1 if stats['blocked_sync']['status'] == 'ok' else 0}",
    ]
    return "\n".join(lines) + "\n"


def plan_preview_payload(session: Session, config: HsajConfig) -> dict[str, Any]:
    plan = build_plan(session=session, config=config)
    request_id = uuid4().hex
    plan_run = create_plan_run(session=session, plan=plan, request_id=request_id)
    validation = validate_plan(session, config, plan)
    session.commit()
    return {
        "preview_id": plan_run.id,
        "request_id": request_id,
        "plan": plan.to_dict(),
        "validation": validation.to_dict(),
    }


def validate_preview_payload(
    session: Session,
    config: HsajConfig,
    *,
    preview_id: str,
) -> dict[str, Any]:
    plan_run, stored_plan = load_plan_run(session, preview_id)
    if plan_run is None or stored_plan is None:
        raise KeyError(preview_id)
    validation = validate_plan(session, config, stored_plan)
    if validation.issues and plan_run.status in {"preview", "review_preview"}:
        plan_run.status = "stale"
        session.commit()
    return {
        "preview_id": preview_id,
        "plan_status": plan_run.status,
        "validation": validation.to_dict(),
    }


def apply_preview_payload(
    session: Session,
    config: HsajConfig,
    *,
    preview_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    request_id = uuid4().hex
    plan_id = preview_id
    plan: Plan
    if preview_id:
        plan_run, stored_plan = load_plan_run(session, preview_id)
        if plan_run is None or stored_plan is None:
            raise KeyError(preview_id)
        plan = stored_plan
    else:
        plan = build_plan(session=session, config=config)
        plan_run = create_plan_run(session=session, plan=plan, request_id=request_id)
        plan_id = plan_run.id

    validation = validate_plan(session, config, plan)
    validated_plan = validation.filtered_plan

    result = apply_plan(
        session=session,
        config=config,
        plan=validated_plan,
        dry_run=dry_run,
        request_id=request_id,
        plan_id=plan_id,
    )
    if preview_id:
        plan_run, _ = load_plan_run(session, preview_id)
        if plan_run is not None and not dry_run:
            if plan_run.status == "review_preview":
                for move in result.quarantined:
                    if not move.reason.startswith("soft_review:"):
                        continue
                    add_review_decision(
                        session,
                        review_type="soft_candidate",
                        file_id=move.file_id,
                        path=str(move.destination),
                        candidate_reason=move.reason.removeprefix("soft_review:"),
                        action="quarantined",
                        notes="Applied from operator review preview",
                    )
            mark_plan_applied(
                session,
                plan_run,
                status="applied_partial" if validation.issues else "applied",
            )
            session.commit()

    return {
        "request_id": request_id,
        "preview_id": plan_id,
        "dry_run": dry_run,
        "validation": validation.to_dict(),
        "applied_atmos": [asdict(item) for item in result.applied_atmos],
        "quarantined": [asdict(item) for item in result.quarantined],
        "skipped": result.skipped + [issue.code for issue in validation.issues],
    }


def restore_payload(session: Session, target: str) -> dict[str, Any]:
    try:
        normalized_target: int | Path = int(target)
    except ValueError:
        normalized_target = Path(target)
    result = restore_from_quarantine(session=session, target=normalized_target)
    return {
        "restored": result.restored_path is not None and not result.conflict,
        "conflict": result.conflict,
        "original_path": str(result.original_path) if result.original_path else None,
    }


def candidates_payload(session: Session) -> list[dict[str, Any]]:
    candidates = session.scalars(select(BlockCandidate).order_by(BlockCandidate.id.asc())).all()
    payload: list[dict[str, Any]] = []
    for candidate in candidates:
        payload.append(
            {
                "id": candidate.id,
                "object_type": candidate.object_type,
                "object_id": candidate.object_id,
                "reason": candidate.reason,
                "status": candidate.status,
                "source": candidate.source,
                "rule_id": candidate.rule_id,
                "first_seen_at": candidate.first_seen_at.isoformat(),
                "last_seen_at": candidate.last_seen_at.isoformat(),
                "planned_action_at": (
                    candidate.planned_action_at.isoformat() if candidate.planned_action_at else None
                ),
                "delete_after": (
                    candidate.delete_after.isoformat() if candidate.delete_after else None
                ),
                "explanation_json": candidate.explanation_json,
            }
        )
    return payload


def soft_candidates_payload(session: Session, config: HsajConfig) -> list[dict[str, Any]]:
    plan = build_plan(session=session, config=config)
    latest_reviews = latest_soft_candidate_actions(session)
    payload: list[dict[str, Any]] = []
    for candidate in plan.soft_candidates:
        review = latest_reviews.get((candidate.file_id, candidate.reason))
        payload.append(
            {
                "file_id": candidate.file_id,
                "source": str(candidate.source),
                "reason": candidate.reason,
                "evidence": candidate.evidence,
                "review_status": review.action if review is not None else None,
                "review_notes": review.notes if review is not None else None,
            }
        )
    return payload


def actions_payload(session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    actions = session.scalars(select(ActionLog).order_by(ActionLog.id.desc()).limit(limit)).all()
    return [
        {
            "id": action.id,
            "action": action.action,
            "target_path": action.target_path,
            "details": action.details,
            "request_id": action.request_id,
            "plan_id": action.plan_id,
            "created_at": action.created_at.isoformat(),
        }
        for action in actions
    ]


def cleanup_payload(session: Session, config: HsajConfig) -> dict[str, Any]:
    result = cleanup_retention(session=session, config=config, request_id=uuid4().hex)
    return {
        "deleted_candidates": result.deleted_candidates,
        "expired_candidates": result.expired_candidates,
    }


def reviews_payload(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": decision.id,
            "review_type": decision.review_type,
            "file_id": decision.file_id,
            "path": decision.path,
            "candidate_reason": decision.candidate_reason,
            "action": decision.action,
            "notes": decision.notes,
            "created_at": decision.created_at.isoformat(),
        }
        for decision in list_review_decisions(session)
    ]


def runtime_jobs_payload(session: Session, config: HsajConfig) -> list[dict[str, Any]]:
    configured_jobs = {
        JOB_BLOCKED_SYNC: {
            "interval_minutes": config.runtime.blocked_sync_interval_minutes,
            "run_on_start": config.runtime.blocked_sync_on_start,
        },
        JOB_CLEANUP: {
            "interval_minutes": config.runtime.cleanup_interval_minutes,
            "run_on_start": config.runtime.cleanup_on_start,
        },
    }
    stored = {item.job_name: item for item in list_runtime_job_statuses(session)}
    payload: list[dict[str, Any]] = []
    for job_name, job_config in configured_jobs.items():
        record = stored.get(job_name)
        payload.append(
            {
                "job_name": job_name,
                "background_enabled": config.runtime.enable_background_jobs,
                "interval_minutes": job_config["interval_minutes"],
                "run_on_start": job_config["run_on_start"],
                "status": record.status if record is not None else "never_run",
                "last_attempt_at": (
                    record.last_attempt_at.isoformat()
                    if record and record.last_attempt_at
                    else None
                ),
                "last_success_at": (
                    record.last_success_at.isoformat()
                    if record and record.last_success_at
                    else None
                ),
                "last_error": record.last_error if record is not None else None,
                "last_result_json": record.last_result_json if record is not None else None,
            }
        )
    return payload


def create_soft_review_preview_payload(
    session: Session,
    config: HsajConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    raw_selections = payload.get("selections", [])
    selections = [
        (int(item["file_id"]), str(item["reason"]))
        for item in raw_selections
        if "file_id" in item and "reason" in item
    ]
    if not selections:
        raise KeyError("soft_review_selections")

    plan = build_soft_review_plan(session=session, config=config, selections=selections)
    request_id = uuid4().hex
    plan_run = create_plan_run(
        session=session, plan=plan, request_id=request_id, status="review_preview"
    )
    session.commit()
    return {
        "preview_id": plan_run.id,
        "request_id": request_id,
        "plan": plan.to_dict(),
    }


def create_soft_review_action_payload(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    file_id = int(payload["file_id"])
    candidate_reason = str(payload["reason"])
    action = str(payload["action"])
    notes = payload.get("notes")
    file_record = session.get(File, file_id)
    if file_record is None:
        raise KeyError(file_id)

    if action == "dismiss":
        decision = add_review_decision(
            session,
            review_type="soft_candidate",
            file_id=file_record.id,
            path=file_record.path,
            candidate_reason=candidate_reason,
            action="dismissed",
            notes=notes,
        )
        session.commit()
        return {"id": decision.id, "action": decision.action}

    if action == "exempt":
        exemption = add_exemption(
            session,
            scope_type="file_id",
            file_id=file_record.id,
            reason=str(notes or f"Exempted from soft candidate review: {candidate_reason}"),
        )
        decision = add_review_decision(
            session,
            review_type="soft_candidate",
            file_id=file_record.id,
            path=file_record.path,
            candidate_reason=candidate_reason,
            action="exempted",
            notes=notes,
        )
        session.commit()
        return {
            "id": decision.id,
            "action": decision.action,
            "exemption_id": exemption.id,
        }

    raise KeyError(f"unsupported_soft_review_action:{action}")


def exemptions_payload(session: Session) -> list[dict[str, Any]]:
    exemptions = list_exemptions(session)
    return [
        {
            "id": exemption.id,
            "scope_type": exemption.scope_type,
            "file_id": exemption.file_id,
            "path": exemption.path,
            "artist": exemption.artist,
            "album": exemption.album,
            "title": exemption.title,
            "track_number": exemption.track_number,
            "reason": exemption.reason,
            "active": exemption.active,
            "created_at": exemption.created_at.isoformat(),
        }
        for exemption in exemptions
    ]


def create_exemption_payload(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    exemption = add_exemption(
        session=session,
        scope_type=str(payload["scope_type"]),
        reason=payload.get("reason"),
        file_id=payload.get("file_id"),
        path=payload.get("path"),
        artist=payload.get("artist"),
        album=payload.get("album"),
        title=payload.get("title"),
        track_number=payload.get("track_number"),
    )
    session.commit()
    return {"id": exemption.id, "scope_type": exemption.scope_type}


def deactivate_exemption_payload(session: Session, exemption_id: int) -> dict[str, Any]:
    exemption = deactivate_exemption(session, exemption_id)
    if exemption is None:
        raise KeyError(exemption_id)
    session.commit()
    return {"id": exemption.id, "active": exemption.active}
