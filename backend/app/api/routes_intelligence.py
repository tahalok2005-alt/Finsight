"""Financial-intelligence endpoints.

Everything served here is read back from what the pipeline stored: entities and
their hierarchy, table semantics, derived metrics with their formulas, the fact
relationship graph, risk statements, insights, anomalies, a per-statement
financial summary, and the full deterministic explanation of any single fact.

Nothing on these routes computes a new financial conclusion at request time
beyond arithmetic over stored facts, and nothing calls an external service.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DbSession, get_document_or_404
from app.core.errors import ResourceNotFound
from app.core.logging import get_logger
from app.database.models import (
    FinancialFactRow, ProvenanceRow, ValidationResultRow,
)
from app.database.models_intelligence import (
    AnomalyRow, DerivedMetricRow, EntityRow, FactRelationRow, InsightRow,
    RiskRow, TableSemanticsRow,
)
from app.extraction import taxonomy
from app.models.enums import StatementType
from app.review import service as review_service

router = APIRouter(prefix="/documents", tags=["intelligence"])
log = get_logger("api.intelligence")


# --------------------------------------------------------------- serialisers
def _entity_dto(e: EntityRow, fact_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "entity_id": e.id, "entity_name": e.name, "entity_type": e.entity_type,
        "parent_entity_id": e.parent_entity_id, "aliases": e.aliases or [],
        "confidence": e.confidence, "source": e.source,
        "first_page": e.first_page, "evidence": e.evidence,
        "fact_count": fact_counts.get(e.id, 0),
    }


def _derived_dto(m: DerivedMetricRow) -> dict[str, Any]:
    return {
        "id": m.id, "canonical_metric": m.canonical_metric,
        "metric_display": m.metric_display, "entity_id": m.entity_id,
        "entity_name": m.entity_name, "period": m.period, "value": m.value,
        "value_type": m.value_type, "unit": m.unit, "currency": m.currency,
        "formula": m.formula, "inputs": m.inputs or {},
        "input_values": m.input_values or {},
        "reported_fact_id": m.reported_fact_id,
        "reported_value": m.reported_value,
        "agrees_with_reported": m.agrees_with_reported,
        "difference": m.difference, "confidence": m.confidence,
        "derivation": m.derivation, "source_pages": m.source_pages or [],
        "notes": m.notes,
    }


def _insight_dto(i: InsightRow) -> dict[str, Any]:
    return {
        "id": i.id, "insight_type": i.insight_type, "headline": i.headline,
        "detail": i.detail, "entity_id": i.entity_id,
        "entity_name": i.entity_name, "metric": i.metric,
        "periods": i.periods or [],
        "supporting_fact_ids": i.supporting_fact_ids or [],
        "value": i.value, "comparison_value": i.comparison_value,
        "change": i.change, "change_unit": i.change_unit,
        "derivation": i.derivation, "confidence": i.confidence,
        "source_pages": i.source_pages or [],
    }


def _anomaly_dto(a: AnomalyRow) -> dict[str, Any]:
    return {
        "id": a.id, "anomaly_type": a.anomaly_type, "severity": a.severity,
        "message": a.message, "reason": a.reason, "entity_id": a.entity_id,
        "entity_name": a.entity_name, "metric": a.metric, "period": a.period,
        "observed_value": a.observed_value, "expected_value": a.expected_value,
        "deviation": a.deviation, "threshold": a.threshold,
        "fact_ids": a.fact_ids or [], "source_pages": a.source_pages or [],
        "requires_review": a.requires_review,
    }


def _risk_dto(r: RiskRow) -> dict[str, Any]:
    return {
        "id": r.id, "page_number": r.page_number, "region_id": r.region_id,
        "text": r.text, "category": r.category, "categories": r.categories or [],
        "entity_id": r.entity_id, "entity_name": r.entity_name,
        "period": r.period, "trigger_phrase": r.trigger_phrase,
        "severity": r.severity, "is_forward_looking": r.is_forward_looking,
        "confidence": r.confidence,
        "bbox": ({"x": r.x, "y": r.y, "width": r.width, "height": r.height}
                 if r.x is not None else None),
    }


def _semantics_dto(t: TableSemanticsRow) -> dict[str, Any]:
    return {
        "id": t.id, "table_id": t.table_id, "page_number": t.page_number,
        "table_type": t.table_type, "statement": t.statement, "title": t.title,
        "entity_id": t.entity_id, "entity_name": t.entity_name,
        "periods": t.periods or [], "current_period": t.current_period,
        "comparison_period": t.comparison_period,
        "sequential_period": t.sequential_period,
        "unit": t.unit, "currency": t.currency, "columns": t.columns or [],
        "row_metrics": t.row_metrics or {}, "confidence": t.confidence,
        "notes": t.notes or [],
    }


# ------------------------------------------------------------------ entities
@router.get("/{document_id}/entities")
def get_entities(document_id: str, session: DbSession) -> dict:
    """Reporting units the document named, flat and as a hierarchy."""
    get_document_or_404(document_id, session)
    rows = list(session.scalars(select(EntityRow)
                                .where(EntityRow.document_id == document_id)))
    counts: dict[str, int] = {}
    for entity_id, in session.execute(
            select(FinancialFactRow.entity_id)
            .where(FinancialFactRow.document_id == document_id)).all():
        if entity_id:
            counts[entity_id] = counts.get(entity_id, 0) + 1

    items = [_entity_dto(e, counts) for e in rows]
    by_id = {i["entity_id"]: {**i, "children": []} for i in items}
    roots: list[dict] = []
    for item in by_id.values():
        parent = by_id.get(item["parent_entity_id"] or "")
        (parent["children"] if parent else roots).append(item)
    return {"items": items, "hierarchy": roots, "total": len(items)}


# ----------------------------------------------------------- table semantics
@router.get("/{document_id}/table-semantics")
def get_table_semantics(document_id: str, session: DbSession,
                        statement: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(TableSemanticsRow).where(
        TableSemanticsRow.document_id == document_id)
    if statement:
        stmt = stmt.where(TableSemanticsRow.statement == statement)
    rows = list(session.scalars(stmt.order_by(TableSemanticsRow.page_number)))
    return {"items": [_semantics_dto(t) for t in rows], "total": len(rows)}


# ------------------------------------------------------------------- derived
@router.get("/{document_id}/derived-metrics")
def get_derived_metrics(document_id: str, session: DbSession,
                        period: str | None = Query(None),
                        entity_id: str | None = Query(None)) -> dict:
    """Ratios FinSight computed, each with its formula and inputs.

    These are never mixed into ``/financials``: a calculated number is not a
    reported one.
    """
    get_document_or_404(document_id, session)
    stmt = select(DerivedMetricRow).where(
        DerivedMetricRow.document_id == document_id)
    if period:
        stmt = stmt.where(DerivedMetricRow.period == period)
    if entity_id:
        stmt = stmt.where(DerivedMetricRow.entity_id == entity_id)
    rows = list(session.scalars(stmt))
    disagreements = [r for r in rows if r.agrees_with_reported is False]
    return {
        "items": [_derived_dto(m) for m in rows], "total": len(rows),
        "disagreements": len(disagreements),
        "note": ("Every figure here was calculated by FinSight from figures the "
                 "document reported. None of them is presented as a number "
                 "management published."),
    }


# --------------------------------------------------------------- relationships
@router.get("/{document_id}/relations")
def get_relations(document_id: str, session: DbSession,
                  fact_id: str | None = Query(None),
                  kind: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(FactRelationRow).where(
        FactRelationRow.document_id == document_id)
    if kind:
        stmt = stmt.where(FactRelationRow.kind == kind)
    rows = list(session.scalars(stmt))
    if fact_id:
        rows = [r for r in rows
                if fact_id in (r.from_fact_id, r.to_fact_id)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    return {
        "items": [{"id": r.id, "from_fact_id": r.from_fact_id,
                   "to_fact_id": r.to_fact_id, "kind": r.kind,
                   "detail": r.detail or {}, "confidence": r.confidence}
                  for r in rows],
        "total": len(rows), "by_kind": counts,
    }


# --------------------------------------------------------------------- risks
@router.get("/{document_id}/risks")
def get_risks(document_id: str, session: DbSession,
              category: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(RiskRow).where(RiskRow.document_id == document_id)
    if category:
        stmt = stmt.where(RiskRow.category == category)
    rows = list(session.scalars(stmt.order_by(RiskRow.page_number)))
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for r in rows:
        by_category[r.category] = by_category.get(r.category, 0) + 1
        by_severity[r.severity] = by_severity.get(r.severity, 0) + 1
    return {
        "items": [_risk_dto(r) for r in rows], "total": len(rows),
        "by_category": by_category, "by_severity": by_severity,
        "forward_looking": sum(1 for r in rows if r.is_forward_looking),
        "note": ("Cautionary statements quoted verbatim from the document. "
                 "FinSight does not compute an overall risk score, because the "
                 "document does not state one."),
    }


# ------------------------------------------------------------------ insights
@router.get("/{document_id}/insights")
def get_insights(document_id: str, session: DbSession,
                 insight_type: str | None = Query(None),
                 limit: int = Query(50, ge=1, le=200)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(InsightRow).where(InsightRow.document_id == document_id)
    if insight_type:
        stmt = stmt.where(InsightRow.insight_type == insight_type)
    rows = list(session.scalars(stmt))[:limit]
    return {
        "items": [_insight_dto(i) for i in rows], "total": len(rows),
        "note": ("Each insight is arithmetic over facts this document "
                 "published, and lists the fact ids it used. No language model "
                 "produced any of this text."),
    }


# ----------------------------------------------------------------- anomalies
@router.get("/{document_id}/anomalies")
def get_anomalies(document_id: str, session: DbSession,
                  severity: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(AnomalyRow).where(AnomalyRow.document_id == document_id)
    if severity:
        stmt = stmt.where(AnomalyRow.severity == severity)
    rows = list(session.scalars(stmt))
    by_severity: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for a in rows:
        by_severity[a.severity] = by_severity.get(a.severity, 0) + 1
        by_type[a.anomaly_type] = by_type.get(a.anomaly_type, 0) + 1
    return {
        "items": [_anomaly_dto(a) for a in rows], "total": len(rows),
        "by_severity": by_severity, "by_type": by_type,
    }


# --------------------------------------------------------- financial summary
#: A comparative statement laid out as a grid is only worth showing when most
#: of the grid has something in it.  Below these, the same figures read better
#: as a list.
_MIN_MATRIX_ROWS = 3
_MIN_MATRIX_FILL = 0.4


def _comparative_verdict(categories: list[dict]) -> dict:
    """Whether these figures can honestly be laid out as a statement.

    A statement grid makes a promise: that the rows are comparable line items
    and the columns are comparable periods, so reading across means something.
    When most cells are empty the grid breaks that promise -- it looks like a
    statement with the numbers missing, and a reader cannot tell whether the
    figures were never extracted, landed in periods that are not shown, or
    simply do not exist in the source.

    On a results presentation whose tables are laid out by business segment,
    the grid came out 13 rows by 2 periods with 3 cells filled. That is not a
    statement; it is a list of three numbers drawn as a spreadsheet. Saying so
    and showing the figures plainly is both more useful and more honest.
    """
    values = [v for c in categories for m in c["metrics"] for v in m["values"]]
    rows = {(m["entity_id"], m["canonical_metric"])
            for c in categories for m in c["metrics"]}
    periods = sorted({v["period"] for v in values if v["period"]})
    dated = sum(1 for v in values if v["period"])

    capacity = len(rows) * len(periods)
    fill = (dated / capacity) if capacity else 0.0
    ok = (len(periods) >= 1 and len(rows) >= _MIN_MATRIX_ROWS
          and fill >= _MIN_MATRIX_FILL)

    reason = None
    if not ok:
        if not periods:
            reason = ("None of these figures could be attributed to a "
                      "reporting period, so there are no columns to compare.")
        elif len(rows) < _MIN_MATRIX_ROWS:
            reason = ("Too few line items were recognised on this statement to "
                      "lay out a comparative view.")
        else:
            reason = (f"Only {dated} of {capacity} positions in the grid could "
                      f"be filled, because most figures could not be "
                      f"attributed to one of the periods shown.")
    return {
        "renderable": ok,
        "periods": periods,
        "row_count": len(rows),
        "dated_values": dated,
        "total_values": len(values),
        "fill": round(fill, 3),
        "reason": reason,
    }


@router.get("/{document_id}/financial-summary")
def get_financial_summary(document_id: str, session: DbSession,
                          entity_id: str | None = Query(None)) -> dict:
    """Facts organised by statement, then category, then metric.

    Figures from different entities are never combined into one line: each
    metric row is scoped to a single entity.
    """
    get_document_or_404(document_id, session)
    stmt = select(FinancialFactRow).where(
        FinancialFactRow.document_id == document_id)
    if entity_id:
        stmt = stmt.where(FinancialFactRow.entity_id == entity_id)
    facts = list(session.scalars(stmt))

    statements: dict[str, dict] = {}
    for f in facts:
        statement = f.statement or str(StatementType.UNKNOWN)
        category = f.category or "other"
        bucket = statements.setdefault(statement, {"statement": statement,
                                                   "categories": {}})
        cat = bucket["categories"].setdefault(
            category, {"category": category, "metrics": {}})
        key = f"{f.entity_id or f.entity or ''}|{f.canonical_metric}"
        metric = cat["metrics"].setdefault(key, {
            "canonical_metric": f.canonical_metric,
            "metric_display": f.metric_display or f.metric,
            "entity_id": f.entity_id, "entity_name": f.entity,
            "entity_type": f.entity_type, "values": [],
        })
        metric["values"].append({
            "fact_id": f.id, "period": f.period, "value": f.value,
            # The cross-tab keys its columns by the period label, but a label
            # is not enough to head a column with: two "H1" columns can end on
            # different days.  The resolved fields travel so the head can say
            # which one it is, in the same words the figures table uses.
            "period_type": f.period_type, "period_start": f.period_start,
            "period_end": f.period_end,
            "period_confidence": f.period_confidence,
            "period_basis": f.period_basis,
            "effective_value": (f.corrected_value if f.corrected_value is not None
                                else f.value),
            "unit": f.unit, "currency": f.currency,
            "value_type": f.value_type, "growth": f.growth,
            "growth_reported": f.growth_reported,
            "growth_calculated": f.growth_calculated,
            "change_bps": f.change_bps,
            "confidence": f.confidence,
            "validation_status": f.validation_status,
            "requires_review": f.requires_review,
            "derivation": f.derivation, "source_page": f.source_page,
        })

    out = []
    for statement, bucket in statements.items():
        categories = []
        for category in bucket["categories"].values():
            metrics = list(category["metrics"].values())
            for m in metrics:
                m["values"].sort(key=lambda v: (v["period"] or ""), reverse=True)
            metrics.sort(key=lambda m: m["metric_display"])
            categories.append({"category": category["category"],
                               "metrics": metrics,
                               "metric_count": len(metrics)})
        categories.sort(key=lambda c: c["category"])
        entry = {"statement": statement, "categories": categories,
                 "fact_count": sum(len(m["values"]) for c in categories
                                   for m in c["metrics"])}
        entry["comparative"] = _comparative_verdict(categories)
        out.append(entry)
    order = [str(s) for s in StatementType]
    out.sort(key=lambda s: order.index(s["statement"])
             if s["statement"] in order else 99)
    return {"statements": out, "total_facts": len(facts)}


# --------------------------------------------------------------- explanation
@router.get("/{document_id}/financials/{fact_id}/explanation")
def explain(document_id: str, fact_id: str, session: DbSession) -> dict:
    """Why the system believes one figure, assembled from stored evidence."""
    from app.analysis.explain import explain_fact
    from app.database.repository import row_to_fact

    get_document_or_404(document_id, session)
    row = session.get(FinancialFactRow, fact_id)
    if row is None or row.document_id != document_id:
        raise ResourceNotFound(f"No fact {fact_id!r} in this document")

    prov = session.scalars(select(ProvenanceRow)
                           .where(ProvenanceRow.fact_id == fact_id)).first()
    fact = row_to_fact(row, prov)

    validations = [v for v in session.scalars(
        select(ValidationResultRow)
        .where(ValidationResultRow.document_id == document_id))
        if fact_id in (v.fact_ids or [])]
    derived_rows = list(session.scalars(
        select(DerivedMetricRow)
        .where(DerivedMetricRow.document_id == document_id,
               DerivedMetricRow.reported_fact_id == fact_id)))
    relation_rows = [r for r in session.scalars(
        select(FactRelationRow)
        .where(FactRelationRow.document_id == document_id))
        if fact_id in (r.from_fact_id, r.to_fact_id)]

    from app.models.schemas import DerivedMetric, FactRelation, ValidationResult
    explanation = explain_fact(
        fact,
        validations=[ValidationResult.model_validate({
            "id": v.id, "document_id": v.document_id, "rule": v.rule,
            "rule_family": v.rule_family, "status": v.status,
            "severity": v.severity, "message": v.message,
            "reported_value": v.reported_value,
            "calculated_value": v.calculated_value,
            "difference": v.difference, "tolerance": v.tolerance,
            "fact_ids": v.fact_ids or [], "page_numbers": v.page_numbers or [],
            "detail": v.detail or {},
        }) for v in validations],
        derived=[DerivedMetric.model_validate(_derived_dto(d))
                 for d in derived_rows],
        relations=[FactRelation.model_validate({
            "id": r.id, "document_id": r.document_id,
            "from_fact_id": r.from_fact_id, "to_fact_id": r.to_fact_id,
            "kind": r.kind, "detail": r.detail or {},
            "confidence": r.confidence}) for r in relation_rows],
        review_history=review_service.history(session, document_id,
                                              target_id=fact_id),
    )
    return explanation.model_dump(mode="json")


# ---------------------------------------------------------------- taxonomy
@router.get("/{document_id}/taxonomy-coverage")
def taxonomy_coverage(document_id: str, session: DbSession) -> dict:
    """Which taxonomy metrics this document actually supplied."""
    get_document_or_404(document_id, session)
    found: dict[str, int] = {}
    for metric, in session.execute(
            select(FinancialFactRow.canonical_metric)
            .where(FinancialFactRow.document_id == document_id)).all():
        if metric:
            found[metric] = found.get(metric, 0) + 1
    by_statement: dict[str, dict] = {}
    for definition in taxonomy.all_metrics():
        bucket = by_statement.setdefault(
            definition["statement"], {"present": [], "absent": []})
        entry = {"key": definition["key"], "display": definition["display"],
                 "category": definition["category"],
                 "count": found.get(definition["key"], 0)}
        (bucket["present"] if entry["count"] else bucket["absent"]).append(entry)
    return {
        "by_statement": by_statement,
        "metrics_defined": len(taxonomy.all_metrics()),
        "metrics_found": len(found),
    }
