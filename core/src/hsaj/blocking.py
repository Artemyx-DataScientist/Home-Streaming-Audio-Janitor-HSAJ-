from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from .bridge_auth import build_bridge_headers
from .db.models import BlockCandidate, BridgeSyncStatus, RoonBlockRaw
from .roon import DEFAULT_BRIDGE_HTTP_URL, BridgeClientError
from .timeutils import ensure_utc, utc_now

CandidateStatus = Literal["planned", "restored"]
BLOCK_GRACE_DAYS_DEFAULT = 30


@dataclass(frozen=True)
class BlockedObject:
    """Minimal blocked object returned by the bridge."""

    object_type: str
    object_id: str
    label: str | None = None
    artist: str | None = None
    album: str | None = None
    title: str | None = None
    track_number: int | None = None
    duration_ms: int | None = None
    source: str = "bridge.blocked.v1"

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
            artist=_normalize_string(payload.get("artist")),
            album=_normalize_string(payload.get("album")),
            title=_normalize_string(payload.get("title")),
            track_number=_parse_optional_int(payload.get("trackno") or payload.get("track_number")),
            duration_ms=_parse_optional_int(payload.get("duration_ms")),
        )

    def metadata_json(self) -> str | None:
        payload = {
            "artist": self.artist,
            "album": self.album,
            "title": self.title,
            "track_number": self.track_number,
            "duration_ms": self.duration_ms,
        }
        if not any(value is not None for value in payload.values()):
            return None
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class SyncResult:
    """Counters produced by blocked-object sync."""

    raw_created: int
    raw_updated: int
    candidates_created: int
    candidates_restored: int


@dataclass(frozen=True)
class BlockedSnapshot:
    """Blocked snapshot envelope returned by the bridge contract."""

    items: list[BlockedObject]
    contract_version: str | None
    generated_at: datetime | None
    source_mode: str | None
    item_count: int


def _reason_for(blocked: BlockedObject) -> str:
    return f"blocked_by_{blocked.object_type}"


