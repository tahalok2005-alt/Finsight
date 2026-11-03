"""Derived metric computation.

Ratios and margins that a document does not print can often be computed from
figures it does print.  Everything computed here is kept **outside**
``financial_facts``: a derived metric carries its formula, the fact ids of its
inputs, and -- when the document also reported the same measure -- whether the
two agree.  A calculated number is never presented as a reported one.

Rules that keep the arithmetic honest:

* inputs must share an entity, a period and a currency;
* monetary inputs are compared on their normalised base values, so a figure in
  crore and one in million still divide correctly;
* a formula whose inputs are missing produces nothing -- never a zero, never an
  estimate.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.enums import (
    Currency, DerivationStatus, MagnitudeUnit, PeriodType, ValueType,
)
from app.normalization.numbers import MAGNITUDE_FACTORS
from app.models.schemas import DerivedMetric, FinancialFact

log = get_logger("analysis.derived")


@dataclass(frozen=True, slots=True)
class Recipe:
    """One derivable metric: how to compute it and what it means."""
    key: str
    display: str
    formula: str
    inputs: tuple[str, ...]
    value_type: ValueType
    compute: str          # "ratio_pct" | "ratio" | "difference" | "sum"
    definition: str = ""
    tolerance_pct: float = 1.0
    """How far the calculation may sit from a reported value of the same metric
    before they are called disagreeing, in relative percent."""
    needs_flow_period: bool = True
    """True when the inputs are flows, which only mean anything over a span of
    time.  Such a recipe is skipped for a balance-sheet date: revenue "as at
    30 June" is not a quarter of revenue, and dividing by it invents a rate."""
    annualise_denominator: bool = False
    """True for a stock-over-flow ratio such as Net Debt / EBITDA.  The
    numerator is a balance at a point in time while the denominator accrues
    over the period, so a single quarter's denominator must be annualised or
    the ratio comes out four times too high."""


RECIPES: tuple[Recipe, ...] = (
    # ---- profitability (flows over flows) --------------------------------
    Recipe("ebitda_margin", "EBITDA Margin", "ebitda / revenue * 100",
           ("ebitda", "__revenue__"), ValueType.PERCENTAGE, "ratio_pct",
           "Operating profitability before interest, tax and depreciation."),
    Recipe("net_margin", "Net Margin", "pat / revenue * 100",
           ("pat", "__revenue__"), ValueType.PERCENTAGE, "ratio_pct",
           "Share of revenue retained as profit after tax."),
    Recipe("ebit_margin", "EBIT Margin", "ebit / revenue * 100",
           ("ebit", "__revenue__"), ValueType.PERCENTAGE, "ratio_pct",
           "Profitability after depreciation but before interest and tax."),
    Recipe("effective_tax_rate", "Effective Tax Rate",
           "tax_expense / pbt * 100", ("tax_expense", "pbt"),
           ValueType.PERCENTAGE, "ratio_pct",
           "Tax charge as a share of pre-tax profit."),
    Recipe("interest_coverage", "Interest Coverage", "ebit / finance_costs",
           ("ebit", "finance_costs"), ValueType.RATIO, "ratio",
           "How many times operating profit covers interest.",
           tolerance_pct=2.0),

    # ---- balance sheet (stocks over stocks): no flow period needed --------
    Recipe("net_debt", "Net Debt", "debt - cash_and_equivalents",
           ("__debt__", "__cash__"), ValueType.MONETARY, "difference",
           "Debt net of cash on hand.", needs_flow_period=False),
    Recipe("debt_to_equity", "Debt to Equity", "debt / net_worth",
           ("__debt__", "__equity__"), ValueType.RATIO, "ratio",
           "Balance-sheet leverage.", tolerance_pct=2.0,
           needs_flow_period=False),
    Recipe("current_ratio", "Current Ratio",
           "current_assets / current_liabilities",
           ("current_assets", "current_liabilities"), ValueType.RATIO, "ratio",
           "Short-term liquidity cover.", tolerance_pct=2.0,
           needs_flow_period=False),
    Recipe("working_capital", "Working Capital",
           "current_assets - current_liabilities",
           ("current_assets", "current_liabilities"), ValueType.MONETARY,
           "difference", "Short-term assets net of short-term obligations.",
           needs_flow_period=False),

    # ---- stock over flow: the denominator must be annualised -------------
    Recipe("net_debt_to_ebitda", "Net Debt to EBITDA", "net_debt / ebitda",
           ("net_debt", "ebitda"), ValueType.RATIO, "ratio",
           "Leverage measured against operating cash generation.",
           tolerance_pct=3.0, needs_flow_period=False,
           annualise_denominator=True),
    Recipe("roce", "ROCE", "ebit / (net_worth + debt) * 100",
           ("ebit", "__equity__", "__debt__"), ValueType.PERCENTAGE, "roce",
           "Operating return on all capital employed.",
           tolerance_pct=2.0, needs_flow_period=False,
           annualise_denominator=True),

    # ---- cash flow --------------------------------------------------------
    Recipe("free_cash_flow", "Free Cash Flow",
           "operating_cash_flow - capex", ("operating_cash_flow", "capex"),
           ValueType.MONETARY, "difference",
           "Operating cash generation after capital expenditure."),
)

