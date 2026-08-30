"""Human review workflow.

The extracted value is treated as evidence and is never overwritten.  A
reviewer's correction is stored alongside it in ``corrected_value``, and every
action is appended to an immutable ``review_actions`` log, so the original
machine reading and the human judgement can both be inspected afterwards.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.errors import ResourceNotFound, ValidationFailure
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.models import (
    ChartRow, CommentaryRow, Document, FinancialFactRow, GuidanceRow,
    ReviewActionRow,
)
from app.models.enums import DocumentStatus, ReviewAction, ReviewState

log = get_logger("review")

TARGETS = {
    "fact": FinancialFactRow,
    "chart": ChartRow,
    "commentary": CommentaryRow,
    "guidance": GuidanceRow,
}


def queue(session: Session, document_id: str, *, limit: int = 100,
          offset: int = 0, include_resolved: bool = False) -> dict[str, Any]:
    """Facts needing attention, worst first."""
    stmt = select(FinancialFactRow).where(
        FinancialFactRow.document_id == document_id,
        FinancialFactRow.requires_review.is_(True))
    if not include_resolved:
        stmt = stmt.where(FinancialFactRow.review_state == ReviewState.PENDING.value)

    total = session.scalar(
        select(func.count()).select_from(stmt.subquery())) or 0

    severity = case(
        (FinancialFactRow.validation_status == "FAILED", 0),
        (FinancialFactRow.validation_status == "WARNING", 1),
        (FinancialFactRow.validation_status == "REQUIRES_REVIEW", 2),
        else_=3)
    rows = list(session.scalars(
        stmt.order_by(severity, FinancialFactRow.confidence)
        .limit(limit).offset(offset)))

    return {"total": total, "items": [_fact_summary(f) for f in rows]}


def _fact_summary(f: FinancialFactRow) -> dict[str, Any]:
    return {
        "fact_id": f.id, "metric": f.metric, "metric_display": f.metric_display,
        "canonical_metric": f.canonical_metric, "entity": f.entity,
        "value": f.value, "corrected_value": f.corrected_value,
        "raw_value": f.raw_value, "unit": f.unit, "currency": f.currency,
        "period": f.period, "confidence": f.confidence,
        "confidence_band": f.confidence_band,
        "confidence_breakdown": f.confidence_breakdown,
        "validation_status": f.validation_status,
        "review_state": f.review_state, "source_page": f.source_page,
        "original_text": f.original_text, "kind": f.kind,
        "table_id": f.table_id, "cell_id": f.cell_id, "region_id": f.region_id,
    }


def apply_action(session: Session, document_id: str, *, target_type: str,
                 target_id: str, action: str, reviewer: str = "analyst",
                 note: str | None = None,
                 new_value: dict | None = None) -> dict[str, Any]:
    """Record a review decision and apply its effect."""
    model = TARGETS.get(target_type)
    if model is None:
        raise ValidationFailure(
            f"Unsupported review target {target_type!r}",
            detail={"allowed": sorted(TARGETS)})
    row = session.get(model, target_id)
    if row is None or row.document_id != document_id:
        raise ResourceNotFound(
            f"No {target_type} {target_id!r} in document {document_id}")

    action_enum = ReviewAction(action)
    previous = _snapshot(row, target_type)

    if action_enum == ReviewAction.APPROVE:
        _set_state(row, ReviewState.APPROVED, target_type)
    elif action_enum == ReviewAction.REJECT:
        _set_state(row, ReviewState.REJECTED, target_type)
    elif action_enum == ReviewAction.REOPEN:
        _set_state(row, ReviewState.PENDING, target_type, requires_review=True)
    elif action_enum == ReviewAction.EDIT:
        if not new_value:
            raise ValidationFailure("An edit action requires new_value")
        _apply_edit(row, target_type, new_value, reviewer)
    elif action_enum == ReviewAction.COMMENT:
        if not note:
            raise ValidationFailure("A comment action requires a note")

    repo.record_review(session, document_id=document_id,
                       target_type=target_type, target_id=target_id,
                       action=action_enum.value, reviewer=reviewer,
                       previous_value=previous,
                       new_value=_snapshot(row, target_type), note=note)
    session.flush()
    _refresh_document_status(session, document_id)
    return _snapshot(row, target_type)


def _set_state(row: Any, state: ReviewState, target_type: str, *,
               requires_review: bool | None = None) -> None:
    if hasattr(row, "review_state"):
        row.review_state = state.value
    if requires_review is not None and hasattr(row, "requires_review"):
        row.requires_review = requires_review
    elif state in (ReviewState.APPROVED, ReviewState.REJECTED,
                   ReviewState.EDITED) and hasattr(row, "requires_review"):
        row.requires_review = False


def _apply_edit(row: Any, target_type: str, new_value: dict,
                reviewer: str) -> None:
    if target_type == "fact":
        if "value" in new_value:
            try:
                row.corrected_value = (None if new_value["value"] is None
                                       else float(new_value["value"]))
            except (TypeError, ValueError) as exc:
                raise ValidationFailure(
                    f"value must be numeric, got {new_value['value']!r}") from exc
        # editable descriptive fields; the extracted value itself is preserved
        for field in ("metric", "canonical_metric", "period", "entity",
                      "unit", "currency"):
            if field in new_value and new_value[field] is not None:
                setattr(row, field, str(new_value[field])[:256])
        row.corrected_by = reviewer
        row.corrected_at = datetime.now(timezone.utc)
        row.review_state = ReviewState.EDITED.value
        row.requires_review = False
    else:
        for field, value in new_value.items():
            if hasattr(row, field) and field not in ("id", "document_id"):
                setattr(row, field, value)
        _set_state(row, ReviewState.EDITED, target_type)


def _snapshot(row: Any, target_type: str) -> dict[str, Any]:
    if target_type == "fact":
        return {"value": row.value, "corrected_value": row.corrected_value,
                "metric": row.metric, "canonical_metric": row.canonical_metric,
                "period": row.period, "entity": row.entity, "unit": row.unit,
                "currency": row.currency, "review_state": row.review_state,
                "requires_review": row.requires_review}
    return {"id": row.id,
            "review_state": getattr(row, "review_state", None),
            "requires_review": getattr(row, "requires_review", None)}


def _refresh_document_status(session: Session, document_id: str) -> None:
    """Clear REQUIRES_REVIEW once the queue is empty; never invent success."""
    doc = session.get(Document, document_id)
    if doc is None:
        return
    if doc.status not in (DocumentStatus.REQUIRES_REVIEW.value,
                          DocumentStatus.COMPLETED.value):
        return
    outstanding = session.scalar(
        select(func.count()).select_from(FinancialFactRow).where(
            FinancialFactRow.document_id == document_id,
            FinancialFactRow.requires_review.is_(True),
            FinancialFactRow.review_state == ReviewState.PENDING.value)) or 0
    new_status = (DocumentStatus.COMPLETED if outstanding == 0
                  else DocumentStatus.REQUIRES_REVIEW)
    if doc.status != new_status.value:
        doc.status = new_status.value
        doc.updated_at = datetime.now(timezone.utc)
        log.info("review.status_updated", document_id=document_id,
                 status=new_status.value, outstanding=outstanding)


def history(session: Session, document_id: str, *, target_id: str | None = None,
            limit: int = 200) -> list[dict[str, Any]]:
    stmt = select(ReviewActionRow).where(
        ReviewActionRow.document_id == document_id)
    if target_id:
        stmt = stmt.where(ReviewActionRow.target_id == target_id)
    rows = session.scalars(
        stmt.order_by(ReviewActionRow.created_at.desc()).limit(limit))
    return [{"id": r.id, "target_type": r.target_type, "target_id": r.target_id,
             "action": r.action, "reviewer": r.reviewer, "note": r.note,
             "previous_value": r.previous_value, "new_value": r.new_value,
             "created_at": r.created_at} for r in rows]
