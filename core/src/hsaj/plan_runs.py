from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy.orm import Session

from .db.models import PlanRun
from .planner import Plan, plan_from_dict
from .timeutils import utc_now


def create_plan_run(
    session: Session,
    *,
    plan: Plan,
    request_id: str | None = None,
    status: str = "preview",
) -> PlanRun:
    record = PlanRun(
        id=uuid4().hex,
        request_id=request_id,
        status=status,
        plan_json=plan.to_json(),
    )
    session.add(record)
    session.flush()
    return record


def load_plan_run(session: Session, plan_id: str) -> tuple[PlanRun | None, Plan | None]:
    record = session.get(PlanRun, plan_id)
    if record is None:
        return (None, None)
    return (record, plan_from_dict(json.loads(record.plan_json)))


def mark_plan_applied(session: Session, record: PlanRun, *, status: str = "applied") -> None:
    record.status = status
    record.applied_at = utc_now()
