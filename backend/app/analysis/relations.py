"""The financial relationship graph.

Facts are not independent rows.  The same measure appears in several periods,
a growth figure belongs to a value, a segment rolls into a group, one number is
the input to a computed ratio, and sometimes two extractions describe the same
cell.  This module makes those links explicit and typed, so the API and the UI
can navigate from any fact to the facts that give it meaning.

Every link is *derived from what the document says*, never assumed:

* a comparison only exists between facts of the same metric, entity, unit and
  currency in different periods;
* a component link only exists where the taxonomy declares a parent/child
  relationship, or where the entity hierarchy does;
* duplicates and conflicts are recorded as links, not resolved by deletion.
"""
from __future__ import annotations

from collections import defaultdict

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import taxonomy
from app.models.enums import Currency, RelationKind, ValueType
from app.models.schemas import DerivedMetric, FactRelation, FinancialFact
from app.normalization.periods import (
    is_quarter_over_quarter, is_year_over_year,
)

log = get_logger("analysis.relations")

# Which metric is a component of which.  Derived from accounting structure, not
# from any single filer's line items.
COMPONENT_OF: dict[str, str] = {
    "revenue_from_operations": "total_income",
    "other_income": "total_income",
    "investment_income": "total_income",
    "raw_material_cost": "total_expenses",
    "employee_cost": "total_expenses",
    # "finance_cost" here named no metric the taxonomy defines, so the
    # relationship was never built for any document.
    "finance_costs": "total_expenses",
    "depreciation": "total_expenses",
    "other_expenses": "total_expenses",
    "segment_revenue": "revenue_from_operations",
    "segment_ebitda": "ebitda",
    "segment_ebit": "ebit",
    "current_assets": "total_assets",
    "current_liabilities": "total_liabilities",
    "receivables": "current_assets",
    "inventory": "current_assets",
    "payables": "current_liabilities",
}


def _comparable(a: FinancialFact, b: FinancialFact) -> bool:
    """Whether two facts describe the same measure of the same thing.

    Unit and currency must agree, because a comparison across an unconverted
    currency or an unreconciled magnitude is meaningless.
    """
    if a.canonical_metric != b.canonical_metric or not a.canonical_metric:
        return False
    if (a.entity_id or a.entity) != (b.entity_id or b.entity):
        return False
    if a.value_type != b.value_type:
        return False
    if a.value_type == ValueType.MONETARY:
        if a.currency != b.currency:
            return False
        if Currency.UNKNOWN in (a.currency, b.currency):
            # An unknown currency on either side means we cannot assert they
            # are the same money; still comparable if both are unknown and the
            # magnitudes reconcile through base values.
            if a.base_value is None or b.base_value is None:
                return False
    return True


def _relation(document_id: str, source: FinancialFact | str,
              target: FinancialFact | str, kind: RelationKind,
              detail: dict | None = None, confidence: float = 1.0
              ) -> FactRelation:
    from_id = source if isinstance(source, str) else source.fact_id
    to_id = target if isinstance(target, str) else target.fact_id
    return FactRelation(
        id=stable_id("rel", document_id, from_id, to_id, str(kind)),
        document_id=document_id, from_fact_id=from_id, to_fact_id=to_id,
        kind=kind, detail=detail or {}, confidence=confidence)


def _period_key(fact: FinancialFact) -> tuple[int, int]:
    period = fact.period_normalized
    if period is None:
        return (0, 0)
    return (period.fiscal_year or 0, period.quarter or 0)


def build_relations(document_id: str, facts: list[FinancialFact],
                    derived: list[DerivedMetric] | None = None,
                    ) -> list[FactRelation]:
    """Build every relationship the extracted facts support."""
    derived = derived or []
    relations: list[FactRelation] = []
    seen: set[tuple[str, str, str]] = set()

    def add(rel: FactRelation) -> None:
        key = (rel.from_fact_id, rel.to_fact_id, str(rel.kind))
        if key in seen or rel.from_fact_id == rel.to_fact_id:
            return
        seen.add(key)
        relations.append(rel)

    # ---- comparisons across periods -------------------------------------
    by_series: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.canonical_metric and f.period:
            by_series[(f.entity_id or f.entity or "", f.canonical_metric)].append(f)

    for series in by_series.values():
        ordered = sorted(series, key=_period_key, reverse=True)
        for i, current in enumerate(ordered):
            for prior in ordered[i + 1:]:
                if not _comparable(current, prior):
                    continue
                if current.period == prior.period:
                    continue
                yoy = is_year_over_year(current.period_normalized,
                                        prior.period_normalized)
                qoq = is_quarter_over_quarter(current.period_normalized,
                                              prior.period_normalized)
                if not (yoy or qoq):
                    continue
                detail = {"basis": "yoy" if yoy else "qoq",
                          "from_period": current.period,
                          "to_period": prior.period}
                if (current.value is not None and prior.value not in (None, 0)
                        and current.value_type != ValueType.PERCENTAGE):
                    detail["change_pct"] = round(
                        (current.value - prior.value) / abs(prior.value) * 100.0, 4)
                elif current.value is not None and prior.value is not None:
                    detail["change_pp"] = round(current.value - prior.value, 4)
                add(_relation(document_id, current, prior,
                              RelationKind.COMPARISON, detail))
                if detail.get("change_pct") is not None:
                    add(_relation(document_id, current, prior,
                                  RelationKind.GROWTH_OF,
                                  {"basis": detail["basis"],
                                   "growth_pct": detail["change_pct"]}))

    # ---- component roll-ups ---------------------------------------------
    by_slot: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.canonical_metric and f.period:
            by_slot[(f.entity_id or f.entity or "", f.period,
                     f.canonical_metric)].append(f)

    for (entity, period, metric), members in by_slot.items():
        parent_metric = COMPONENT_OF.get(metric)
        if not parent_metric:
            continue
        parents = by_slot.get((entity, period, parent_metric), [])
        for child in members:
            for parent in parents:
                if child.value_type != parent.value_type:
                    continue
                add(_relation(document_id, child, parent,
                              RelationKind.COMPONENT_OF,
                              {"parent_metric": parent_metric,
                               "period": period}))

    # ---- segment facts roll into their group ----------------------------
    for f in facts:
        if not f.parent_entity_id or not f.canonical_metric or not f.period:
            continue
        for parent in by_slot.get((f.parent_entity_id, f.period,
                                   f.canonical_metric), []):
            add(_relation(document_id, f, parent, RelationKind.COMPONENT_OF,
                          {"basis": "entity_hierarchy",
                           "parent_entity_id": f.parent_entity_id},
                          confidence=0.85))

    # ---- inputs to derived metrics --------------------------------------
    for metric in derived:
        for fact_id in metric.input_fact_ids:
            add(_relation(document_id, fact_id, metric.id,
                          RelationKind.INPUT_TO,
                          {"formula": metric.formula,
                           "derived_metric": metric.canonical_metric}))
        if metric.reported_fact_id:
            add(_relation(document_id, metric.id, metric.reported_fact_id,
                          RelationKind.DERIVED_FROM,
                          {"agrees": metric.agrees_with_reported,
                           "difference": metric.difference}))

    log.info("relations.built", document_id=document_id, count=len(relations))
    return relations


def is_child_metric(metric: str) -> bool:
    return metric in COMPONENT_OF


def parent_metric(metric: str) -> str | None:
    parent = COMPONENT_OF.get(metric)
    if parent and taxonomy.get(parent) is None:
        return None
    return parent
