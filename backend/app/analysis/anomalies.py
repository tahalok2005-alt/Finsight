"""Explainable anomaly detection.

An anomaly here is never "the model thought this looked odd".  Each one names
the rule it broke, the values involved, the threshold it crossed and the facts
it came from, so a reviewer can agree or disagree from the evidence alone.

Detectors:

* **reported vs calculated** -- the document printed a ratio that its own
  figures do not reproduce;
* **unusual growth** -- a period-on-period move beyond a stated threshold;
* **margin swing** -- a rate that moved further in one period than rates
  normally do;
* **segment vs total mismatch** -- named components exceed the total they
  belong to (only checked when the document printed both);
* **unit change** -- the same measure reported in two different magnitudes;
* **sign flip** -- a metric the taxonomy says has a fixed sign appearing with
  the wrong one;
* **outlier value** -- a figure far outside the distribution of its own series.

Thresholds are constants, stated on each anomaly, not learned -- a single
document is far too small a sample to learn a distribution from.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import taxonomy
from app.models.enums import AnomalyType, Severity, ValueType
from app.models.schemas import Anomaly, DerivedMetric, FinancialFact

log = get_logger("analysis.anomalies")

GROWTH_THRESHOLD_PCT = 75.0     # a >75% move is worth a second look
MARGIN_SWING_BPS = 800.0        # 8 percentage points
OUTLIER_Z = 3.0
MIN_SERIES_FOR_OUTLIER = 4


def _anomaly(document_id: str, kind: AnomalyType, severity: Severity,
             message: str, reason: str, **kw) -> Anomaly:
    return Anomaly(
        id=stable_id("anom", document_id, str(kind), kw.get("metric") or "",
                     kw.get("period") or "", kw.get("entity_name") or "",
                     str(kw.get("observed_value"))),
        document_id=document_id, anomaly_type=kind, severity=severity,
        message=message, reason=reason, **kw)


def _reported_vs_calculated(document_id: str, derived: list[DerivedMetric],
                            ) -> list[Anomaly]:
    out: list[Anomaly] = []
    for m in derived:
        if m.agrees_with_reported is not False or m.reported_value is None:
            continue
        base = abs(m.reported_value) or 1.0
        deviation = abs(m.difference or 0) / base * 100.0
        severity = (Severity.HIGH if deviation > 10 else
                    Severity.MEDIUM if deviation > 3 else Severity.LOW)
        out.append(_anomaly(
            document_id, AnomalyType.REPORTED_VS_CALCULATED, severity,
            message=(f"{m.metric_display} reported as {m.reported_value:,.2f} "
                     f"but computes to {m.value:,.2f} for {m.period}"),
            reason=(f"Recomputing {m.formula} from the document's own reported "
                    f"figures gives {m.value:,.2f}, which differs from the "
                    f"printed {m.reported_value:,.2f} by {m.difference:,.2f} "
                    f"({deviation:,.1f}%). Either an input was misread, or the "
                    f"document defines this measure differently."),
            entity_id=m.entity_id, entity_name=m.entity_name,
            metric=m.canonical_metric, period=m.period,
            observed_value=m.reported_value, expected_value=m.value,
            deviation=m.difference,
            threshold=round(base * 0.01, 4),
            fact_ids=[*m.input_fact_ids] + ([m.reported_fact_id]
                                            if m.reported_fact_id else []),
            source_pages=list(m.source_pages)))
    return out


def _series(facts: list[FinancialFact]) -> dict[tuple, list[FinancialFact]]:
    out: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.canonical_metric and f.period and f.value is not None:
            out[(f.entity_id or f.entity or "", f.canonical_metric)].append(f)
    for members in out.values():
        members.sort(key=lambda f: ((f.period_normalized.fiscal_year or 0,
                                     f.period_normalized.quarter or 0)
                                    if f.period_normalized else (0, 0)),
                     reverse=True)
    return out


def _growth_and_margin(document_id: str, facts: list[FinancialFact],
                       ) -> list[Anomaly]:
    out: list[Anomaly] = []
    for (entity, metric), members in _series(facts).items():
        if len(members) < 2:
            continue
        definition = taxonomy.get(metric)
        current, prior = members[0], members[1]
        if (current.period == prior.period or prior.value in (None, 0)
                or current.currency != prior.currency
                or current.unit != prior.unit):
            continue

        if definition is not None and definition.is_rate:
            bps = (current.value - prior.value) * 100.0
            if abs(bps) >= MARGIN_SWING_BPS:
                out.append(_anomaly(
                    document_id, AnomalyType.MARGIN_SWING,
                    Severity.MEDIUM if abs(bps) < 1500 else Severity.HIGH,
                    message=(f"{definition.display} moved {bps:,.0f} bps "
                             f"between {prior.period} and {current.period}"),
                    reason=(f"{prior.period}: {prior.value:,.2f}% -> "
                            f"{current.period}: {current.value:,.2f}%. A swing "
                            f"beyond {MARGIN_SWING_BPS:,.0f} bps is unusual for "
                            f"a rate and may indicate a misread figure or a "
                            f"one-off item."),
                    entity_id=current.entity_id, entity_name=current.entity,
                    metric=metric, period=current.period,
                    observed_value=current.value, expected_value=prior.value,
                    deviation=round(bps, 1), threshold=MARGIN_SWING_BPS,
                    fact_ids=[current.fact_id, prior.fact_id],
                    source_pages=sorted({p for p in (current.source_page,
                                                     prior.source_page) if p})))
            continue

        change = (current.value - prior.value) / abs(prior.value) * 100.0
        if abs(change) >= GROWTH_THRESHOLD_PCT:
            out.append(_anomaly(
                document_id, AnomalyType.UNUSUAL_GROWTH,
                Severity.MEDIUM if abs(change) < 200 else Severity.HIGH,
                message=(f"{(definition.display if definition else metric)} "
                         f"changed {change:+,.1f}% between {prior.period} and "
                         f"{current.period}"),
                reason=(f"{prior.period}: {prior.value:,.2f} -> "
                        f"{current.period}: {current.value:,.2f}, a "
                        f"{change:+,.1f}% move against a "
                        f"{GROWTH_THRESHOLD_PCT:,.0f}% threshold. Large moves "
                        f"are legitimate after an acquisition or a one-off, "
                        f"but also occur when a value or period is misread."),
                entity_id=current.entity_id, entity_name=current.entity,
                metric=metric, period=current.period,
                observed_value=current.value, expected_value=prior.value,
                deviation=round(change, 2), threshold=GROWTH_THRESHOLD_PCT,
                fact_ids=[current.fact_id, prior.fact_id],
                source_pages=sorted({p for p in (current.source_page,
                                                 prior.source_page) if p})))

        # sign discipline: a metric declared positive should not print negative
        if definition is not None and definition.sign == "positive" \
                and current.value < 0:
            out.append(_anomaly(
                document_id, AnomalyType.SIGN_FLIP, Severity.HIGH,
                message=(f"{definition.display} is negative "
                         f"({current.value:,.2f}) in {current.period}"),
                reason=(f"The taxonomy declares {definition.display} as a "
                        f"non-negative measure. A negative value usually means "
                        f"a bracketed figure was read from the wrong column, or "
                        f"the row label was matched to the wrong metric."),
                entity_id=current.entity_id, entity_name=current.entity,
                metric=metric, period=current.period,
                observed_value=current.value, threshold=0.0,
                fact_ids=[current.fact_id],
                source_pages=[current.source_page] if current.source_page else []))
    return out


def _segment_vs_total(document_id: str, facts: list[FinancialFact],
                      ) -> list[Anomaly]:
    """Components that exceed the total the document printed for them.

    Only fires when the document published *both* the parts and the whole for
    the same metric, period, entity parent, unit and currency.  Segment values
    are never summed to manufacture a total.
    """
    out: list[Anomaly] = []
    by_key: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.canonical_metric and f.period and f.value is not None:
            by_key[(f.canonical_metric, f.period)].append(f)

    for (metric, period), members in by_key.items():
        definition = taxonomy.get(metric)
        if definition is None or definition.is_rate:
            continue
        totals = [f for f in members if not f.parent_entity_id]
        if not totals:
            continue
        total = max(totals, key=lambda f: abs(f.value or 0))
        children = [f for f in members
                    if f.parent_entity_id == total.entity_id
                    and f.currency == total.currency and f.unit == total.unit]
        if len(children) < 2 or not total.value:
            continue
        summed = sum(c.value for c in children)
        if summed <= abs(total.value) * 1.02:
            continue
        excess = summed - total.value
        out.append(_anomaly(
            document_id, AnomalyType.SEGMENT_TOTAL_MISMATCH, Severity.MEDIUM,
            message=(f"Named components of {definition.display} sum to "
                     f"{summed:,.2f}, above the reported total "
                     f"{total.value:,.2f} for {period}"),
            reason=(f"{len(children)} component figures "
                    f"({', '.join(sorted({c.entity or '?' for c in children}))}) "
                    f"exceed the printed total by {excess:,.2f}. Inter-segment "
                    f"eliminations legitimately cause this; so does attributing "
                    f"a figure to the wrong reporting unit."),
            entity_id=total.entity_id, entity_name=total.entity,
            metric=metric, period=period,
            observed_value=round(summed, 4), expected_value=total.value,
            deviation=round(excess, 4),
            threshold=round(abs(total.value) * 0.02, 4),
            fact_ids=[total.fact_id, *[c.fact_id for c in children]],
            source_pages=sorted({c.source_page for c in
                                 [total, *children] if c.source_page})))
    return out


def _unit_changes(document_id: str, facts: list[FinancialFact]) -> list[Anomaly]:
    out: list[Anomaly] = []
    by_key: dict[tuple, set] = defaultdict(set)
    examples: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.canonical_metric and f.period and f.value_type == ValueType.MONETARY:
            key = (f.entity_id or f.entity or "", f.canonical_metric, f.period)
            by_key[key].add((str(f.unit), str(f.currency)))
            examples[key].append(f)

    for key, variants in by_key.items():
        if len(variants) < 2:
            continue
        entity, metric, period = key
        definition = taxonomy.get(metric)
        members = examples[key]
        out.append(_anomaly(
            document_id, AnomalyType.UNIT_CHANGE, Severity.MEDIUM,
            message=(f"{(definition.display if definition else metric)} appears "
                     f"in {len(variants)} different unit/currency combinations "
                     f"for {period}"),
            reason=("The same measure, entity and period was extracted with "
                    + ", ".join(f"{u}/{c}" for u, c in sorted(variants))
                    + ". Either the document restates the figure in another "
                      "unit, or a unit declaration was applied to the wrong "
                      "table."),
            entity_id=members[0].entity_id, entity_name=members[0].entity,
            metric=metric, period=period,
            fact_ids=[f.fact_id for f in members],
            source_pages=sorted({f.source_page for f in members if f.source_page})))
    return out


def _outliers(document_id: str, facts: list[FinancialFact]) -> list[Anomaly]:
    out: list[Anomaly] = []
    for (entity, metric), members in _series(facts).items():
        if len(members) < MIN_SERIES_FOR_OUTLIER:
            continue
        units = {(str(f.unit), str(f.currency)) for f in members}
        if len(units) > 1:
            continue                      # a mixed-unit series is not a series
        values = [f.value for f in members]
        mean = statistics.fmean(values)
        try:
            sd = statistics.stdev(values)
        except statistics.StatisticsError:
            continue
        if sd == 0:
            continue
        for f in members:
            z = abs(f.value - mean) / sd
            if z < OUTLIER_Z:
                continue
            definition = taxonomy.get(metric)
            out.append(_anomaly(
                document_id, AnomalyType.OUTLIER_VALUE, Severity.LOW,
                message=(f"{(definition.display if definition else metric)} of "
                         f"{f.value:,.2f} in {f.period} sits "
                         f"{z:,.1f} standard deviations from the mean of its "
                         f"own series"),
                reason=(f"Series of {len(values)} values for this metric and "
                        f"entity has mean {mean:,.2f} and standard deviation "
                        f"{sd:,.2f}. A single document is a small sample, so "
                        f"this is a prompt to check the figure, not a verdict."),
                entity_id=f.entity_id, entity_name=f.entity, metric=metric,
                period=f.period, observed_value=f.value,
                expected_value=round(mean, 4), deviation=round(z, 2),
                threshold=OUTLIER_Z, fact_ids=[f.fact_id],
                source_pages=[f.source_page] if f.source_page else []))
    return out


def _conflicting_duplicates(document_id: str, facts: list[FinancialFact],
                            ) -> list[Anomaly]:
    """One anomaly per contradicting *pair*.

    Both sides of a conflict record it, so iterating facts naively reports the
    same disagreement twice.  The pair of fact ids is the identity here.
    """
    out: list[Anomaly] = []
    reported: set[tuple[str, str]] = set()
    for f in facts:
        conflicts = f.meta.get("conflicts") or []
        if not conflicts:
            continue
        other = conflicts[0]
        pair = tuple(sorted((f.fact_id, other.get("fact_id", ""))))
        if pair in reported:
            continue
        reported.add(pair)
        definition = taxonomy.get(f.canonical_metric or "")
        out.append(_anomaly(
            document_id, AnomalyType.CONTRADICTORY_DUPLICATE, Severity.HIGH,
            message=(f"{(definition.display if definition else f.metric)} has "
                     f"two different values for {f.period}"),
            reason=(f"Page {f.source_page} reports {f.value:,.2f}; page "
                    f"{other.get('page')} reports "
                    f"{(other.get('value') or 0):,.2f} for the same measure, "
                    f"entity, period and unit. Both were kept -- FinSight does "
                    f"not choose between contradictory source figures."),
            entity_id=f.entity_id, entity_name=f.entity,
            metric=f.canonical_metric, period=f.period,
            observed_value=f.value, expected_value=other.get("value"),
            fact_ids=[f.fact_id, other.get("fact_id", "")],
            source_pages=sorted({p for p in (f.source_page, other.get("page"))
                                 if p})))
    return out


def detect_anomalies(document_id: str, facts: list[FinancialFact],
                     derived: list[DerivedMetric] | None = None,
                     *, limit: int = 60) -> list[Anomaly]:
    """Run every detector and return the findings, most severe first."""
    derived = derived or []
    found: list[Anomaly] = []
    found.extend(_reported_vs_calculated(document_id, derived))
    found.extend(_growth_and_margin(document_id, facts))
    found.extend(_segment_vs_total(document_id, facts))
    found.extend(_unit_changes(document_id, facts))
    found.extend(_outliers(document_id, facts))
    found.extend(_conflicting_duplicates(document_id, facts))

    seen: set[str] = set()
    unique: list[Anomaly] = []
    for a in found:
        if a.id in seen:
            continue
        seen.add(a.id)
        unique.append(a)

    rank = {str(Severity.HIGH): 0, str(Severity.MEDIUM): 1,
            str(Severity.LOW): 2, str(Severity.INFO): 3}
    unique.sort(key=lambda a: (rank.get(str(a.severity), 9),
                               -abs(a.deviation or 0)))
    log.info("anomalies.detected", document_id=document_id, count=len(unique))
    return unique[:limit]