# Several measures have more than one legitimate name.  Rather than assume one,
# each placeholder lists the taxonomy keys that satisfy it, most specific
# first; whichever the document actually printed is the one used.
ALTERNATES: dict[str, tuple[str, ...]] = {
    "__revenue__": ("revenue_from_operations", "gross_revenue", "revenue",
                    "net_revenue", "sales", "total_income"),
    "__debt__": ("outstanding_debt", "debt"),
    "__cash__": ("cash_and_equivalents", "cash"),
    "__equity__": ("net_worth",),
}


# Revenue has several legitimate names; prefer the most specific the document
# actually used rather than assuming one.

def _scale(fact: FinancialFact) -> float | None:
    """The comparable magnitude of a fact.

    Monetary figures are compared on their base value so that crore and million
    are on the same footing; rates and counts use the printed value.
    """
    if fact.value is None:
        return None
    if fact.value_type == ValueType.MONETARY and fact.base_value is not None:
        return fact.base_value
    return fact.value


def _pick(facts: list[FinancialFact]) -> FinancialFact | None:
    """Choose the best fact when several report the same measure."""
    usable = [f for f in facts if _scale(f) is not None]
    if not usable:
        return None
    return max(usable, key=lambda f: (f.confidence, f.kind == "table_cell"))


def _index(facts: list[FinancialFact]) -> dict[tuple, dict[str, FinancialFact]]:
    """Group facts by (entity, period) and then by canonical metric.

    Facts with no period are excluded: an unattributed number cannot safely
    become the denominator of a period ratio.
    """
    out: dict[tuple, dict[str, list[FinancialFact]]] = {}
    for f in facts:
        if not f.canonical_metric or not f.period:
            continue
        key = (f.entity_id or f.entity or "", f.period)
        out.setdefault(key, {}).setdefault(f.canonical_metric, []).append(f)
    return {k: {m: best for m, fs in v.items()
                if (best := _pick(fs)) is not None}
            for k, v in out.items()}


# How many of each period fit in a year -- used to annualise a flow that is
# being measured against a balance.
_PERIODS_PER_YEAR = {
    PeriodType.QUARTER: 4.0,
    PeriodType.HALF_YEAR: 2.0,
    PeriodType.NINE_MONTH: 4.0 / 3.0,
    PeriodType.FISCAL_YEAR: 1.0,
    PeriodType.CALENDAR_YEAR: 1.0,
    PeriodType.TTM: 1.0,
}


def _resolve(name: str, bucket: dict[str, FinancialFact]) -> FinancialFact | None:
    """Resolve one recipe input, following the alternates for a placeholder."""
    for key in ALTERNATES.get(name, (name,)):
        if key in bucket:
            return bucket[key]
    return None


def _candidates(name: str, bucket: dict[str, FinancialFact],
                ) -> list[FinancialFact]:
    """Every figure in this scope that could satisfy one recipe input.

    "Revenue" is the case that matters.  A filing may print revenue from
    operations, gross revenue (sales and services, before indirect taxes) and
    total income side by side, and its own published margins are computed
    against exactly one of them.  Which one is a fact about the document, not a
    convention to assume.
    """
    return [bucket[key] for key in ALTERNATES.get(name, (name,))
            if key in bucket]


def _period_type(bucket: dict[str, FinancialFact]) -> PeriodType | None:
    for f in bucket.values():
        if f.period_normalized is not None:
            return PeriodType(str(f.period_normalized.period_type))
    return None


def _is_flow_period(period_type: PeriodType | None) -> bool:
    """A flow only means something over a span of time.

    A balance-sheet date is not one: "revenue as at 30 June" is not a quarter
    of revenue, and dividing by it would invent a rate the document never
    stated.
    """
    return period_type in _PERIODS_PER_YEAR if period_type else False


