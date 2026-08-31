"""Financial validation engine.

The system's purpose is trustworthy data, so every extracted number is checked
against the document's own internal arithmetic.  Rules are grouped into
families:

* **growth**      - recompute a reported % change from the two period values
* **margin**      - recompute a reported margin from its numerator/denominator
* **derived**     - check identities such as PBT - Tax = PAT, Debt - Cash = Net Debt
* **ratio**       - check reported ratios such as Net Debt / EBITDA
* **aggregation** - segment values against the reported consolidated total
* **consistency** - duplicates, period coverage, sign and magnitude sanity

Two invariants hold throughout:

1. The document is never "corrected".  Both the reported and the calculated
   value are stored, and the discrepancy is surfaced for a human.
2. A check only runs when its inputs are genuinely comparable (same currency,
   same magnitude, same entity, same period).  Comparing a crore figure with a
   million figure would manufacture false warnings, so it is refused instead.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from app.config.settings import settings
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.enums import (
    Currency, MagnitudeUnit, StatementNature, ValidationSeverity,
    ValidationStatus, ValueType,
)
from app.models.schemas import FinancialFact, ValidationResult
from app.normalization.numbers import MAGNITUDE_FACTORS

log = get_logger("validation.engine")

Key = tuple[str, str, str]  # (entity, period, canonical_metric)


@dataclass(slots=True)
class ValidationReport:
    results: list[ValidationResult]
    fact_status: dict[str, ValidationStatus]

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.results
                   if r.status in (ValidationStatus.WARNING,
                                   ValidationStatus.REQUIRES_REVIEW))

    @property
    def failures(self) -> int:
        return sum(1 for r in self.results if r.status == ValidationStatus.FAILED)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == ValidationStatus.PASSED)


def _idx(facts: Sequence[FinancialFact]) -> dict[Key, list[FinancialFact]]:
    """Bucket facts by (entity, period, metric) for cross-fact rules.

    Facts with **no** period are excluded.  "Unattributed" is not a period: two
    figures that both failed period resolution are not thereby known to belong
    to the same period, and bucketing them together lets a margin rule divide
    one quarter's profit by another quarter's revenue.  They are still checked
    individually by the sanity and coverage rules, which is where a missing
    period gets surfaced.
    """
    out: dict[Key, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.nature != StatementNature.ACTUAL and f.nature != "actual":
            continue
        if not f.period:
            continue
        out[(f.entity or "", f.period, f.canonical_metric)].append(f)
    return out


def _pick(bucket: dict[Key, list[FinancialFact]], entity: str, period: str,
          metric: str) -> FinancialFact | None:
    group = bucket.get((entity, period, metric))
    if not group:
        return None
    return max(group, key=lambda f: f.confidence)


def _same_scale(a: FinancialFact, b: FinancialFact) -> bool:
    """True only when two monetary facts can be compared without conversion."""
    if a.value_type != b.value_type:
        return False
    if a.value_type != ValueType.MONETARY:
        return True
    if a.currency != b.currency and Currency.UNKNOWN not in (a.currency, b.currency):
        return False
    return a.unit == b.unit


def _rescale(f: FinancialFact, target: MagnitudeUnit) -> float | None:
    if f.value is None:
        return None
    if f.unit == target:
        return f.value
    fa, ft = MAGNITUDE_FACTORS.get(f.unit), MAGNITUDE_FACTORS.get(target)
    if fa is None or ft in (None, 0):
        return None
    return f.value * fa / ft


def _result(document_id: str, rule: str, family: str, status: ValidationStatus,
            message: str, *, severity: ValidationSeverity | None = None,
            reported: float | None = None, calculated: float | None = None,
            tolerance: float | None = None, facts: Iterable[FinancialFact] = (),
            detail: dict | None = None) -> ValidationResult:
    fact_list = list(facts)
    diff = (None if reported is None or calculated is None
            else round(reported - calculated, 6))
    if severity is None:
        severity = {
            ValidationStatus.PASSED: ValidationSeverity.INFO,
            ValidationStatus.WARNING: ValidationSeverity.WARNING,
            ValidationStatus.REQUIRES_REVIEW: ValidationSeverity.WARNING,
            ValidationStatus.FAILED: ValidationSeverity.ERROR,
        }.get(status, ValidationSeverity.INFO)
    return ValidationResult(
        id=stable_id("val", document_id, rule,
                     ",".join(sorted(f.fact_id for f in fact_list))),
        document_id=document_id, rule=rule, rule_family=family, status=status,
        severity=severity, message=message,
        reported_value=(None if reported is None else round(reported, 6)),
        calculated_value=(None if calculated is None else round(calculated, 6)),
        difference=diff, tolerance=tolerance,
        fact_ids=[f.fact_id for f in fact_list],
        page_numbers=sorted({f.source_page for f in fact_list}),
        detail=detail or {})


# ---------------------------------------------------------------- rule bodies
def check_growth(document_id: str, facts: Sequence[FinancialFact],
                 bucket: dict[Key, list[FinancialFact]]) -> list[ValidationResult]:
    """Recompute each reported growth figure from the two period values."""
    out: list[ValidationResult] = []
    tol = settings.growth_tolerance_pct
    for f in facts:
        if f.growth is None or not f.growth_reported:
            continue
        if f.value is None or f.comparison_value in (None, 0):
            continue
        if f.value_type in (ValueType.PERCENTAGE, ValueType.RATIO):
            continue
        prev = f.comparison_value
        calc = (f.value - prev) / abs(prev) * 100.0
        delta = abs(calc - f.growth)
        if delta <= tol:
            status, msg = ValidationStatus.PASSED, (
                f"Reported {f.growth_type or 'growth'} of {f.growth:.1f}% for "
                f"{f.metric_display or f.canonical_metric} ({f.period}) agrees "
                f"with the value pair.")
        else:
            status = (ValidationStatus.FAILED if delta > max(5.0, tol * 8)
                      else ValidationStatus.WARNING)
            msg = (f"{f.metric_display or f.canonical_metric} ({f.period}): "
                   f"document reports {f.growth:.1f}% {f.growth_type or ''} but "
                   f"{f.value:,.0f} vs {prev:,.0f} computes to {calc:.1f}%.")
        out.append(_result(
            document_id, "growth_matches_values", "growth", status, msg,
            reported=f.growth, calculated=calc, tolerance=tol, facts=[f],
            detail={"metric": f.canonical_metric, "period": f.period,
                    "comparison_period": f.comparison_period,
                    "current": f.value, "previous": prev}))
    return out


def check_margins(document_id: str, bucket: dict[Key, list[FinancialFact]],
                  ) -> list[ValidationResult]:
    """Recompute reported margins from their numerator and denominator."""
    pairs = [
        ("ebitda_margin", "ebitda", ("revenue_from_operations", "gross_revenue",
                                     "revenue", "net_revenue", "sales")),
        ("ebit_margin", "ebit", ("revenue_from_operations", "gross_revenue",
                                 "revenue", "net_revenue", "sales")),
        ("net_margin", "pat", ("revenue_from_operations", "gross_revenue",
                               "revenue", "net_revenue", "sales")),
    ]
    out: list[ValidationResult] = []
    tol = settings.margin_tolerance_pct
    seen: set[tuple] = set()
    for (entity, period, metric), group in bucket.items():
        for margin_key, num_key, den_keys in pairs:
            if metric != margin_key:
                continue
            margin = max(group, key=lambda f: f.confidence)
            if margin.value is None:
                continue
            numerator = _pick(bucket, entity, period, num_key)
            if numerator is None or numerator.value in (None, 0):
                continue
            denominator = None
            for dk in den_keys:
                cand = _pick(bucket, entity, period, dk)
                if cand is not None and cand.value not in (None, 0):
                    denominator = cand
                    break
            if denominator is None:
                continue
            if not _same_scale(numerator, denominator):
                rescaled = _rescale(denominator, numerator.unit)
                if rescaled is None:
                    out.append(_result(
                        document_id, "margin_units_incomparable", "margin",
                        ValidationStatus.REQUIRES_REVIEW,
                        f"{margin_key} for {entity or 'entity'} {period} could not "
                        f"be checked: {num_key} is in {numerator.unit} but "
                        f"{denominator.canonical_metric} is in {denominator.unit}.",
                        facts=[margin, numerator, denominator]))
                    continue
                den_value = rescaled
            else:
                den_value = denominator.value
            if not den_value:
                continue
            key = (entity, period, margin_key)
            if key in seen:
                continue
            seen.add(key)
            calc = numerator.value / den_value * 100.0
            delta = abs(calc - margin.value)
            if delta <= tol:
                status = ValidationStatus.PASSED
                msg = (f"{margin.metric_display} {period}: reported "
                       f"{margin.value:.1f}% matches "
                       f"{num_key}/{denominator.canonical_metric}.")
            else:
                status = (ValidationStatus.FAILED if delta > max(3.0, tol * 8)
                          else ValidationStatus.WARNING)
                msg = (f"{margin.metric_display} {period}: document reports "
                       f"{margin.value:.2f}% but {numerator.value:,.0f} / "
                       f"{den_value:,.0f} = {calc:.2f}%.")
            out.append(_result(
                document_id, "margin_matches_components", "margin", status, msg,
                reported=margin.value, calculated=calc, tolerance=tol,
                facts=[margin, numerator, denominator],
                detail={"numerator": num_key, "denominator":
                        denominator.canonical_metric, "entity": entity,
                        "period": period}))
    return out


def check_identities(document_id: str, bucket: dict[Key, list[FinancialFact]],
                     ) -> list[ValidationResult]:
    """Check accounting identities that must hold within one period."""
    identities: list[tuple[str, str, list[str], list[str], str]] = [
        # (rule, result_metric, plus, minus, human description)
        ("pbt_less_tax_is_pat", "pat", ["pbt"], ["tax_expense"],
         "Profit Before Tax - Tax Expense = Profit After Tax"),
        ("debt_less_cash_is_net_debt", "net_debt", ["outstanding_debt"],
         ["cash_and_equivalents"],
         "Outstanding Debt - Cash & Equivalents = Net Debt"),
    ]
    out: list[ValidationResult] = []
    tol_rel = settings.total_tolerance_rel
    # One check per (entity, period) -- iterating the full bucket keys would run
    # each identity once per metric present and emit N identical results.
    scopes = sorted({(e, p) for (e, p, _m) in bucket})
    for entity, period in scopes:
        for rule, target, plus, minus, desc in identities:
            res = _pick(bucket, entity, period, target)
            if res is None or res.value is None:
                continue
            terms: list[FinancialFact] = []
            total = 0.0
            ok = True
            for key, sign in [(k, 1) for k in plus] + [(k, -1) for k in minus]:
                f = _pick(bucket, entity, period, key)
                if f is None or f.value is None:
                    ok = False
                    break
                if not _same_scale(f, res):
                    v = _rescale(f, res.unit)
                    if v is None:
                        ok = False
                        break
                else:
                    v = f.value
                total += sign * v
                terms.append(f)
            if not ok or not terms:
                continue
            scale = max(abs(res.value), 1.0)
            delta = abs(total - res.value) / scale
            if delta <= tol_rel:
                status = ValidationStatus.PASSED
                msg = f"{desc} holds for {entity or 'entity'} {period}."
            else:
                status = (ValidationStatus.FAILED if delta > tol_rel * 5
                          else ValidationStatus.WARNING)
                msg = (f"{desc} does not hold for {entity or 'entity'} {period}: "
                       f"computed {total:,.0f} vs reported {res.value:,.0f} "
                       f"({delta * 100:.1f}% apart).")
            out.append(_result(
                document_id, rule, "derived", status, msg,
                reported=res.value, calculated=total, tolerance=tol_rel,
                facts=[res, *terms],
                detail={"identity": desc, "entity": entity, "period": period}))
    return out


def check_ratios(document_id: str, bucket: dict[Key, list[FinancialFact]],
                 ) -> list[ValidationResult]:
    """Check reported ratios, respecting the periodicity of their inputs.

    Net Debt / EBITDA is a stock over a *flow*: comparing a quarter-end debt
    balance with one quarter of EBITDA gives a number four times the annualised
    figure filers normally quote, so both readings are accepted and the basis is
    recorded rather than one being declared wrong.
    """
    out: list[ValidationResult] = []
    tol = settings.ratio_tolerance_rel
    for (entity, period, metric), group in bucket.items():
        if metric != "net_debt_to_ebitda":
            continue
        ratio = max(group, key=lambda f: f.confidence)
        nd = _pick(bucket, entity, period, "net_debt")
        eb = _pick(bucket, entity, period, "ebitda")
        if ratio.value is None or nd is None or eb is None:
            continue
        if nd.value is None or not eb.value:
            continue
        eb_value = eb.value if _same_scale(nd, eb) else _rescale(eb, nd.unit)
        if not eb_value:
            continue
        raw = nd.value / eb_value
        annualised = nd.value / (eb_value * 4.0)
        best, basis = min(
            ((abs(raw - ratio.value), raw, "as-reported EBITDA"),
             (abs(annualised - ratio.value), annualised, "annualised EBITDA")),
            key=lambda t: t[0])[1:]
        delta = abs(best - ratio.value) / max(abs(ratio.value), 0.01)
        if delta <= tol:
            status = ValidationStatus.PASSED
            msg = (f"Net Debt / EBITDA of {ratio.value:.2f} for {period} is "
                   f"consistent with Net Debt {nd.value:,.0f} on {basis}.")
        else:
            status = ValidationStatus.WARNING
            msg = (f"Net Debt / EBITDA reported as {ratio.value:.2f} for {period}; "
                   f"Net Debt {nd.value:,.0f} / EBITDA {eb_value:,.0f} gives "
                   f"{raw:.2f} ({annualised:.2f} annualised).")
        out.append(_result(
            document_id, "net_debt_to_ebitda_consistent", "ratio", status, msg,
            reported=ratio.value, calculated=best, tolerance=tol,
            facts=[ratio, nd, eb],
            detail={"basis": basis, "raw": round(raw, 4),
                    "annualised": round(annualised, 4)}))
    return out


def check_aggregation(document_id: str, facts: Sequence[FinancialFact],
                      bucket: dict[Key, list[FinancialFact]],
                      ) -> list[ValidationResult]:
    """Compare the sum of segment values against the consolidated total.

    Only runs where a consolidated figure and two or more segment figures exist
    for the same metric, period, currency and unit.  Segment sums legitimately
    fall short of a consolidated total (eliminations, unallocated items), so a
    shortfall is reported as information unless it is large.
    """
    out: list[ValidationResult] = []
    consolidated_names = {"consolidated", "", "group", "total"}
    by_metric_period: dict[tuple[str, str], list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.value is None or f.value_type != ValueType.MONETARY:
            continue
        if not f.period:
            continue  # see _idx: an unattributed period is not a period
        by_metric_period[(f.canonical_metric, f.period)].append(f)

    for (metric, period), group in by_metric_period.items():
        total_fact = next(
            (f for f in group
             if (f.entity or "").strip().lower() in consolidated_names), None)
        if total_fact is None or not total_fact.value:
            continue
        segments = [f for f in group
                    if f is not total_fact
                    and (f.entity or "").strip().lower() not in consolidated_names
                    and _same_scale(f, total_fact)]
        # one value per segment
        by_entity: dict[str, FinancialFact] = {}
        for s in segments:
            key = (s.entity or "").strip().lower()
            if key not in by_entity or s.confidence > by_entity[key].confidence:
                by_entity[key] = s
        segments = list(by_entity.values())
        if len(segments) < 2:
            continue
        seg_sum = sum(s.value for s in segments if s.value is not None)
        diff_rel = abs(seg_sum - total_fact.value) / max(abs(total_fact.value), 1.0)
        if diff_rel <= settings.total_tolerance_rel:
            status = ValidationStatus.PASSED
            msg = (f"{len(segments)} segment values for {metric} {period} sum to "
                   f"{seg_sum:,.0f}, matching the reported total "
                   f"{total_fact.value:,.0f}.")
        elif seg_sum <= total_fact.value * 1.02:
            status = ValidationStatus.PASSED
            msg = (f"{len(segments)} segment values for {metric} {period} sum to "
                   f"{seg_sum:,.0f} against a consolidated {total_fact.value:,.0f}; "
                   f"the shortfall is consistent with eliminations or "
                   f"unallocated items.")
        else:
            status = ValidationStatus.WARNING
            msg = (f"Segment values for {metric} {period} sum to {seg_sum:,.0f}, "
                   f"exceeding the consolidated total {total_fact.value:,.0f} by "
                   f"{diff_rel * 100:.1f}%.")
        out.append(_result(
            document_id, "segments_sum_to_total", "aggregation", status, msg,
            reported=total_fact.value, calculated=seg_sum,
            tolerance=settings.total_tolerance_rel,
            facts=[total_fact, *segments],
            detail={"metric": metric, "period": period,
                    "segments": [s.entity for s in segments]}))
    return out


def check_sanity(document_id: str, facts: Sequence[FinancialFact],
                 ) -> list[ValidationResult]:
    """Sign, range and unit plausibility checks."""
    from app.extraction.taxonomy import get as get_metric  # local: avoids cycle
    out: list[ValidationResult] = []
    for f in facts:
        if f.value is None:
            out.append(_result(
                document_id, "value_unparsed", "consistency",
                ValidationStatus.REQUIRES_REVIEW,
                f"{f.metric} on page {f.source_page} has no parseable value "
                f"(raw text: {f.raw_value!r}).", facts=[f]))
            continue
        definition = get_metric(f.canonical_metric)
        if definition and definition.sign == "positive" and f.value < 0:
            out.append(_result(
                document_id, "unexpected_negative", "consistency",
                ValidationStatus.WARNING,
                f"{f.metric_display or f.metric} ({f.period}) is negative "
                f"({f.value:,.2f}); this metric is normally positive.",
                facts=[f]))
        if f.value_type == ValueType.PERCENTAGE and abs(f.value) > 200:
            out.append(_result(
                document_id, "implausible_percentage", "consistency",
                ValidationStatus.WARNING,
                f"{f.metric_display or f.metric} ({f.period}) is "
                f"{f.value:,.1f}%, outside a plausible range for a percentage.",
                facts=[f]))
        if (f.value_type == ValueType.MONETARY
                and f.unit == MagnitudeUnit.NONE
                and f.kind == "table_cell"):
            out.append(_result(
                document_id, "unit_not_determined", "consistency",
                ValidationStatus.REQUIRES_REVIEW,
                f"{f.metric_display or f.metric} ({f.period}) has no stated "
                f"magnitude; the document did not declare one for this table.",
                facts=[f]))
    return out


def check_period_coverage(document_id: str, facts: Sequence[FinancialFact],
                          ) -> list[ValidationResult]:
    """Flag metrics whose values are missing a period label entirely."""
    out: list[ValidationResult] = []
    missing = [f for f in facts if not f.period and f.value is not None]
    if missing:
        out.append(_result(
            document_id, "facts_without_period", "consistency",
            ValidationStatus.REQUIRES_REVIEW,
            f"{len(missing)} extracted value(s) could not be attributed to a "
            f"reporting period; their column header carried no period label.",
            facts=missing[:40],
            detail={"count": len(missing),
                    "metrics": sorted({f.canonical_metric for f in missing})[:20]}))
    return out


# ------------------------------------------------------------------ entry point
def validate(document_id: str, facts: Sequence[FinancialFact]) -> ValidationReport:
    """Run every rule family and roll the outcome up onto each fact."""
    actuals = [f for f in facts
               if f.nature in (StatementNature.ACTUAL, "actual")]
    bucket = _idx(actuals)

    results: list[ValidationResult] = []
    runners: list[tuple[str, Callable[[], list[ValidationResult]]]] = [
        ("growth", lambda: check_growth(document_id, actuals, bucket)),
        ("margin", lambda: check_margins(document_id, bucket)),
        ("derived", lambda: check_identities(document_id, bucket)),
        ("ratio", lambda: check_ratios(document_id, bucket)),
        ("aggregation", lambda: check_aggregation(document_id, actuals, bucket)),
        ("sanity", lambda: check_sanity(document_id, actuals)),
        ("coverage", lambda: check_period_coverage(document_id, actuals)),
    ]
    for name, fn in runners:
        try:
            results.extend(fn())
        except Exception as exc:  # noqa: BLE001 - one bad rule must not kill the run
            log.exception("validation.rule_failed", rule_family=name,
                          document_id=document_id, error=str(exc))
            results.append(_result(
                document_id, f"{name}_rule_error", name, ValidationStatus.FAILED,
                f"Validation rule family '{name}' raised an error and was "
                f"skipped: {exc}", severity=ValidationSeverity.ERROR))

    # Roll status up per fact, worst-wins.  NOT_CHECKED ranks lowest so that a
    # fact touched by a passing rule is recorded as PASSED rather than staying
    # unchecked; anything more serious then overrides it.
    severity_order = {
        ValidationStatus.NOT_CHECKED: 0, ValidationStatus.PASSED: 1,
        ValidationStatus.REQUIRES_REVIEW: 2, ValidationStatus.WARNING: 3,
        ValidationStatus.FAILED: 4,
    }
    status_by_fact: dict[str, ValidationStatus] = {
        f.fact_id: ValidationStatus.NOT_CHECKED for f in actuals}
    for r in results:
        incoming = ValidationStatus(r.status)
        for fid in r.fact_ids:
            cur = status_by_fact.get(fid, ValidationStatus.NOT_CHECKED)
            if severity_order[incoming] > severity_order[ValidationStatus(cur)]:
                status_by_fact[fid] = incoming

    return ValidationReport(results=results, fact_status=status_by_fact)
