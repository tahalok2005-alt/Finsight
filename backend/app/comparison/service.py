"""Compare two processed reports.

Comparison is only meaningful between like-for-like figures, so a metric is
compared only when both sides agree on entity, currency and magnitude.  Where
they do not, the pair is reported as *not comparable* with the reason, rather
than being silently converted or dropped.

Every returned figure is labelled with its basis:

* ``reported``   - the document stated this growth itself
* ``calculated`` - computed here from the two extracted values
* ``reviewed``   - a human corrected the value; the correction is used
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.models import Document, FinancialFactRow, GuidanceRow
from app.extraction.taxonomy import get as get_metric
from app.normalization.numbers import MAGNITUDE_FACTORS

log = get_logger("comparison")

# Headline metrics an analyst expects in a period-over-period view.
DEFAULT_METRICS: tuple[str, ...] = (
    "gross_revenue", "revenue", "revenue_from_operations", "ebitda",
    "ebitda_margin", "ebit", "pbt", "pat", "net_income", "eps",
    "depreciation", "finance_costs", "tax_expense", "capex",
    "cash_and_equivalents", "outstanding_debt", "net_debt",
    "net_debt_to_ebitda", "operating_cash_flow", "free_cash_flow",
    "subscribers", "arpu", "store_count",
)


@dataclass(slots=True)
class MetricComparison:
    canonical_metric: str
    metric_display: str
    entity: str
    left_value: float | None = None
    right_value: float | None = None
    left_period: str | None = None
    right_period: str | None = None
    unit: str | None = None
    currency: str | None = None
    value_type: str | None = None
    change: float | None = None
    change_pct: float | None = None
    change_basis: Literal["calculated", "reported", "not_comparable"] = "calculated"
    left_basis: str = "extracted"
    right_basis: str = "extracted"
    left_confidence: float | None = None
    right_confidence: float | None = None
    left_fact_id: str | None = None
    right_fact_id: str | None = None
    left_page: int | None = None
    right_page: int | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_metric": self.canonical_metric,
            "metric": self.metric_display, "entity": self.entity,
            "left": {"value": self.left_value, "period": self.left_period,
                     "basis": self.left_basis, "confidence": self.left_confidence,
                     "fact_id": self.left_fact_id, "page": self.left_page},
            "right": {"value": self.right_value, "period": self.right_period,
                      "basis": self.right_basis, "confidence": self.right_confidence,
                      "fact_id": self.right_fact_id, "page": self.right_page},
            "unit": self.unit, "currency": self.currency,
            "value_type": self.value_type,
            "change": self.change, "change_pct": self.change_pct,
            "change_basis": self.change_basis, "note": self.note,
        }


def _effective(f: FinancialFactRow) -> tuple[float | None, str]:
    """The value to use, preferring a human correction over the extraction."""
    if f.corrected_value is not None:
        return f.corrected_value, "reviewed"
    return f.value, "extracted"


def _best(facts: list[FinancialFactRow]) -> FinancialFactRow | None:
    if not facts:
        return None
    # a reviewed fact outranks a merely confident one
    reviewed = [f for f in facts if f.review_state in ("approved", "edited")]
    pool = reviewed or facts
    return max(pool, key=lambda f: (f.confidence, f.source_page * -1))


def _load(session: Session, document_id: str, period: str | None,
          ) -> dict[tuple[str, str], list[FinancialFactRow]]:
    stmt = select(FinancialFactRow).where(
        FinancialFactRow.document_id == document_id,
        FinancialFactRow.nature == "actual",
        FinancialFactRow.review_state != "rejected")
    if period:
        stmt = stmt.where(FinancialFactRow.period == period)
    out: dict[tuple[str, str], list[FinancialFactRow]] = {}
    for f in session.scalars(stmt):
        out.setdefault((f.canonical_metric, (f.entity or "Consolidated")), []).append(f)
    return out


def available_periods(session: Session, document_id: str) -> list[str]:
    rows = session.execute(
        select(FinancialFactRow.period, FinancialFactRow.fiscal_year,
               FinancialFactRow.quarter)
        .where(FinancialFactRow.document_id == document_id,
               FinancialFactRow.period.is_not(None))
        .distinct()).all()
    rows.sort(key=lambda r: (r[1] or 0, r[2] or 0), reverse=True)
    return [r[0] for r in rows]


def compare(session: Session, *, left_document_id: str, right_document_id: str,
            left_period: str | None = None, right_period: str | None = None,
            metrics: list[str] | None = None,
            entity: str | None = None) -> dict[str, Any]:
    """Compare two documents (or two periods within one document)."""
    left_doc = session.get(Document, left_document_id)
    right_doc = session.get(Document, right_document_id)
    if left_doc is None or right_doc is None:
        raise ValueError("Both documents must exist")

    left = _load(session, left_document_id, left_period)
    right = _load(session, right_document_id, right_period)

    wanted = metrics or list(DEFAULT_METRICS)
    keys = [k for k in set(left) | set(right) if k[0] in wanted]
    if entity:
        keys = [k for k in keys if k[1] == entity]
    keys.sort(key=lambda k: (wanted.index(k[0]) if k[0] in wanted else 999, k[1]))

    comparisons: list[MetricComparison] = []
    for canonical, ent in keys:
        lf = _best(left.get((canonical, ent), []))
        rf = _best(right.get((canonical, ent), []))
        definition = get_metric(canonical)
        display = definition.display if definition else canonical

        c = MetricComparison(canonical_metric=canonical, metric_display=display,
                             entity=ent)
        if lf is not None:
            c.left_value, c.left_basis = _effective(lf)
            c.left_period, c.left_confidence = lf.period, lf.confidence
            c.left_fact_id, c.left_page = lf.id, lf.source_page
            c.unit, c.currency, c.value_type = lf.unit, lf.currency, lf.value_type
        if rf is not None:
            c.right_value, c.right_basis = _effective(rf)
            c.right_period, c.right_confidence = rf.period, rf.confidence
            c.right_fact_id, c.right_page = rf.id, rf.source_page
            c.unit = c.unit or rf.unit
            c.currency = c.currency or rf.currency
            c.value_type = c.value_type or rf.value_type

        if lf is not None and rf is not None and \
                c.left_value is not None and c.right_value is not None:
            if lf.value_type != rf.value_type:
                c.change_basis = "not_comparable"
                c.note = (f"value types differ ({lf.value_type} vs "
                          f"{rf.value_type})")
            elif (lf.currency != rf.currency
                  and "UNKNOWN" not in (lf.currency, rf.currency)):
                c.change_basis = "not_comparable"
                c.note = f"currencies differ ({lf.currency} vs {rf.currency})"
            elif (lf.period_type and rf.period_type
                  and lf.period_type != rf.period_type):
                # A full year against a single quarter is not a change, it is a
                # different measurement.  Refuse rather than subtract.
                c.change_basis = "not_comparable"
                c.note = (f"period types differ ({lf.period} is a "
                          f"{lf.period_type}, {rf.period} is a "
                          f"{rf.period_type})")
            else:
                left_v, right_v = c.left_value, c.right_value
                if lf.unit != rf.unit:
                    fl = MAGNITUDE_FACTORS.get(lf.unit)
                    fr = MAGNITUDE_FACTORS.get(rf.unit)
                    if fl is None or fr is None:
                        c.change_basis = "not_comparable"
                        c.note = f"units differ ({lf.unit} vs {rf.unit})"
                    else:
                        # express both in the left side's unit
                        right_v = right_v * fr / fl
                        c.note = (f"right side converted from {rf.unit} to "
                                  f"{lf.unit} for comparison")
                if c.change_basis != "not_comparable":
                    # Same measure, same kind of period: say whether the two
                    # sides are the *same* period (a like-for-like check
                    # between documents) or successive ones (a real movement).
                    if lf.period and rf.period and lf.period != rf.period:
                        c.change_basis = "period_over_period"
                        c.note = ((c.note or "") +
                                  f" comparing {lf.period} with {rf.period}").strip()
                    elif lf.period and lf.period == rf.period:
                        c.change_basis = "like_for_like"
                    c.change = round(right_v - left_v, 6)
                    if lf.value_type in ("percentage", "ratio", "bps"):
                        c.change_pct = None
                        c.note = (c.note or "") + (
                            " change shown in percentage points" if
                            lf.value_type == "percentage" else "")
                    elif left_v:
                        c.change_pct = round((right_v - left_v) / abs(left_v) * 100, 4)
                    if rf.growth_reported and rf.growth is not None:
                        c.change_basis = "reported"
                        c.change_pct = rf.growth
        elif lf is None or rf is None:
            c.note = "metric present on only one side"

        comparisons.append(c)

    return {
        "left": {"document_id": left_document_id,
                 "filename": left_doc.original_filename,
                 "company": left_doc.company_name,
                 "period": left_period or left_doc.report_period,
                 "status": left_doc.status},
        "right": {"document_id": right_document_id,
                  "filename": right_doc.original_filename,
                  "company": right_doc.company_name,
                  "period": right_period or right_doc.report_period,
                  "status": right_doc.status},
        "entities": sorted({k[1] for k in set(left) | set(right)}),
        "comparisons": [c.as_dict() for c in comparisons],
        "guidance_changes": _guidance_delta(session, left_document_id,
                                            right_document_id),
    }


def _guidance_delta(session: Session, left_id: str, right_id: str) -> dict:
    """Guidance present on each side, so changes in outlook are visible."""
    def load(doc_id: str) -> list[dict]:
        return [{"metric": g.canonical_metric or g.metric, "text": g.text[:400],
                 "target_value": g.target_value, "target_period": g.target_period,
                 "direction": g.direction, "nature": g.nature, "page": g.page_number}
                for g in session.scalars(select(GuidanceRow).where(
                    GuidanceRow.document_id == doc_id))]

    left, right = load(left_id), load(right_id)
    left_metrics = {g["metric"] for g in left if g["metric"]}
    right_metrics = {g["metric"] for g in right if g["metric"]}
    return {
        "left": left, "right": right,
        "added": sorted(right_metrics - left_metrics),
        "removed": sorted(left_metrics - right_metrics),
        "retained": sorted(left_metrics & right_metrics),
    }
