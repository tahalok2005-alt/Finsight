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

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from app.config.settings import settings
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.models.enums import (
    Currency, MagnitudeUnit, PeriodBasis, StatementNature, ValidationSeverity,
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


def _period_scope(f: FinancialFact) -> str | None:
    """The period a fact is checked within.

    Its period, where it has one. A table figure without one is scoped to its
    COLUMN: two figures that both failed period resolution are not known to
    share a period in general, but two figures in one column of one table are
    -- whatever that period is. An image cash-flow statement whose heads could
    not be read still adds up down each column, and a misread net investing
    figure (-168 for -1,168) went unchecked because its column had no date.
    Figures that are not table cells, and have no period, are left out.
    """
    if f.period:
        return f.period
    meta = f.meta or {}
    if meta.get("table_id") is not None and meta.get("col") is not None:
        return f"column {meta['table_id']}:{meta['col']}"
    return None


def _idx(facts: Sequence[FinancialFact]) -> dict[Key, list[FinancialFact]]:
    """Bucket facts by (entity, period scope, metric) for cross-fact rules."""
    out: dict[Key, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.nature != StatementNature.ACTUAL and f.nature != "actual":
            continue
        scope = _period_scope(f)
        if scope is None:
            continue
        out[(f.entity_id or f.entity or "", scope,
             f.canonical_metric)].append(f)
    return out


#: Lines only a bank's results print, in the prescribed bank format.
_BANK_FORMAT = frozenset({"interest_expended", "provisions_and_contingencies",
                          "total_expenditure", "provisions_for_npa"})
#: Lines any lender prints -- a bank's slide deck or an NBFC's statement.
_LENDER_LINES = frozenset({"interest_earned", "net_interest_income", "net_total_income",
                           "non_interest_income", "pre_provision_operating_profit",
                           "impairment_on_financial_instruments", "aum", "credit_cost",
                           *_BANK_FORMAT})


def statement_categories(facts: Sequence[FinancialFact]) -> dict[str, str]:
    """Which kind of statement each table is: BANKING, NBFC or GENERAL_CORPORATE.

    Read from the lines the table itself prints, never from who filed it. A
    conglomerate's results mention interest income somewhere; they are not a
    bank's, and the bank's total-income identity fired on them four times.
    Technology, manufacturing and other corporates share one set of identities,
    so they are one category here.
    """
    keys: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        t = (f.meta or {}).get("table_id")
        if t is not None:
            keys[t].add(f.canonical_metric)
    return {t: category_of(ks) for t, ks in keys.items()}


def category_of(keys: set[str] | frozenset[str]) -> str:
    """The statement a set of printed lines belongs to (see
    ``statement_categories``)."""
    lender = keys & _LENDER_LINES
    if keys & _BANK_FORMAT and len(lender) >= 2:
        return "BANKING"
    if len(lender) >= 2:
        return "NBFC"
    return "GENERAL_CORPORATE"


def _pick(bucket: dict[Key, list[FinancialFact]], entity: str, period: str,
          metric: str) -> FinancialFact | None:
    group = bucket.get((entity, period, metric))
    if not group:
        return None
    # A component printed under its total with the total's own name -- "(c)
    # Other income" under "Other income (a+b+c)" -- is not the total. Taken
    # for it, a bank's total income failed to add up against a statement
    # that adds up exactly.
    totals = [f for f in group if not (f.meta or {}).get("component")]
    return max(totals or group, key=lambda f: f.confidence)


#: The components a total's own label declares: "(a)+(b)+(c)", "(a+b+c+d)",
#: "[8(a) to 8(c)]", "(1)+(2)", "(i)+(ii)" -- OCR sometimes reads "+" as "t".
_FORMULA_RANGE = re.compile(
    r"\d*\(\s*(?P<a>[a-z])\s*\)\s*to\s*\d*\(\s*(?P<b>[a-z])\s*\)", re.I)
_FORMULA_LETTERS = re.compile(r"\(\s*([a-z](?:\s*\+\s*[a-z])+)\s*\)", re.I)
_FORMULA_TERMS = re.compile(
    r"(?:\(\s*(?:[a-z]|[ivx]{1,4}|\d{1,2})\s*\)\s*[+t]\s*)+"
    r"\(\s*(?:[a-z]|[ivx]{1,4}|\d{1,2})\s*\)", re.I)


def _declared_components(label: str | None) -> int | None:
    """How many terms a total's label says it sums, or None if it says none.

    A difference ("(3-6)") declares no sum and returns None."""
    t = label or ""
    m = _FORMULA_RANGE.search(t)
    if m:
        return abs(ord(m.group("b").lower()) - ord(m.group("a").lower())) + 1
    m = _FORMULA_LETTERS.search(t)
    if m:
        return len(re.findall(r"[a-z]", m.group(1), re.I))
    m = _FORMULA_TERMS.search(t)
    if m:
        return len(re.findall(r"\(", m.group(0)))
    return None


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
            # Which revenue a margin is struck on is the filer's choice -- a
            # refiner's EBITDA margin is on gross revenue, 54,067 / 340,257 =
            # 15.9% as printed, and on net revenue it is 17.3%. The margin is
            # consistent if it agrees with ANY revenue line the statement
            # prints; it is reported against the closest when it agrees with
            # none. (Testing only the first found raised nine false warnings.)
            options = []
            for dk in den_keys:
                cand = _pick(bucket, entity, period, dk)
                if cand is None or cand.value in (None, 0):
                    continue
                value = (cand.value if _same_scale(numerator, cand)
                         else _rescale(cand, numerator.unit))
                if value:
                    options.append((cand, value))
            if not options:
                continue
            key = (entity, period, margin_key)
            if key in seen:
                continue
            seen.add(key)
            denominator, den_value = min(
                options, key=lambda o: abs(numerator.value / o[1] * 100.0 - margin.value))
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


def _identity_sources(bucket: dict[Key, list[FinancialFact]], entity: str,
                      period: str, keys: list[str],
                      accept: Callable[[dict[str, FinancialFact]], bool] | None = None,
                      ) -> dict[str, FinancialFact] | None:
    """The facts an identity is evaluated on: all from one table where one
    table holds every term, or else -- across tables -- only when each term
    has a single, uncontested value. None when neither holds.

    Picking each term separately from the whole document mixed statements: a
    results release prints profit before and after tax EXCLUDING an
    exceptional gain on its first page and INCLUDING it in the statement on
    page 21, and page 1's profit was tested against page 21's tax. It
    "failed", lost its confidence, and a figure the page states correctly
    was reported as the one in doubt.
    """
    candidates: dict[str, list[FinancialFact]] = {}
    for key in keys:
        group = [f for f in bucket.get((entity, period, key), [])
                 if f.value is not None]
        # A component of a total of the same name is never that total, so
        # where only the component was read the identity is not evaluated.
        candidates[key] = [f for f in group if not (f.meta or {}).get("component")]
        if not candidates[key]:
            return None

    def table_of(f: FinancialFact) -> str | None:
        return (f.meta or {}).get("table_id")

    target = keys[0]
    # ``accept`` may refuse a set of terms the statement does not build its
    # result from; another printing of the result -- "profit after tax
    # before minority interest" above "profit after tax" -- may still be.
    for anchor in sorted(candidates[target], key=lambda f: -f.confidence):
        table = table_of(anchor)
        if table is None:
            continue
        picks = {target: anchor}
        for key in keys[1:]:
            same = [f for f in candidates[key] if table_of(f) == table]
            if not same:
                break
            picks[key] = max(same, key=lambda f: f.confidence)
        else:
            if accept is None or accept(picks):
                return picks
    # Across tables: every term must say one thing wherever it is printed.
    if all(len({round(f.value, 2) for f in candidates[k]}) == 1 for k in keys):
        picks = {k: max(candidates[k], key=lambda f: f.confidence) for k in keys}
        if accept is None or accept(picks):
            return picks
    return None


#: Lines a statement may print between an identity's terms and its result,
#: each moving the result by something the identity does not name. A bank's
#: consolidated results strike "Net profit for the period" after "Less: share
#: of profit of minority shareholders" and "Add: share of profit in
#: associates"; profit before tax less tax is not that figure, and the check
#: failed on a statement that adds up.
_ADJUSTED_BETWEEN: dict[str, tuple[str, ...]] = {
    "pbt_less_tax_is_pat": ("nci_share_of_profit", "share_of_associates",
                            "extraordinary_items", "pat_discontinued"),
}


def _adjusted_between(bucket: dict[Key, list[FinancialFact]], entity: str,
                      period: str, chosen: dict[str, FinancialFact],
                      target: str, adjusting: tuple[str, ...]) -> bool:
    """Whether a line adjusting the result is printed between the identity's
    terms and its result, in the one table all of them come from.

    Where the adjustment is printed after the result -- "Profit for the
    period", then "attributable to non-controlling interests" -- it splits the
    result rather than building it, and the identity stands."""
    if not adjusting:
        return False

    def position(f: FinancialFact) -> tuple[str | None, int | None]:
        meta = f.meta or {}
        return meta.get("table_id"), meta.get("row")

    table, target_row = position(chosen[target])
    if table is None or target_row is None:
        return False
    term_rows: list[int] = []
    for key, f in chosen.items():
        if key == target:
            continue
        t, r = position(f)
        if t != table or r is None:
            return False
        term_rows.append(r)
    if not term_rows:
        return False
    nearest = min(term_rows, key=lambda r: abs(r - target_row))
    lo, hi = sorted((nearest, target_row))
    for key in adjusting:
        for f in bucket.get((entity, period, key), []):
            t, r = position(f)
            # a nil line ("Extraordinary items 0.00") moves nothing
            if t == table and r is not None and lo < r < hi and f.value:
                return True
    return False


#: The statements an identity is written for. Identities not listed hold for
#: every statement that prints all their terms.
_APPLIES_TO: dict[str, frozenset[str]] = {
    "interest_plus_other_is_total_income_bank": frozenset({"BANKING"}),
    "bank_expenditure_components": frozenset({"BANKING"}),
    "income_less_expenditure_is_operating_profit": frozenset({"BANKING"}),
    "lender_nii": frozenset({"BANKING", "NBFC"}),
    "lender_nii_finance_costs": frozenset({"BANKING", "NBFC"}),
    "lender_net_total_income": frozenset({"BANKING", "NBFC"}),
    "lender_ppop": frozenset({"BANKING", "NBFC"}),
    "revenue_plus_other_is_total_income": frozenset({"GENERAL_CORPORATE", "NBFC"}),
}


#: A total whose label states which numbered lines it adds: "Total
#: comprehensive income (9+10)", "XI Total comprehensive income (IX+X)".
_LINE_FORMULA = re.compile(
    r"\(\s*((?:\d{1,2}|[ivxl]{1,5})(?:\s*[+\-]\s*(?:\d{1,2}|[ivxl]{1,5}))+)\s*\)", re.I)
#: A line's own number, printed before its name: "7 Net profit after tax".
_OWN_LINE = re.compile(r"^\s*(\d{1,2}|[ivxl]{1,5})[\s.)]+(?=[A-Za-z(])", re.I)


def _declared_lines(label: str) -> set[str] | None:
    m = _LINE_FORMULA.search(label or "")
    if not m:
        return None
    return {t.lower() for t in re.split(r"\s*[+\-]\s*", m.group(1).strip())}


def _own_line(label: str) -> str | None:
    m = _OWN_LINE.match(label or "")
    return m.group(1).lower() if m else None


def _not_the_declared_lines(target: FinancialFact, operands: list[FinancialFact]) -> bool:
    """Whether the statement says its total adds other lines than these.

    "Total comprehensive income (9+10)" is line 9 -- profit after the share of
    joint ventures -- plus line 10; the profit the taxonomy knows is line 7,
    "Net profit after tax (5-6)". Testing the identity on line 7 reported a
    correct statement as failing by exactly the joint ventures' share (Phase
    22). Where the total names its lines and an operand prints a number of
    the same kind that is not among them, the identity is not this
    statement's arithmetic, and is not run.
    """
    lines = _declared_lines(target.metric or "")
    if not lines:
        return False
    numeric = all(t.isdigit() for t in lines)
    for f in operands:
        own = _own_line(f.metric or "")
        if own is None or own.isdigit() != numeric:
            continue
        if own not in lines:
            return True
    return False


def check_identities(document_id: str, bucket: dict[Key, list[FinancialFact]],
                     ) -> list[ValidationResult]:
    """Check accounting identities that must hold within one period."""
    # (rule, target, plus, minus, description, blocked_by)
    #
    # ``blocked_by`` names metrics whose presence makes the identity
    # inapplicable to this statement. A bank's total income is interest earned
    # plus other income, not revenue from operations plus other income, so the
    # ordinary corporate rule fired on a bank's results and reported eight
    # failures against a statement that adds up perfectly. An identity that is
    # wrong for a whole class of filer has to say so; forcing the equality
    # would be the accounting engine inventing a defect.
    identities: list[tuple[str, str, list[str], list[str], str, list[str]]] = [
        # (rule, result_metric, plus, minus, human description)
        # ---- income statement
        ("pbt_less_tax_is_pat", "pat", ["pbt"], ["tax_expense"],
         "Profit Before Tax - Tax Expense = Profit After Tax", []),
        ("revenue_plus_other_is_total_income", "total_income",
         ["revenue_from_operations", "other_income"], [],
         "Revenue from Operations + Other Income = Total Income",
         ["interest_earned"]),
        ("income_less_expenses_is_pbt", "pbt",
         ["total_income"], ["total_expenses"],
         "Total Income - Total Expenses = Profit Before Tax",
         ["pbt_before_associates", "total_expenditure"]),
        ("ebitda_less_dep_is_ebit", "ebit", ["ebitda"], ["depreciation"],
         "EBITDA - Depreciation = EBIT", []),
        # Four rules below carried metric keys the taxonomy does not define --
        # "finance_cost" for "finance_costs", "total_equity" for "net_worth",
        # and two cash-flow names that were never keys at all. A rule naming a
        # key no fact can carry never matches anything, so it never ran and
        # never failed; it simply produced nothing, which is indistinguishable
        # from a document that does not support the check. Named correctly,
        # they run.
        ("ebit_less_finance_is_pbt", "pbt", ["ebit"], ["finance_costs"],
         "EBIT - Finance Costs = Profit Before Tax", []),
        ("tax_components_sum_to_tax_expense", "tax_expense",
         ["current_tax", "deferred_tax"], [],
         "Current Tax + Deferred Tax = Total Tax Expense", []),
        ("profit_plus_oci_is_comprehensive_income",
         "total_comprehensive_income",
         ["pat", "other_comprehensive_income"], [],
         "Profit for the Period + Other Comprehensive Income = "
         "Total Comprehensive Income", []),
        ("pbt_before_associates_plus_share_is_pbt", "pbt",
         ["pbt_before_associates", "share_of_associates"], [],
         "Profit Before Associates and Tax + Share of Associates = "
         "Profit Before Tax", []),
        # ---- a bank's income statement, which is shaped differently
        ("interest_plus_other_is_total_income_bank", "total_income",
         ["interest_earned", "other_income"], [],
         "Interest Earned + Other Income = Total Income", []),
        # Named "finance_costs" until Phase 22 -- a key a bank's "Interest
        # expended" is never filed under -- so it never ran on a bank.
        ("bank_expenditure_components", "total_expenditure",
         ["interest_expended", "operating_expenses"], [],
         "Interest Expended + Operating Expenses = Total Expenditure", []),
        ("operating_expense_components", "operating_expenses",
         ["employee_cost", "other_operating_expenses"], [],
         "Employee Cost + Other Operating Expenses = Operating Expenses", []),
        ("income_less_expenditure_is_operating_profit",
         "pre_provision_operating_profit",
         ["total_income"], ["total_expenditure"],
         "Total Income - Total Expenditure = Operating Profit before "
         "provisions", []),
        # ---- balance sheet
        ("assets_equal_liabilities_plus_equity", "total_assets",
         ["total_liabilities", "net_worth"], [],
         "Total Liabilities + Total Equity = Total Assets", []),
        ("equity_components", "net_worth", ["share_capital", "other_equity"],
         [],
         "Paid-up Share Capital + Other Equity = Total Equity", []),
        ("debt_less_cash_is_net_debt", "net_debt", ["outstanding_debt"],
         ["cash_and_equivalents"],
         "Outstanding Debt - Cash & Equivalents = Net Debt", []),
        ("total_debt_less_cash_is_net_debt", "net_debt", ["debt"],
         ["cash_and_equivalents"],
         "Total Debt - Cash & Equivalents = Net Debt", []),
        # ---- a lender's summary (a bank's deck, an NBFC's statement)
        ("lender_nii", "net_interest_income", ["interest_earned"],
         ["interest_expended"], "Interest Income - Interest Expended = NII",
         ["finance_costs"]),
        ("lender_nii_finance_costs", "net_interest_income", ["interest_earned"],
         ["finance_costs"], "Interest Income - Interest Expenses = NII",
         ["interest_expended"]),
        ("lender_net_total_income", "net_total_income",
         ["net_interest_income", "non_interest_income"], [],
         "NII + Non-Interest Income = Net Total (Operating) Income", []),
        ("lender_ppop", "pre_provision_operating_profit", ["net_total_income"],
         ["operating_expenses"],
         "Operating Income - Operating Expenses = Operating Profit before "
         "provisions", []),
        # ---- cash flow. The target was "cash" -- a balance -- until Phase
        # 22, so the rule never ran on a cash-flow statement.
        ("cash_flow_movement", "net_change_in_cash",
         ["operating_cash_flow", "investing_cash_flow", "financing_cash_flow"],
         [],
         "Operating + Investing + Financing cash flow = Net Change in Cash", []),
        # The same, where the statement puts the effect of exchange rates
        # above its net change rather than below it. Which layout a statement
        # uses decides which of the two it obeys; see `_one_layout`.
        ("cash_flow_movement_with_fx", "net_change_in_cash",
         ["operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
          "fx_effect_on_cash"], [],
         "Operating + Investing + Financing + FX effect = Net Change in Cash", []),
        ("cash_roll_forward", "cash_at_end",
         ["cash_at_beginning", "net_change_in_cash", "fx_effect_on_cash"], [],
         "Opening Cash + Net Change + FX Effect = Closing Cash", []),
        ("cash_roll_forward_no_fx", "cash_at_end",
         ["cash_at_beginning", "net_change_in_cash"], [],
         "Opening Cash + Net Change = Closing Cash", ["fx_effect_on_cash"]),
    ]
    out: list[ValidationResult] = []
    tol_rel = settings.total_tolerance_rel
    categories = statement_categories([f for fs in bucket.values() for f in fs])
    # One check per (entity, period) -- iterating the full bucket keys would run
    # each identity once per metric present and emit N identical results.
    scopes = sorted({(e, p) for (e, p, _m) in bucket})
    for entity, period in scopes:
        for rule, target, plus, minus, desc, blocked_by in identities:
            if any(_pick(bucket, entity, period, key) is not None
                   for key in blocked_by):
                # This statement reports a line that makes the identity
                # inapplicable to it. Silence is the right answer.
                continue
            adjusting = _ADJUSTED_BETWEEN.get(rule, ())
            chosen = _identity_sources(
                bucket, entity, period, [target, *plus, *minus],
                accept=lambda picks, t=target, a=adjusting: not _adjusted_between(
                    bucket, entity, period, picks, t, a))
            if chosen is None:
                continue
            res = chosen[target]
            # Which statement the result is printed in decides which identities
            # it obeys (Phase 22, section 11).
            table = (res.meta or {}).get("table_id")
            category = (categories[table] if table in categories else category_of(
                {m for (e, p, m) in bucket if e == entity and p == period}))
            applies = _APPLIES_TO.get(rule)
            if applies is not None and category not in applies:
                continue
            # The total's own label may say how it is built. "Total tax
            # expense [8(a) to 8(c)]" has three components -- the third,
            # "current tax in relation to earlier years", is not a line this
            # identity knows -- and two of them cannot be expected to add up
            # to it. An identity the statement contradicts by construction is
            # not applied: forcing it would report a defect the statement does
            # not have.
            declared = _declared_components(res.metric)
            if (declared is not None and not minus
                    and declared != len(plus)):
                continue
            if _not_the_declared_lines(res, [chosen[k] for k in [*plus, *minus]]):
                continue
            terms: list[FinancialFact] = []
            total = 0.0
            ok = True
            for key, sign in [(k, 1) for k in plus] + [(k, -1) for k in minus]:
                f = chosen[key]
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
    return _one_layout(out)


#: Identities that are two layouts of one statement: the one the statement
#: obeys is its arithmetic, and the other says nothing about it.
_LAYOUT_PAIRS = (("cash_flow_movement", "cash_flow_movement_with_fx"),)


def _one_layout(results: list[ValidationResult]) -> list[ValidationResult]:
    """Keep, for each scope, the layout the statement follows.

    A Japanese IFRS statement prints the effect of exchange rates before its
    net increase in cash; testing it without that line reported a correct
    statement as broken (Phase 22). Where either layout holds, the other is
    dropped; where neither does, only the plain one is reported.
    """
    drop: set[int] = set()
    for plain, with_fx in _LAYOUT_PAIRS:
        by_scope: dict[tuple, dict[str, int]] = defaultdict(dict)
        for i, r in enumerate(results):
            if r.rule in (plain, with_fx):
                d = r.detail or {}
                by_scope[(d.get("entity"), d.get("period"))][r.rule] = i
        for pair in by_scope.values():
            passed = [rule for rule, i in pair.items()
                      if results[i].status == ValidationStatus.PASSED]
            if passed:
                drop.update(i for rule, i in pair.items() if rule not in passed[:1])
            elif with_fx in pair and plain in pair:
                drop.add(pair[with_fx])
            elif with_fx in pair:
                drop.add(pair[with_fx])
    return [r for i, r in enumerate(results) if i not in drop]


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
        # Only business SEGMENTS are meant to add up to the group; the figures
        # of subsidiaries do not (a conglomerate's listed subsidiaries sum to
        # more than its consolidated profit, by minority interests and
        # intra-group eliminations), and eleven warnings said so.
        segments = [f for f in group
                    if f is not total_fact
                    and str(f.entity_type) == "segment"
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
            # Segment revenue is commonly reported gross of inter-segment sales
            # and the eliminations line is not always read, so an excess proves
            # nothing about any figure: not checked, and no figure is flagged.
            status = ValidationStatus.NOT_CHECKED
            msg = (f"Segment values for {metric} {period} sum to {seg_sum:,.0f}, "
                   f"above the consolidated total {total_fact.value:,.0f} by "
                   f"{diff_rel * 100:.1f}%; inter-segment eliminations were not "
                   f"read, so the difference is not judged.")
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
        # An expense printed in brackets is the statement's sign convention,
        # not a misread: IFRS statements show costs as "(1,848)". Only a
        # measure that is not a cost is suspicious when negative.
        bracketed_cost = (definition is not None
                          and str(definition.category) == "costs"
                          and "(" in (f.raw_value or ""))
        # So is an outflow in a cash-flow statement: "Purchase of property,
        # plant and equipment (44,049.78)" is capital expenditure printed as
        # the cash it used, and was reported as a suspicious negative (Phase
        # 22). The measure is positive; the statement's sign is its direction.
        if (definition is not None and str(definition.statement) == "cash_flow"
                and ("(" in (f.raw_value or "") or (f.raw_value or "").lstrip().startswith(("-", "−")))):
            bracketed_cost = True
        if (definition and definition.sign == "positive" and f.value < 0
                and not bracketed_cost):
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
    # A figure whose column was a segment, a geography or a unit declaration
    # was never offered a period. Counting those as a coverage failure reports
    # an ordinary segment table as a problem.
    missing = [f for f in facts
               if not f.period and f.value is not None
               and (f.meta or {}).get("period_absence")
               != str(PeriodBasis.NOT_APPLICABLE)]
    if missing:
        # Deliberately an observation about the document, not a verdict on the
        # figures, and deliberately *not* attached to them.
        #
        # Attaching the facts marked every one of them REQUIRES_REVIEW, because
        # any validation touching a fact raises its review flag. On a results
        # presentation whose tables are laid out by segment that meant 35 of 38
        # figures carried a warning -- which says the values are doubtful when
        # what is actually missing is a column header. The gap is reported in
        # the period column, where it is true.
        out.append(_result(
            document_id, "facts_without_period", "coverage",
            ValidationStatus.WARNING,
            f"{len(missing)} extracted value(s) could not be attributed to a "
            f"reporting period; their column header carried no period label.",
            facts=[],
            detail={"count": len(missing),
                    "metrics": sorted({f.canonical_metric for f in missing})[:20],
                    "pages": sorted({f.source_page for f in missing})[:20]}))
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
