from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import ReviewDecision


def add_review_decision(
    session: Session,
    *,
    review_type: str,
    action: str,
    file_id: int | None = None,
    path: str | None = None,
    candidate_reason: str | None = None,
    notes: str | None = None,
) -> ReviewDecision:
    decision = ReviewDecision(
        review_type=review_type,
        file_id=file_id,
        path=path,
        candidate_reason=candidate_reason,
        action=action,
        notes=notes,
    )
    session.add(decision)
    session.flush()
    return decision


def list_review_decisions(session: Session) -> list[ReviewDecision]:
    return session.scalars(select(ReviewDecision).order_by(ReviewDecision.id.desc())).all()


def latest_soft_candidate_actions(session: Session) -> dict[tuple[int, str], ReviewDecision]:
    decisions = session.scalars(
        select(ReviewDecision)
        .where(ReviewDecision.review_type == "soft_candidate")
        .order_by(ReviewDecision.id.desc())
    ).all()

    latest: dict[tuple[int, str], ReviewDecision] = {}
    for decision in decisions:
        if decision.file_id is None or decision.candidate_reason is None:
            continue
        key = (decision.file_id, decision.candidate_reason)
        latest.setdefault(key, decision)
    return latest
