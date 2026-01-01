from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import BlockCandidate, RoonBlockRaw
from .roon import BridgeClientError, DEFAULT_BRIDGE_HTTP_URL

CandidateStatus = Literal["planned", "restored"]
BLOCK_GRACE_DAYS_DEFAULT = 30


@dataclass(frozen=True)
class BlockedObject:
    """Минимальное представление заблокированного объекта из Roon."""

    object_type: str
    object_id: str
    label: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "BlockedObject":
        """Создаёт объект из произвольного словаря."""

        object_type_raw = str(payload.get("type", "")).strip()
        object_id_raw = str(payload.get("id", "")).strip()
        if not object_type_raw or not object_id_raw:
            msg = "Ответ bridge должен содержать поля type и id"
            raise BridgeClientError(msg)

        label_raw = payload.get("label")
        label = str(label_raw).strip() if label_raw is not None else None

        return cls(
            object_type=object_type_raw.lower(),
            object_id=object_id_raw,
            label=label or None,
        )


@dataclass(frozen=True)
class SyncResult:
    """Результат синхронизации блоков."""

    raw_created: int
    raw_updated: int
    candidates_created: int
    candidates_restored: int


def _reason_for(blocked: BlockedObject) -> str:
    return f"blocked_by_{blocked.object_type}"


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def upsert_raw_block(
    session: Session,
    blocked: BlockedObject,
    seen_at: datetime,
) -> tuple[RoonBlockRaw, bool]:
    """Создаёт или обновляет запись roon_blocks_raw, сохраняя first_seen_at."""

    existing = session.scalar(
        select(RoonBlockRaw).where(
            RoonBlockRaw.object_type == blocked.object_type, RoonBlockRaw.object_id == blocked.object_id
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
    """Создаёт или обновляет кандидата на действие по блоку."""

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
    """Переводит кандидатов в статус restored, если блоки исчезли в Roon."""

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
    """Сохраняет сырые блоки и обновляет таблицу кандидатов.

    Наследование artist→album→track пока не разворачивается, так как нет доступа
    к каталожным данным Roon в текущей интеграции. Обрабатываются только те
    объекты, что вернул bridge.
    """

    timestamp = _normalize_datetime(seen_at or datetime.now(timezone.utc))
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

    restored_candidates = mark_restored_candidates(session=session, active_keys=active_keys, restored_at=timestamp)

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
    """Получает список заблокированных объектов из bridge."""

    bridge_base = base_url or os.environ.get("HSAJ_BRIDGE_HTTP") or DEFAULT_BRIDGE_HTTP_URL
    url = f"{bridge_base.rstrip('/')}/blocked"
    request = Request(url, headers={"Accept": "application/json"})

    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - контролируемый URL
            payload = response.read().decode("utf-8")
            if response.status == 501:
                raise BridgeClientError("Bridge не поддерживает /blocked (501)")
            if response.status != 200:
                raise BridgeClientError(f"Bridge вернул статус {response.status} для /blocked")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise BridgeClientError(f"Не удалось получить /blocked: {exc}") from exc

    import json  # локальный импорт, чтобы не нагружать старты CLI

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - неожиданный ответ
        raise BridgeClientError(f"Некорректный JSON от bridge: {exc}") from exc

    if not isinstance(parsed, list):
        msg = "Ожидался массив объектов в /blocked"
        raise BridgeClientError(msg)

    return [BlockedObject.from_dict(item) for item in parsed]
