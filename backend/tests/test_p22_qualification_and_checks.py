"""Phase 22: qualified facts, and accounting checks that know their statement.

A results release printed last year's quarter "excluding" an 8,924 crore gain
on its first page and the statutory figure in its statements: one measure and
period, two true figures. They were one fact in conflict with itself, and a
benchmark picked between them on a tie of confidence scores. The same release
drew four failures of a BANK identity -- it is a conglomerate -- and nine
margin warnings struck on the wrong revenue line.
"""
from __future__ import annotations

from app.analysis.reconcile import classify
from app.extraction.qualification import (
    from_column_head, qualify_from_footnotes, same_basis,
)
from app.models.enums import (
    Currency, DuplicateResolution, EntityType, MagnitudeUnit, QualificationType,
    StatementNature, ValidationStatus, ValueType,
)
from app.models.schemas import FinancialFact, NormalizedPeriod, Qualification
from app.normalization.periods import FiscalCalendar
from app.validation.engine import (
    _idx, category_of, check_identities, check_margins, statement_categories,
)

MARCH = FiscalCalendar(year_end_month=3)


def _f(fid, key, value, *, period="Q1 FY26", table="t1", col=4, row=0, page=1,
       label=None, vt=ValueType.MONETARY, unit=MagnitudeUnit.CRORE, q=None):
    return FinancialFact(
        fact_id=fid, document_id="d", metric=label or key, canonical_metric=key,
        entity="Consolidated", entity_id="e", entity_type=EntityType.CONSOLIDATED,
        value=value, raw_value=str(value), value_type=vt, currency=Currency.INR,
        unit=unit, period=period, source_page=page, confidence=0.9,
        nature=StatementNature.ACTUAL, qualification=q,
        period_normalized=(NormalizedPeriod(label=period, raw_text=period,
                                            period_type="quarter",
                                            end_date="2025-06-30") if period else None),
        meta={"table_id": table, "col": col, "row": row})


# ------------------------------------------------------------- qualification
class TestAColumnHeadStatesABasis:
    def test_the_words(self):
        cases = {"Reported $million": QualificationType.REPORTED,
                 "Adjusted¹ $million": QualificationType.ADJUSTED,
                 "Restated Q1 FY26": QualificationType.RESTATED,
                 "Continuing operations": QualificationType.CONTINUING_OPERATIONS}
        for head, kind in cases.items():
            assert from_column_head(head).qualification_type == kind, head

    def test_a_date_or_audit_status_is_not_a_basis(self):
        assert from_column_head("Quarter ended 30.06.2026 Audited") is None


class TestAFootnoteStatesABasis:
    """RIL, page 1, column 1Q FY26: EBITDA 49,100, depreciation 13,842,
    finance costs 7,036, PBT 28,222, tax 6,465, PAT 21,757, share of
    associates 102, PAT incl. associates 21,859, gross revenue 273,252,
    EBITDA margin 18.0. The statement on page 25 prints PBT 37,146."""

    def _page(self):
        return [_f("rev", "gross_revenue", 273252, row=3), _f("e", "ebitda", 49100, row=4),
                _f("m", "ebitda_margin", 18.0, row=5, vt=ValueType.PERCENTAGE,
                   unit=MagnitudeUnit.PERCENT),
                _f("d", "depreciation", 13842, row=6), _f("fc", "finance_costs", 7036, row=7),
                _f("pbt", "pbt", 28222, row=8), _f("tax", "tax_expense", 6465, row=9),
                _f("pat", "pat", 21757, row=10), _f("sa", "share_of_associates", 102, row=11),
                _f("pa", "pat_after_associates", 21859, row=12)]

    NOTE = ("1Q FY26 EB/TDA excludes 8,924 crore being proceeds of profit from sale of "
            "listed investments for comparison")

    def test_the_named_measure_and_what_the_page_computes_from_it(self):
        facts = self._page()
        qualify_from_footnotes(facts, {1: [self.NOTE]}, MARCH)
        q = {f.fact_id: f.qualification for f in facts}
        for fid in ("e", "pbt", "pat", "pa", "m"):
            assert q[fid] is not None, fid
            assert q[fid].qualification_type == QualificationType.EXCLUDING
        # the lines the gain does not touch keep no qualification
        for fid in ("d", "fc", "tax", "rev", "sa"):
            assert q[fid] is None, fid

    def test_nothing_is_carried_where_the_arithmetic_does_not_hold(self):
        facts = self._page()
        facts[5].value = 37146            # PBT printed including the gain
        qualify_from_footnotes(facts, {1: [self.NOTE]}, MARCH)
        assert facts[5].qualification is None

    def test_a_degraded_scan_without_spaces(self):
        facts = self._page()
        qualify_from_footnotes(facts, {1: [
            "1QFY26EBITDAexcludes8.924crorebeingproceedsofprofit fromsaleof listed"]}, MARCH)
        assert facts[1].qualification is not None

    def test_a_row_marker_note(self):
        capex = [_f("c1", "capex", 38682, period="Q1 FY27", label="Capital Expenditure#"),
                 _f("c2", "capex", 29875, label="Capital Expenditure#")]
        qualify_from_footnotes(capex, {1: ["# Excluding amount incurred towards spectrum"]},
                               MARCH)
        assert all(c.qualification.qualification_type == QualificationType.EXCLUDING
                   for c in capex)