def _explanation_for(blocked: BlockedObject) -> str:
    return json.dumps(
        {
            "source": blocked.source,
            "object_type": blocked.object_type,
            "object_id": blocked.object_id,
            "label": blocked.label,
            "artist": blocked.artist,
            "album": blocked.album,
            "title": blocked.title,
            "track_number": blocked.track_number,
            "duration_ms": blocked.duration_ms,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


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
        existing.metadata_json = blocked.metadata_json()
        existing.last_seen_at = normalized_seen
        return existing, False

    record = RoonBlockRaw(
        object_type=blocked.object_type,
        object_id=blocked.object_id,
        label=blocked.label,
        metadata_json=blocked.metadata_json(),
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
        existing.metadata_json = blocked.metadata_json()
        existing.reason = _reason_for(blocked)
        existing.source = blocked.source
        existing.rule_id = _reason_for(blocked)
        existing.explanation_json = _explanation_for(blocked)
        existing.last_seen_at = normalized_seen
        if existing.status == "restored":
            existing.status = "planned"
            existing.restored_at = None
        existing.last_transition_at = normalized_seen
        return existing, False

    planned_action_at = normalized_seen + timedelta(days=grace_period_days)
    candidate = BlockCandidate(
        object_type=blocked.object_type,
        object_id=blocked.object_id,
        label=blocked.label,
        metadata_json=blocked.metadata_json(),
        reason=_reason_for(blocked),
        status="planned",
        source=blocked.source,
        rule_id=_reason_for(blocked),
        explanation_json=_explanation_for(blocked),
        first_seen_at=normalized_seen,
        last_seen_at=normalized_seen,
        planned_action_at=planned_action_at,
        last_transition_at=normalized_seen,
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
        candidate.delete_after = None
        candidate.last_seen_at = normalized_restored
        candidate.last_transition_at = normalized_restored
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
        _, created_raw = upsert_raw_block(session=session, blocked=item, seen_at=timestamp)
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


def fetch_blocked_snapshot_from_bridge(
    base_url: str | None = None,
    timeout: float = 5.0,
) -> BlockedSnapshot:
    """Fetch blocked objects from the bridge."""

    bridge_base = base_url or os.environ.get("HSAJ_BRIDGE_HTTP") or DEFAULT_BRIDGE_HTTP_URL
    url = f"{bridge_base.rstrip('/')}/blocked"
    request = Request(url, headers=build_bridge_headers(accept="application/json"))

    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - controlled URL source
            payload = response.read().decode("utf-8")
            if response.status == 501:
                raise BridgeClientError("Bridge does not implement /blocked (501)")
            if response.status != 200:
                raise BridgeClientError(f"Bridge returned status {response.status} for /blocked")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise BridgeClientError(f"Could not fetch /blocked: {exc}") from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - unexpected response
        raise BridgeClientError(f"Invalid bridge JSON: {exc}") from exc

    if isinstance(parsed, list):
        items = [BlockedObject.from_dict(item) for item in parsed]
        return BlockedSnapshot(
            items=items,
            contract_version=None,
            generated_at=None,
            source_mode=None,
            item_count=len(items),
        )

    if not isinstance(parsed, dict):
        raise BridgeClientError("Expected a JSON array or snapshot object from /blocked")

    items_payload = parsed.get("items")
    if not isinstance(items_payload, list):
        raise BridgeClientError("Blocked snapshot must contain an items array")

    generated_at_raw = parsed.get("generated_at")
    generated_at = None
    if generated_at_raw:
        try:
            generated_at = ensure_utc(datetime.fromisoformat(str(generated_at_raw)))
        except ValueError as exc:
            raise BridgeClientError(
                f"Invalid blocked snapshot generated_at: {generated_at_raw}"
            ) from exc

    source_payload = parsed.get("source")
    source_mode = None
    if isinstance(source_payload, dict):
        mode = source_payload.get("mode")
        source_mode = _normalize_string(mode)

    items = [BlockedObject.from_dict(item) for item in items_payload]
    item_count_raw = parsed.get("item_count")
    item_count = len(items)
    if item_count_raw is not None:
        item_count = _parse_optional_int(item_count_raw) or 0

    return BlockedSnapshot(
        items=items,
        contract_version=_normalize_string(parsed.get("contract_version")),
        generated_at=generated_at,
        source_mode=source_mode,
        item_count=item_count,
    )


def fetch_blocked_from_bridge(
    base_url: str | None = None,
    timeout: float = 5.0,
) -> list[BlockedObject]:
    """Compatibility helper that returns only blocked items."""

    return fetch_blocked_snapshot_from_bridge(base_url=base_url, timeout=timeout).items


def record_blocked_sync_success(
    session: Session,
    *,
    snapshot: BlockedSnapshot,
    attempted_at: datetime | None = None,
) -> BridgeSyncStatus:
    return _upsert_bridge_sync_status(
        session=session,
        status="ok",
        attempted_at=attempted_at or utc_now(),
        snapshot=snapshot,
        error=None,
    )


def record_blocked_sync_failure(
    session: Session,
    *,
    error: str,
    attempted_at: datetime | None = None,
) -> BridgeSyncStatus:
    return _upsert_bridge_sync_status(
        session=session,
        status="error",
        attempted_at=attempted_at or utc_now(),
        snapshot=None,
        error=error,
    )


def _upsert_bridge_sync_status(
    session: Session,
    *,
    status: str,
    attempted_at: datetime,
    snapshot: BlockedSnapshot | None,
    error: str | None,
) -> BridgeSyncStatus:
    normalized_attempt = _normalize_datetime(attempted_at)
    record = session.get(BridgeSyncStatus, "blocked")
    if record is None:
        record = BridgeSyncStatus(sync_name="blocked", status=status)
        session.add(record)

    record.status = status
    record.last_attempt_at = normalized_attempt
    record.last_error = error
    if snapshot is not None:
        record.contract_version = snapshot.contract_version
        record.source_mode = snapshot.source_mode
        record.item_count = snapshot.item_count
        record.snapshot_generated_at = snapshot.generated_at
        record.last_success_at = normalized_attempt
    return record


def _normalize_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BridgeClientError(f"Could not parse integer value: {value}") from exc
