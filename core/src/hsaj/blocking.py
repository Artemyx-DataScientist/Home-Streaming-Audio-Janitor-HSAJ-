from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from .bridge_auth import build_bridge_headers
from .db.models import BlockCandidate, RoonBlockRaw
from .roon import BridgeClientError, DEFAULT_BRIDGE_HTTP_URL
from .timeutils import ensure_utc, utc_now

CandidateStatus = Literal["planned", "restored"]
BLOCK_GRACE_DAYS_DEFAULT = 30


@dataclass(frozen=True)
class BlockedObject:
    """Minimal blocked object returned by the bridge."""

    object_type: str
    object_id: str
    label: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "BlockedObject":
        """Create a blocked object from bridge JSON payload."""

        object_type_raw = str(payload.get("type", "")).strip()
        object_id_raw = str(payload.get("id", "")).strip()
        if not object_type_raw or not object_id_raw:
            raise BridgeClientError("Bridge response must contain both type and id")

        label_raw = payload.get("label")
        label = str(label_raw).strip() if label_raw is not None else None

        return cls(
            object_type=object_type_raw.lower(),
            object_id=object_id_raw,
            label=label or None,
        )


@dataclass(frozen=True)
class SyncResult:
    """Counters produced by blocked-object sync."""

    raw_created: int
    raw_updated: int
    candidates_created: int
    candidates_restored: int


def _reason_for(blocked: BlockedObject) -> str:
    return f"blocked_by_{blocked.object_type}"


def _normalize_datetime(value: datetime) -> datetime:
    normalized = ensure_utc(value)
    if normalized is None:
        raise ValueError("datetime value is required")
    return normalized


def upsert_raw_block(
    session: Session,
    blocked: BlockedObject,
    seen_at: datetime,
) -> tuple[RoonBlockRaw, bool]:
    """Insert or update roon_blocks_raw while preserving first_seen_at."""

    existing = session.scalar(
        select(RoonBlockRaw).where(
            RoonBlockRaw.object_type == blocked.object_type,
            RoonBlockRaw.object_id == blocked.object_id,
        )
    )
    normalized_seen = _normalize_datetime(seen_at)
    if existing:
        existing.label = blocked.label
        existing.last_seen_at = normalized_seen
        return existing, False

    record = RoonBlockRaw(
        object_type=blocked.object_type,
        object_id=blocked.object_id,
        label=blocked.label,
        first_seen_at=normalized_seen,
        last_seen_at=normalized_seen,
    )
    session.add(record)
    return record, True


def upsert_block_candidate(
    session: Session,
    blocked: BlockedObject,
    seen_at: datetime,
    grace_period_days: int = BLOCK_GRACE_DAYS_DEFAULT,
) -> tuple[BlockCandidate, bool]:
    """Insert or update a block candidate."""

    existing = session.scalar(
        select(BlockCandidate).where(
            BlockCandidate.object_type == blocked.object_type,
            BlockCandidate.object_id == blocked.object_id,
        )
    )
    normalized_seen = _normalize_datetime(seen_at)
    if existing:
        existing.label = blocked.label
        existing.reason = _reason_for(blocked)
        existing.last_seen_at = normalized_seen
        if existing.status == "restored":
            existing.status = "planned"
            existing.restored_at = None
        return existing, False

    planned_action_at = normalized_seen + timedelta(days=grace_period_days)
    candidate = BlockCandidate(
        object_type=blocked.object_type,
        object_id=blocked.object_id,
        label=blocked.label,
        reason=_reason_for(blocked),
        status="planned",
        first_seen_at=normalized_seen,
        last_seen_at=normalized_seen,
        planned_action_at=planned_action_at,
    )
    session.add(candidate)
    return candidate, True


def mark_restored_candidates(
    session: Session,
    active_keys: set[tuple[str, str]],
    restored_at: datetime,
) -> list[BlockCandidate]:
    """Mark candidates as restored when the corresponding block disappears."""

    normalized_restored = _normalize_datetime(restored_at)
    restored: list[BlockCandidate] = []
    all_candidates = session.scalars(select(BlockCandidate)).all()
    for candidate in all_candidates:
        key = (candidate.object_type, candidate.object_id)
        if key in active_keys:
            continue
        if candidate.status == "restored":
            continue
        candidate.status = "restored"
        candidate.restored_at = normalized_restored
        candidate.planned_action_at = None
        candidate.last_seen_at = normalized_restored
        restored.append(candidate)
    return restored


def sync_blocked_objects(
    session: Session,
    blocked_items: Sequence[BlockedObject],
    grace_period_days: int = BLOCK_GRACE_DAYS_DEFAULT,
    seen_at: datetime | None = None,
) -> SyncResult:
    """Persist raw blocks and refresh the candidate table."""

    timestamp = _normalize_datetime(seen_at or utc_now())
    active_keys: set[tuple[str, str]] = set()
    raw_created = 0
    raw_updated = 0
    candidates_created = 0

    for item in blocked_items:
        active_keys.add((item.object_type, item.object_id))
        _, created_raw = upsert_raw_block(
            session=session, blocked=item, seen_at=timestamp
        )
        if created_raw:
            raw_created += 1
        else:
            raw_updated += 1

        _, created_candidate = upsert_block_candidate(
            session=session,
            blocked=item,
            seen_at=timestamp,
            grace_period_days=grace_period_days,
        )
        if created_candidate:
            candidates_created += 1

    restored_candidates = mark_restored_candidates(
        session=session, active_keys=active_keys, restored_at=timestamp
    )

    return SyncResult(
        raw_created=raw_created,
        raw_updated=raw_updated,
        candidates_created=candidates_created,
        candidates_restored=len(restored_candidates),
    )


def fetch_blocked_from_bridge(
    base_url: str | None = None,
    timeout: float = 5.0,
) -> list[BlockedObject]:
    """Fetch blocked objects from the bridge."""

    bridge_base = (
        base_url or os.environ.get("HSAJ_BRIDGE_HTTP") or DEFAULT_BRIDGE_HTTP_URL
    )
    url = f"{bridge_base.rstrip('/')}/blocked"
    request = Request(url, headers=build_bridge_headers(accept="application/json"))

    try:
        with urlopen(
            request, timeout=timeout
        ) as response:  # noqa: S310 - controlled URL source
            payload = response.read().decode("utf-8")
            if response.status == 501:
                raise BridgeClientError("Bridge does not implement /blocked (501)")
            if response.status != 200:
                raise BridgeClientError(
                    f"Bridge returned status {response.status} for /blocked"
                )
    except (HTTPError, URLError, TimeoutError) as exc:
        raise BridgeClientError(f"Could not fetch /blocked: {exc}") from exc

    import json

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - unexpected response
        raise BridgeClientError(f"Invalid bridge JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise BridgeClientError("Expected a JSON array from /blocked")

    return [BlockedObject.from_dict(item) for item in parsed]