class TestTwoBasesAreTwoFacts:
    def _q(self):
        return Qualification(qualification_type=QualificationType.EXCLUDING,
                             qualification_label="excludes 8,924 crore",
                             qualification_source="footnote")

    def test_not_a_conflict(self):
        page1 = _f("p1", "pbt", 28222, q=self._q())
        page25 = _f("p25", "pbt", 37146, table="t25", page=25)
        assert classify(page1, page25) == DuplicateResolution.DIFFERENT_QUALIFICATION

    def test_even_where_the_figures_agree(self):
        adj = _f("a", "pbt", 100, q=Qualification(
            qualification_type=QualificationType.ADJUSTED, qualification_label="Adjusted",
            qualification_source="column_head"))
        rep = _f("r", "pbt", 100)
        assert not same_basis(adj, rep)
        assert classify(adj, rep) == DuplicateResolution.DIFFERENT_QUALIFICATION

    def test_the_same_basis_still_conflicts(self):
        a = _f("a", "pbt", 28222)
        b = _f("b", "pbt", 37146, table="t25", page=25)
        assert classify(a, b) == DuplicateResolution.GENUINE_CONFLICT


# ------------------------------------------------------------- statements
class TestWhichStatementAnIdentityIsFor:
    def test_categories_come_from_the_lines_printed(self):
        assert category_of({"interest_earned", "interest_expended", "total_income"}) == "BANKING"
        assert category_of({"interest_earned", "net_interest_income",
                            "impairment_on_financial_instruments"}) == "NBFC"
        assert category_of({"interest_earned", "revenue", "total_income"}) == "GENERAL_CORPORATE"

    def test_a_conglomerate_is_not_held_to_a_banks_identity(self):
        # RIL: an "interest income" line beside total income is not a bank's
        # statement; the bank identity failed on it four times.
        facts = [_f("ti", "total_income", 547779, period="FY26"),
                 _f("ie", "interest_earned", 20000, period="FY26"),
                 _f("oi", "other_income", 12848, period="FY26")]
        rules = {r.rule for r in check_identities("d", _idx(facts))}
        assert "interest_plus_other_is_total_income_bank" not in rules

    def test_a_banks_statement_is(self):
        facts = [_f("ti", "total_income", 27850.79), _f("ix", "interest_expended", 9921.23),
                 _f("ie", "interest_earned", 17506.80), _f("oi", "other_income", 10343.99)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "interest_plus_other_is_total_income_bank"]
        assert [str(r.status) for r in results] == ["PASSED"]

    def test_a_lenders_summary_adds_up(self):
        # SBI's deck, Q1 FY27: 1,27,896 - 80,904 = 46,992; + 15,923 = 62,915;
        # - 29,386 = 33,529.
        facts = [_f("ii", "interest_earned", 127896), _f("ix", "interest_expended", 80904),
                 _f("nii", "net_interest_income", 46992),
                 _f("ni", "non_interest_income", 15923),
                 _f("nti", "net_total_income", 62915),
                 _f("ox", "operating_expenses", 29386),
                 _f("pp", "pre_provision_operating_profit", 33529)]
        results = {r.rule: str(r.status) for r in check_identities("d", _idx(facts))}
        assert results["lender_nii"] == "PASSED"
        assert results["lender_net_total_income"] == "PASSED"
        assert results["lender_ppop"] == "PASSED"


class TestAMarginMayUseAnyRevenueLine:
    def test_gross_revenue(self):
        # 54,067 / 340,257 = 15.9% as printed; on net revenue it is 17.3%.
        facts = [_f("m", "ebitda_margin", 15.9, vt=ValueType.PERCENTAGE,
                    unit=MagnitudeUnit.PERCENT, period="Q1 FY27"),
                 _f("e", "ebitda", 54067, period="Q1 FY27"),
                 _f("r", "revenue", 311850, period="Q1 FY27"),
                 _f("g", "gross_revenue", 340257, period="Q1 FY27")]
        results = check_margins("d", _idx(facts))
        assert [str(r.status) for r in results] == ["PASSED"]


class TestAnUndatedColumnIsStillChecked:
    """Eternal's image cash-flow statement: heads unread, so no period, but
    (A)+(B)+(C) still has to equal the net change in each column. The
    misread "(168)" for "(1,168)" went unchecked."""

    def test_a_misread_total_fails_in_its_column(self):
        facts = [_f("a", "operating_cash_flow", 1015, period=None, col=1),
                 _f("b", "investing_cash_flow", -168, period=None, col=1),
                 _f("c", "financing_cash_flow", -355, period=None, col=1),
                 _f("n", "net_change_in_cash", -508, period=None, col=1)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "cash_flow_movement"]
        assert results and str(results[0].status) in ("FAILED", "WARNING")

    def test_the_same_column_adds_up_when_read_right(self):
        facts = [_f("a", "operating_cash_flow", 1015, period=None, col=1),
                 _f("b", "investing_cash_flow", -1168, period=None, col=1),
                 _f("c", "financing_cash_flow", -355, period=None, col=1),
                 _f("n", "net_change_in_cash", -508, period=None, col=1)]
        results = [r for r in check_identities("d", _idx(facts))
                   if r.rule == "cash_flow_movement"]
        assert [str(r.status) for r in results] == ["PASSED"]

    def test_two_columns_are_never_mixed(self):
        facts = [_f("a", "operating_cash_flow", 1015, period=None, col=1),
                 _f("b", "investing_cash_flow", -1168, period=None, col=2),
                 _f("c", "financing_cash_flow", -355, period=None, col=1),
                 _f("n", "net_change_in_cash", -508, period=None, col=1)]
        assert not [r for r in check_identities("d", _idx(facts))
                    if r.rule == "cash_flow_movement"]
