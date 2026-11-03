"""An accounting identity applies only where the statement is built that way.

Found in the Phase 21 corpus, as false alarms on figures that were all read
correctly:

  * "Total tax expense [8(a) to 8(c)]" -- a steel maker's total names three
    components; the third, current tax of earlier years, is no line the
    identity knows, and "current + deferred = total" was reported broken;
  * "Operating expenses (a)+(b)+(c)" -- a bank's third component is the
    operating expense of its insurance business;
  * "(c) Other income" under "Other income (a+b+c)" -- the component was taken
    for the total, and a bank's total income failed to add up;
  * "(1,848)" -- an IFRS statement prints its costs in brackets, and each was
    reported as an implausible negative.

The rule these hold: the engine never forces an equation the statement does
not assert, and never rewrites a value to make one hold.
"""
from __future__ import annotations

from app.models.enums import Currency, MagnitudeUnit, ValueType
from app.models.schemas import FinancialFact
from app.validation.engine import (
    _declared_components, _idx, check_identities, check_sanity,
)


def _fact(fid, metric, key, value, *, period="Q1 FY27", raw=None, component=False):
    return FinancialFact(
        fact_id=fid, document_id="d", metric=metric, canonical_metric=key,
        entity="Standalone", entity_id="e1", value=value,
        raw_value=raw if raw is not None else f"{value:,}",
        value_type=ValueType.MONETARY, currency=Currency.INR,
        unit=MagnitudeUnit.CRORE, period=period, confidence=0.9,
        meta={"component": True} if component else {})


class TestATotalSaysHowItIsBuilt:
    def test_the_declared_count(self):
        assert _declared_components("Total tax expense [8(a) to 8(c)]") == 3
        assert _declared_components("Operating expenses (a)+(b)t(c)") == 3
        assert _declared_components("Other income (a+b+c)") == 3
        assert _declared_components("TOTAL INCOME (1)+(2)") == 2
        assert _declared_components("Operating Profit (3-6)") is None
        assert _declared_components("Total tax expense") is None

    def test_a_three_part_total_is_not_checked_against_two_parts(self):
        facts = [_fact("t", "Total tax expense [8(a) to 8(c)]", "tax_expense", 1284.93),
                 _fact("c", "a) Current tax", "current_tax", 1404.08),
                 _fact("d", "c) Deferred tax", "deferred_tax", 361.10)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "tax_components_sum_to_tax_expense"]
        assert results == []

    def test_an_undeclared_total_is_still_checked(self):
        facts = [_fact("t", "Tax expense", "tax_expense", 1765.18),
                 _fact("c", "Current tax", "current_tax", 1404.08),
                 _fact("d", "Deferred tax", "deferred_tax", 361.10)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "tax_components_sum_to_tax_expense"]
        assert [str(r.status) for r in results] == ["PASSED"]


class TestAComponentIsNotItsTotal:
    def test_total_income_adds_up_with_the_total_not_the_component(self):
        # A bank's statement: it prints interest expended, which is what makes
        # the bank identity apply (Phase 22 -- a conglomerate's stray
        # "interest income" line does not).
        facts = [_fact("ti", "Total income (1+2)", "total_income", 27850.79),
                 _fact("ix", "Interest expended", "interest_expended", 9921.23),
                 _fact("ie", "Interest earned (a+b+c+d)", "interest_earned", 17506.80),
                 _fact("oi", "Other income (a+b+c)", "other_income", 10343.99),
                 _fact("oic", "(c) Other income (Refer Note 4)", "other_income",
                       4522.37, component=True)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "interest_plus_other_is_total_income_bank"]
        assert [str(r.status) for r in results] == ["PASSED"]

    def test_a_component_alone_is_never_taken_for_the_total(self):
        # The total's own row was unreadable; only "(c) Other income" was.
        facts = [_fact("ti", "Total income (1+2)", "total_income", 27850.79),
                 _fact("ie", "Interest earned (a+b+c+d)", "interest_earned", 17506.80),
                 _fact("oic", "(c) Other income (Refer Note 4)", "other_income",
                       4522.37, component=True)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "interest_plus_other_is_total_income_bank"]
        assert results == []


def _row(fid, metric, key, value, row, *, table="t9"):
    f = _fact(fid, metric, key, value, period="Q1 FY27")
    f.meta = {"table_id": table, "row": row}
    return f


