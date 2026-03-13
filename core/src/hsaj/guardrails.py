from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import HsajConfig
from .db.models import BridgeSyncStatus
from .timeutils import utc_now


class SafetyError(RuntimeError):
    """Raised when a destructive operation is blocked by runtime guardrails."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def readiness_checks(
    session: Session | None,
    config: HsajConfig,
    *,
    boot_error: str | None = None,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if boot_error:
        checks.append(CheckResult("database", False, boot_error))
        checks.append(CheckResult("blocked_sync", False, "Database bootstrap is unavailable"))
        return checks

    checks.append(_database_check(session))
    checks.append(_library_roots_check(config))
    checks.append(_quarantine_check(config))
    checks.append(_ffprobe_check(config))
    checks.append(_hard_delete_config_check(config))
    checks.extend(_blocked_sync_checks(session, config))
    return checks


def destructive_guardrails(
    session: Session | None,
    config: HsajConfig,
    *,
    boot_error: str | None = None,
    action_name: str = "destructive action",
) -> dict[str, Any]:
    reasons: list[str] = []
    checks = readiness_checks(session, config, boot_error=boot_error)
    failing_checks = {
        check.name: check.detail
        for check in checks
        if check.name in {
            "database",
            "blocked_sync",
            "blocked_contract",
            "blocked_source_mode",
            "blocked_sync_fresh",
            "hard_delete_config",
        }
        and not check.ok
    }
    for name in (
        "database",
        "blocked_sync",
        "blocked_contract",
        "blocked_source_mode",
        "blocked_sync_fresh",
        "hard_delete_config",
    ):
        detail = failing_checks.get(name)
        if detail:
            reasons.append(detail)

    if action_name == "hard_delete" and not _hard_delete_allowed(config):
        reasons.append(
            "Hard delete is disabled: set policy.auto_delete=true and policy.allow_hard_delete=true"
        )

    return {
        "allowed": not reasons,
        "reasons": reasons,
        "required_source_mode": config.bridge.required_source_mode,
        "expected_contract_version": config.bridge.contract_version,
        "hard_delete_enabled": _hard_delete_allowed(config),
    }


def assert_destructive_actions_allowed(
    session: Session | None,
    config: HsajConfig,
    *,
    boot_error: str | None = None,
    action_name: str = "destructive action",
) -> None:
    payload = destructive_guardrails(
        session,
        config,
        boot_error=boot_error,
        action_name=action_name,
    )
    if payload["allowed"]:
        return
    reasons = "; ".join(str(reason) for reason in payload["reasons"])
    raise SafetyError(f"{action_name} blocked by guardrails: {reasons}")


def _database_check(session: Session | None) -> CheckResult:
    if session is None:
        return CheckResult("database", False, "Database session is unavailable")
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - defensive DB boundary
        return CheckResult("database", False, f"Database check failed: {exc}")
    return CheckResult("database", True, "Database is initialized")


def _library_roots_check(config: HsajConfig) -> CheckResult:
    roots_ok = bool(config.paths.library_roots) and all(
        root.exists() and root.is_dir() for root in config.paths.library_roots
    )
    return CheckResult(
        "library_roots",
        roots_ok,
        "All configured library roots exist"
        if roots_ok
        else "One or more library roots are missing",
    )


def _quarantine_check(config: HsajConfig) -> CheckResult:
    if config.paths.quarantine_dir is None:
        return CheckResult("quarantine_dir", False, "paths.quarantine_dir is not configured")
    return CheckResult("quarantine_dir", True, "Quarantine directory is configured")


def _ffprobe_check(config: HsajConfig) -> CheckResult:
    ffprobe_path = config.ffprobe_resolved_path()
    ok = ffprobe_path is not None and ffprobe_path.exists()
    return CheckResult(
        "ffprobe",
        ok,
        f"ffprobe resolved to {ffprobe_path}"
        if ok
        else f"Could not resolve ffprobe from {config.paths.ffprobe_path}",
    )


def _hard_delete_config_check(config: HsajConfig) -> CheckResult:
    if config.policy.auto_delete and not config.policy.allow_hard_delete:
        return CheckResult(
            "hard_delete_config",
            False,
            "policy.auto_delete=true requires policy.allow_hard_delete=true",
        )
    return CheckResult(
        "hard_delete_config",
        True,
        "Hard delete configuration is consistent",
    )


def _blocked_sync_checks(session: Session | None, config: HsajConfig) -> list[CheckResult]:
    if session is None:
        return [CheckResult("blocked_sync", False, "Database session is unavailable")]

    blocked_sync = session.get(BridgeSyncStatus, "blocked")
    if blocked_sync is None:
        if config.runtime.enable_background_jobs or config.bridge.required_source_mode:
            return [
                CheckResult(
                    "blocked_sync",
                    False,
                    "Blocked sync has never completed successfully",
                )
            ]
        return [CheckResult("blocked_sync", True, "Manual blocked sync mode")]

    checks: list[CheckResult] = []
    if blocked_sync.status != "ok":
        checks.append(
            CheckResult(
                "blocked_sync",
                False,
                "Last blocked sync status is "
                f"{blocked_sync.status}: {blocked_sync.last_error or 'no detail'}",
            )
        )
        return checks

    checks.append(CheckResult("blocked_sync", True, "Blocked sync is healthy"))

    expected_contract = (config.bridge.contract_version or "").strip() or None
    actual_contract = (blocked_sync.contract_version or "").strip() or None
    contract_ok = expected_contract is None or actual_contract == expected_contract
    checks.append(
        CheckResult(
            "blocked_contract",
            contract_ok,
            f"Blocked contract is {actual_contract or 'legacy'}"
            if contract_ok
            else "Expected blocked contract "
            f"{expected_contract}, got {actual_contract or 'legacy'}",
        )
    )

    required_source_mode = (config.bridge.required_source_mode or "").strip() or None
    actual_source_mode = (blocked_sync.source_mode or "").strip() or None
    source_mode_ok = required_source_mode is None or actual_source_mode == required_source_mode
    checks.append(
        CheckResult(
            "blocked_source_mode",
            source_mode_ok,
            f"Blocked source mode is {actual_source_mode or 'unknown'}"
            if source_mode_ok
            else "Expected blocked source mode "
            f"{required_source_mode}, got {actual_source_mode or 'unknown'}",
        )
    )

    max_age = _blocked_sync_max_age(config)
    if max_age is None:
        return checks
    if blocked_sync.last_success_at is None:
        checks.append(
            CheckResult(
                "blocked_sync_fresh",
                False,
                "Blocked sync has no success timestamp",
            )
        )
        return checks
    age = utc_now() - blocked_sync.last_success_at
    checks.append(
        CheckResult(
            "blocked_sync_fresh",
            age <= max_age,
            f"Blocked sync age is {int(age.total_seconds())}s"
            if age <= max_age
            else f"Blocked sync is stale by {int(age.total_seconds())}s",
        )
    )
    return checks


def _blocked_sync_max_age(config: HsajConfig) -> timedelta | None:
    if config.bridge.max_blocked_sync_age_minutes is not None:
        return timedelta(minutes=config.bridge.max_blocked_sync_age_minutes)
    if config.runtime.enable_background_jobs:
        return timedelta(minutes=max(config.runtime.blocked_sync_interval_minutes * 2, 5))
    if config.bridge.required_source_mode:
        return timedelta(minutes=5)
    return None


def _hard_delete_allowed(config: HsajConfig) -> bool:
    return bool(config.policy.auto_delete and config.policy.allow_hard_delete)