def _currency_of(inputs: list[FinancialFact]) -> Currency | None:
    """A single currency shared by every monetary input, or None."""
    seen = {f.currency for f in inputs
            if f.value_type == ValueType.MONETARY
            and f.currency != Currency.UNKNOWN}
    if len(seen) > 1:
        return None                       # never silently convert currencies
    return next(iter(seen), Currency.UNKNOWN)


def _compute(recipe: Recipe, values: dict[str, float]) -> float | None:
    try:
        if recipe.compute == "ratio_pct":
            num, den = values["_0"], values["_1"]
            return None if den == 0 else num / den * 100.0
        if recipe.compute == "ratio":
            num, den = values["_0"], values["_1"]
            return None if den == 0 else num / den
        if recipe.compute == "difference":
            return values["_0"] - values["_1"]
        if recipe.compute == "sum":
            return sum(values.values())
        if recipe.compute == "roce":
            capital = values["_1"] + values["_2"]
            return None if capital == 0 else values["_0"] / capital * 100.0
    except (KeyError, ZeroDivisionError, TypeError):
        return None
    return None


def _round(value: float, value_type: ValueType) -> float:
    if value_type == ValueType.PERCENTAGE:
        return round(value, 2)
    if value_type == ValueType.RATIO:
        return round(value, 3)
    return round(value, 4)