class TestAResultStruckAfterFurtherLines:
    """ICICI Bank's consolidated results, page 9, the quarter to June 2026:
    profit before tax 21,373.61, tax 5,097.39, "Less: share of profit of
    minority shareholders" 836.16, then "NET PROFIT FOR THE PERIOD (15)-(16)"
    15,440.06. Every figure was read correctly; the check failed."""

    def _pat_checks(self, facts):
        return [r for r in check_identities("d", _idx(facts))
                if r.rule == "pbt_less_tax_is_pat"]

    def test_a_minority_line_before_the_result_means_it_is_not_pbt_less_tax(self):
        facts = [
            _row("pbt", "PROFIT BEFORE TAX AND MINORITY INTEREST (9)-(10)",
                 "pbt", 21373.61, 20),
            _row("tax", "Tax expense (i)+(k)", "tax_expense", 5097.39, 21),
            _row("nci", "Less: Share of profit/(loss) of minority shareholders",
                 "nci_share_of_profit", 836.16, 25),
            _row("pat", "NET PROFIT FOR THE PERIOD (15)-(16)", "pat",
                 15440.06, 27)]
        assert self._pat_checks(facts) == []

    def test_a_split_printed_after_the_result_leaves_the_identity_standing(self):
        # IFRS: "Profit for the period", then "attributable to
        # non-controlling interests" -- a split of the result, not a step to it.
        facts = [
            _row("pbt", "Profit before tax", "pbt", 1765.0, 10),
            _row("tax", "Income tax expense", "tax_expense", 465.0, 11),
            _row("pat", "Profit for the period", "pat", 1300.0, 12),
            _row("nci", "Non-controlling interests", "nci_share_of_profit",
                 40.0, 15)]
        assert [str(r.status) for r in self._pat_checks(facts)] == ["PASSED"]

    def test_a_nil_line_between_moves_nothing(self):
        # Indian Bank, Q1 FY27: "Extraordinary items (net of tax expense) 0.00"
        facts = [
            _row("pbt", "Profit from ordinary activities before tax (7-8-9)",
                 "pbt", 4361.57, 16),
            _row("tax", "Provision for Taxes", "tax_expense", 1088.48, 17),
            _row("xo", "Extraordinary items (net of tax expense)",
                 "extraordinary_items", 0.0, 19),
            _row("pat", "Net Profit for the period (12-13)", "pat", 3273.09, 20)]
        assert [str(r.status) for r in self._pat_checks(facts)] == ["PASSED"]

    def test_the_printing_before_the_adjustments_is_the_one_checked(self):
        # Kotak consolidated, Q3 FY25: profit after tax before minority
        # interest 4,639.63, then associates 61.39, then profit after tax
        # 4,701.02. The first is PBT less tax; the second is not.
        after = _row("pat2", "Profit after tax (14-15+16)", "pat", 4701.02, 31)
        after.confidence = 0.95
        facts = [
            _row("pbt", "Profit from ordinary activities before tax (7-8+9)",
                 "pbt", 6180.08, 24),
            _row("tax", "Tax expense", "tax_expense", 1540.45, 25),
            _row("pat1", "Net Profit after tax before Minority Interest (12-13)",
                 "pat", 4639.63, 28),
            _row("ass", "Add: Share in Profit/(Loss) of associates",
                 "share_of_associates", 61.39, 30),
            after]
        results = self._pat_checks(facts)
        assert [str(r.status) for r in results] == ["PASSED"]
        assert results[0].reported_value == 4639.63

    def test_a_minority_line_in_another_table_decides_nothing(self):
        facts = [
            _row("pbt", "Profit before tax", "pbt", 1765.0, 10),
            _row("tax", "Tax expense", "tax_expense", 465.0, 11),
            _row("pat", "Profit after tax", "pat", 1300.0, 14),
            _row("nci", "Share of minority", "nci_share_of_profit", 40.0, 12,
                 table="t_other")]
        assert [str(r.status) for r in self._pat_checks(facts)] == ["PASSED"]


class TestACostPrintedInBrackets:
    def test_is_the_statements_convention(self):
        f = _fact("c", "Cost of revenues", "cost_of_revenue", -1848.0, raw="(1,848)")
        assert not [r for r in check_sanity("d", [f]) if r.rule == "unexpected_negative"]

    def test_a_negative_revenue_is_still_flagged(self):
        f = _fact("r", "Revenue from operations", "revenue_from_operations",
                  -1848.0, raw="(1,848)")
        assert [r for r in check_sanity("d", [f]) if r.rule == "unexpected_negative"]