def compute_derived(document_id: str, facts: list[FinancialFact],
                    ) -> list[DerivedMetric]:
    """Compute every ratio the document's own figures support."""
    index = _index(facts)
    out: list[DerivedMetric] = []

    for (entity_key, period), bucket in sorted(index.items()):
        # net_debt may itself become an input, so run recipes in declared order
        # and let later ones see what earlier ones produced.
        local: dict[str, tuple[float, DerivedMetric]] = {}
        period_type = _period_type(bucket)
        periods_per_year = _PERIODS_PER_YEAR.get(period_type, 1.0)
        for recipe in RECIPES:
            if recipe.needs_flow_period and not _is_flow_period(period_type):
                # e.g. a margin against a balance-sheet date
                continue
            resolved: list[FinancialFact] = []
            values: dict[str, float] = {}
            synthetic: dict[str, DerivedMetric] = {}
            ok = True
            for position, name in enumerate(recipe.inputs):
                fact = _resolve(name, bucket)
                if fact is not None:
                    scale = _scale(fact)
                    if scale is None:
                        ok = False
                        break
                    resolved.append(fact)
                    values[f"_{position}"] = scale
                elif name in local:
                    prior_value, prior_metric = local[name]
                    values[f"_{position}"] = prior_value
                    synthetic[name] = prior_metric
                else:
                    ok = False
                    break
            if not ok:
                continue

            currency = _currency_of(resolved)
            if currency is None:
                log.info("derived.skipped_mixed_currency", metric=recipe.key,
                         period=period, document_id=document_id)
                continue

            # A balance divided by one quarter of a flow comes out four times
            # too high.  Annualise the flow and say so, rather than reporting a
            # ratio that disagrees with the document for a reason nobody can
            # see.
            annualised = False
            formula = recipe.formula
            if (recipe.annualise_denominator and periods_per_year != 1.0
                    and _is_flow_period(period_type)):
                for position in range(1, len(recipe.inputs)):
                    key = f"_{position}"
                    if key in values:
                        values[key] *= periods_per_year
                annualised = True
                formula = (f"{recipe.formula}  [denominator annualised "
                           f"x{periods_per_year:g} from {period_type}]")

            value = _compute(recipe, values)
            if value is None:
                continue

            # A monetary result is computed on normalised base values so that
            # crore and million reconcile.  Present it back in the magnitude the
            # inputs shared, so the figure reads like the document's own, and
            # keep the base value for any calculation that chains off it.
            # This must happen *before* the comparison below: comparing a base
            # figure against a printed one in crore reports a discrepancy of
            # ten million to one that does not exist.
            base_value = value
            unit = MagnitudeUnit.NONE
            if recipe.value_type == ValueType.MONETARY:
                units = {f.unit for f in resolved
                         if f.value_type == ValueType.MONETARY}
                if len(units) == 1:
                    unit = next(iter(units))
                    factor = MAGNITUDE_FACTORS.get(unit, 1.0)
                    if factor:
                        value = value / factor
                else:
                    unit = MagnitudeUnit.UNIT
            value = _round(value, recipe.value_type)

            # The reported counterpart is compared on the same footing: its own
            # magnitude is normalised to the one just chosen.
            reported = bucket.get(recipe.key)
            agrees: bool | None = None
            difference: float | None = None
            if reported is not None and reported.value is not None:
                reported_value = reported.value
                if (recipe.value_type == ValueType.MONETARY
                        and reported.unit != unit):
                    rf = MAGNITUDE_FACTORS.get(reported.unit)
                    tf = MAGNITUDE_FACTORS.get(unit)
                    if rf and tf:
                        reported_value = reported.value * rf / tf
                    else:
                        reported_value = None
                if reported_value is not None:
                    difference = round(value - reported_value, 4)
                    base = abs(reported_value) or 1.0
                    agrees = (abs(difference) / base * 100.0
                              <= recipe.tolerance_pct)

            # When the document also published this ratio and the first-choice
            # denominator does not reproduce it, try the other figures the
            # document printed for the same input.  If one reconciles, that is
            # evidence of the basis management actually used, and it is
            # recorded explicitly rather than left as an unexplained
            # discrepancy.
            basis_note = ""
            if (agrees is False and reported is not None
                    and recipe.compute in ("ratio_pct", "ratio")
                    and len(recipe.inputs) >= 2):
                alt_name = recipe.inputs[1]
                alternatives = [f for f in _candidates(alt_name, bucket)
                                if f is not resolved[1]]
                for alternative in alternatives:
                    alt_scale = _scale(alternative)
                    if not alt_scale:
                        continue
                    trial = dict(values)
                    trial["_1"] = (alt_scale * periods_per_year
                                   if annualised else alt_scale)
                    candidate = _compute(recipe, trial)
                    if candidate is None:
                        continue
                    candidate = _round(candidate, recipe.value_type)
                    delta = abs(candidate - (reported_value or 0.0))
                    base = abs(reported_value or 1.0) or 1.0
                    if delta / base * 100.0 <= recipe.tolerance_pct:
                        basis_note = (
                            f" The document's own figure reconciles against "
                            f"{alternative.metric_display or alternative.metric} "
                            f"rather than "
                            f"{resolved[1].metric_display or resolved[1].metric}, "
                            f"so that is the basis management used; FinSight "
                            f"adopted it for this ratio.")
                        values = trial
                        resolved[1] = alternative
                        value = candidate
                        difference = round(value - reported_value, 4)
                        agrees = True
                        formula = formula.replace(
                            "revenue", alternative.canonical_metric, 1)
                        break

            entity_name = resolved[0].entity if resolved else None
            entity_id = resolved[0].entity_id if resolved else None
            metric_id = stable_id("derived", document_id, entity_key, period,
                                  recipe.key)

            metric = DerivedMetric(
                id=metric_id, document_id=document_id,
                canonical_metric=recipe.key, metric_display=recipe.display,
                entity_id=entity_id, entity_name=entity_name, period=period,
                value=value, base_value=base_value,
                value_type=recipe.value_type, unit=unit,
                currency=(currency if recipe.value_type == ValueType.MONETARY
                          else Currency.UNKNOWN),
                formula=formula,
                inputs={name: f.fact_id for name, f
                        in zip(recipe.inputs, resolved)},
                input_fact_ids=[f.fact_id for f in resolved],
                input_values={f.canonical_metric: _scale(f) for f in resolved},
                derived_inputs=[m.id for m in synthetic.values()],
                reported_fact_id=(reported.fact_id if reported else None),
                reported_value=(reported.value if reported else None),
                agrees_with_reported=agrees, difference=difference,
                derivation=DerivationStatus.CALCULATED,
                confidence=round(min(f.confidence for f in resolved) * 0.95, 3)
                if resolved else 0.5,
                source_pages=sorted({f.source_page for f in resolved
                                     if f.source_page}),
                definition=recipe.definition,
                notes=(("Calculated by FinSight from the document's own "
                        "figures; not a number the document reported."
                        if reported is None else
                        "Calculated and compared against the value the "
                        "document reported for the same measure.")
                       + basis_note
                       + (f" The denominator is a flow measured over one "
                          f"{period_type}, so it was annualised "
                          f"(x{periods_per_year:g}) before dividing a "
                          f"point-in-time balance by it."
                          if annualised else "")),
            )
            out.append(metric)
            # chaining always uses the base value, never the
            # display-scaled one
            local[recipe.key] = (base_value, metric)

    log.info("derived.computed", document_id=document_id, count=len(out))
    return out
