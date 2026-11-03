"""Phase 22, final evaluation (p22c): what it still got wrong, unflagged.

Eleven golden facts were wrong with no review flag, and three accounting
alarms fired over figures that were all correct. Each class below is one
cause, stated in terms of layout, wording or accounting structure.
"""
from __future__ import annotations

from app.extraction.confidence import review_reason_for
from app.extraction.facts import _off_the_label_line
from app.extraction.taxonomy import match_metric
from app.models.enums import (
    Currency, EntityType, MagnitudeUnit, ReviewReason, ValidationStatus, ValueType,
)
from app.models.schemas import BBoxModel, FinancialFact, TableCell
from app.validation.engine import (
    _declared_lines, _idx, _not_the_declared_lines, _own_line, check_identities, check_sanity,
)

from tests.test_p22_generalization import _by_value, _extract, _line


def _cell(y, h, col=1, text="1,162"):
    return TableCell(id=f"c{col}_{y}", table_id="t", document_id="d", page_number=1, row=0,
                     col=col, text=text, bbox=BBoxModel(x=100 * col, y=y, width=60, height=h),
                     is_numeric=col > 0)


class TestAFigureOnAnotherLine:
    """A scanned statement's heading "Other comprehensive income ...
    attributable to:" was joined to the figures of the unlabelled total line
    just above it, and they were filed as other comprehensive income."""

    def test_figures_half_a_line_above_a_one_line_label(self):
        label = _cell(1505, 34, col=0, text="Other comprehensive income attributable to:")
        assert _off_the_label_line(label, _cell(1491, 26), 27.0)

    def test_figures_on_the_labels_line(self):
        label = _cell(1650, 31, col=0, text="Profit for the quarter attributable to :")
        assert not _off_the_label_line(label, _cell(1658, 25), 26.0)

    def test_a_wrapped_label_carries_its_figures_on_either_line(self):
        label = _cell(1400, 58, col=0, text="Total comprehensive income ... attributable to")
        assert not _off_the_label_line(label, _cell(1402, 27), 30.0)

    def test_the_figure_is_put_in_front_of_a_person(self):
        f = FinancialFact(fact_id="f", document_id="d", metric="OCI", canonical_metric="other_comprehensive_income",
                          value=34469.0, value_type=ValueType.MONETARY, currency=Currency.INR,
                          unit=MagnitudeUnit.CRORE, confidence=0.9, meta={"row_misaligned": True})
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) == ReviewReason.METRIC_AMBIGUOUS


class TestTwoHoldersInOneLabel:
    """ "Owners of the Company Non controlling interest" -- two printed lines
    read as one row -- was filed as the minority's share, carrying the
    owners' figures."""

    def test_neither_holder_is_chosen(self):
        lines = [
            _line(0, 40, [("Quarter ended", 560, 160), ("Year ended", 800, 140)]),
            _line(1, 70, [("31.03.2026", 560, 110), ("31.03.2026", 800, 110)]),
            _line(2, 120, [("Profit for the period", 20, 300), ("4,490", 580, 60), ("16,652", 800, 70)]),
            _line(3, 160, [("Total comprehensive income for the period attributable to:", 20, 480)]),
            _line(4, 200, [("Owners of the Company Non controlling interest", 40, 420),
                           ("35,631", 580, 60), ("151,608", 800, 70)]),
        ]
        f = _by_value(_extract(lines))
        assert 35631.0 not in f or f[35631.0].canonical_metric not in (
            "tci_attributable_to_nci", "tci_attributable_to_owners")


class TestANarrowerKindOfTheSameCore:
    def test_construction_materials_are_not_the_cost_of_materials(self):
        assert match_metric("Construction materials consumed") is None

    def test_raw_materials_still_are(self):
        assert match_metric("Raw materials consumed").key == "raw_material_cost"
        assert match_metric("Cost of materials consumed").key == "raw_material_cost"


def _fact(fid, metric, key, value, *, raw=None, period="Q4 FY26"):
    return FinancialFact(
        fact_id=fid, document_id="d", metric=metric, canonical_metric=key, entity="Consolidated",
        entity_id="e1", entity_type=EntityType.CONSOLIDATED, value=value,
        raw_value=raw if raw is not None else f"{value:,}", value_type=ValueType.MONETARY,
        currency=Currency.INR, unit=MagnitudeUnit.CRORE, period=period, confidence=0.9)


class TestATotalThatNamesItsLines:
    """ "Total comprehensive income (9+10)" is line 9 -- profit after the share
    of joint ventures -- plus line 10. Tested on line 7, "Net profit after tax
    (5-6)", a correct statement failed by exactly the joint ventures' share."""

    def test_reading_the_lines(self):
        assert _declared_lines("Total comprehensive income (9+10) 11") == {"9", "10"}
        assert _declared_lines("XI Total comprehensive income for the period (IX+X)") == {"ix", "x"}
        assert _declared_lines("Total comprehensive income") is None
        assert _own_line("7 Net profit after tax (5-6)") == "7"
        assert _own_line("IX Profit for the period") == "ix"
        assert _own_line("Items that will not be reclassified") is None

    def test_an_operand_the_total_does_not_name(self):
        tci = _fact("t", "Total comprehensive income (9+10) 11", "total_comprehensive_income", 4409.64)
        pat = _fact("p", "7 Net profit after tax (5-6)", "pat", 6317.53)
        oci = _fact("o", "Other comprehensive income [net of tax] (a+b)", "other_comprehensive_income", -1723.42)
        assert _not_the_declared_lines(tci, [pat, oci])
        results = [r for r in check_identities("d", _idx([tci, pat, oci]))
                   if r.rule == "profit_plus_oci_is_comprehensive_income"]
        assert results == []

    def test_operands_the_total_names_are_checked(self):
        pat = _fact("p", "7 Net profit after tax (5-6)", "pat", 6317.53)
        pbt = _fact("b", "5 Profit before tax (3+4)", "pbt", 8410.28)
        tax = _fact("x", "6 Tax expense", "tax_expense", 2092.75)
        results = [r for r in check_identities("d", _idx([pat, pbt, tax]))
                   if r.rule == "pbt_less_tax_is_pat"]
        assert len(results) == 1 and results[0].status == ValidationStatus.PASSED


class TestAnOutflowIsNotASuspiciousNegative:
    def test_capital_expenditure_in_brackets(self):
        f = _fact("c", "Purchase of property, plant and equipment", "capex", -44049.78,
                  raw="(44,049.78)")
        assert not [r for r in check_sanity("d", [f]) if r.rule == "unexpected_negative"]


# ---------------------------------------------------------------------------
# Found by the section-40 human audit of p22d (300 facts, 15 companies): 18
# wrong and unflagged, all on pages no golden table covers.
# ---------------------------------------------------------------------------
from app.document.assembly import _basis_by_page, _prefix_key  # noqa: E402
from app.extraction.facts import _BUCKET_HEAD, _one_basis  # noqa: E402
from app.models.enums import RegionType  # noqa: E402
from app.models.schemas import Region  # noqa: E402
from app.normalization.periods import FiscalCalendar, looks_like_period_header, parse_period  # noqa: E402

MARCH = FiscalCalendar(year_end_month=3)


class TestTheRowSaysWhoseFigureItIs:
    def test_a_basis_in_the_label(self):
        assert _one_basis("CRAR (standalone)") == "standalone"
        assert _one_basis("Consolidated Profit") == "consolidated"
        assert _one_basis("Profit bridge - Standalone to Consolidated") is None
        assert _one_basis("Revenue from operations") is None

    def test_the_label_outranks_the_page(self):
        lines = [
            _line(0, 40, [("Q1 FY27", 560, 110), ("Q1 FY26", 800, 110)]),
            _line(1, 120, [("Standalone Profit", 20, 300), ("5,342", 580, 60), ("4,775", 800, 70)]),
            _line(2, 160, [("Consolidated Profit", 20, 300), ("6,896", 580, 60), ("6,108", 800, 70)]),
        ]
        f = _by_value(_extract(lines))
        assert f[6896.0].entity_type == EntityType.CONSOLIDATED
        assert f[5342.0].entity_type == EntityType.STANDALONE

    def test_a_line_introducing_the_rows_below(self):
        lines = [
            _line(0, 40, [("Quarter ended", 560, 160), ("Year ended", 800, 140)]),
            _line(1, 70, [("30.06.2023", 560, 110), ("31.03.2023", 800, 110)]),
            _line(2, 120, [("Revenue from operations", 20, 300), ("10,309.77", 580, 90), ("36,427.60", 800, 90)]),
            _line(3, 160, [("Key standalone financial information is given below:", 20, 480)]),
            _line(4, 200, [("Profit before tax", 20, 300), ("2,204.64", 580, 90), ("7,408.64", 800, 90)]),
        ]
        f = _by_value(_extract(lines))
        assert f[2204.64].entity_type == EntityType.STANDALONE
        assert f[10309.77].entity_type == EntityType.CONSOLIDATED


class TestPartsOfAMeasureAreNotTheMeasure:
    def test_bucket_heads(self):
        for h in ("1-7 D", ">2-3 M", ">6 M-1 Y", ">3-5 Y", ">5 Y", "Over 5 years", "Upto 1 year"):
            assert _BUCKET_HEAD.match(h), h
        for h in ("12 months", "Q1 FY27", "FY26", "June 30, 2026", "2025-26", "Q2 2026"):
            assert not _BUCKET_HEAD.match(h), h

    def test_a_maturity_bucket_column_yields_nothing(self):
        lines = [
            _line(0, 40, [("1-7 D", 400, 60), (">2-3 M", 560, 60), ("Total", 800, 60)]),
            _line(1, 120, [("Advances", 20, 200), ("9,101", 400, 60), ("21,389", 560, 60), ("4,14,280", 790, 80)]),
            _line(2, 160, [("Borrowings", 20, 200), ("7,011", 400, 60), ("12,777", 560, 60), ("3,20,001", 790, 80)]),
        ]
        values = {f.value for f in _extract(lines)}
        assert 21389.0 not in values and 9101.0 not in values

    def test_people_are_not_money(self):
        lines = [
            _line(0, 40, [("Full-time equivalents", 20, 300), ("EMEA", 560, 80), ("Total", 800, 80)]),
            _line(1, 120, [("Research and development", 20, 300), ("18,741", 560, 80), ("38,309", 800, 80)]),
            _line(2, 160, [("General and administration", 20, 300), ("4,098", 560, 80), ("7,415", 800, 80)]),
        ]
        assert not [f for f in _extract(lines) if f.value_type == ValueType.MONETARY]

    def test_a_stamp_is_not_a_figure(self):
        lines = [
            _line(0, 40, [("Quarter ended", 560, 160), ("Year ended", 800, 140)]),
            _line(1, 70, [("30.06.2023", 560, 110), ("31.03.2023", 800, 110)]),
            _line(2, 120, [("Other income", 20, 300), ("346.33", 580, 60), ("PUNE-35", 800, 90)]),
            _line(3, 160, [("Finance costs", 20, 300), ("11.02", 580, 60), ("31.51", 800, 90)]),
        ]
        assert 35.0 not in {f.value for f in _extract(lines)}


class TestAYearHeadOverItsQuarter:
    def test_the_quarter_governs(self):
        for text, label, kind in (("FY 26 Q4", "Q4 FY26", "quarter"), ("FY26 Q1", "Q1 FY26", "quarter"),
                                  ("FY26 H1", "H1 FY26", "half_year"), ("FY 26", "FY26", "fiscal_year")):
            p = parse_period(text, calendar=MARCH)
            assert (p.label, str(p.period_type)) == (label, kind), text

    def test_calendar_quarter_heads_are_heads(self):
        for text in ("Q2 2026", "Q1-Q2 2025", "January-June 2026"):
            assert looks_like_period_header(text), text


def _title(i, page, text, y=10):
    return Region(id=f"t{page}_{i}", document_id="d", page_number=page, type=RegionType.TITLE,
                  bbox=BBoxModel(x=0, y=y, width=500, height=20), confidence=0.9,
                  reading_order=i, text=text)


class TestAPageTitleOfTheSameBand:
    def test_the_numbers_in_a_title_band_do_not_make_it_another(self):
        assert _prefix_key("19-years financial snapshot") == _prefix_key("9-years financial snapshot")

    def test_a_subsidiary_slide_is_not_on_the_consolidated_basis(self):
        pages = {27: [_title(0, 27, "19-years financial snapshot - Consolidated")],
                 28: [_title(0, 28, "9-years financial snapshot - BHFL")]}
        out = _basis_by_page(pages, set(), {27: 1000, 28: 1000})
        assert out == {27: "consolidated", 28: "unstated"}

    def test_a_damaged_results_title_still_states_its_basis(self):
        pages = {4: [_title(0, 4, "Statement of Audited Standalone Financial Resuits for the quarter")]}
        assert _basis_by_page(pages, set(), {4: 1000}) == {4: "standalone"}


class TestTheStatementsOwnCashFlowLayout:
    """A statement that prints the effect of exchange rates above its net
    increase in cash was reported broken by the identity that leaves it out."""

    def _facts(self, fx_included: bool):
        net = -771548.0 if fx_included else -728805.0
        return [_fact("o", "Net cash provided by operating activities", "operating_cash_flow", 1876481.0),
                _fact("i", "Net cash used in investing activities", "investing_cash_flow", -1802002.0),
                _fact("f", "Net cash used in financing activities", "financing_cash_flow", -803284.0),
                _fact("x", "Effect of exchange rate changes on cash", "fx_effect_on_cash", -42743.0),
                _fact("n", "Net increase (decrease) in cash", "net_change_in_cash", net)]

    def _results(self, facts):
        return [r for r in check_identities("d", _idx(facts)) if r.rule.startswith("cash_flow_movement")]

    def test_fx_above_the_net_change(self):
        rs = self._results(self._facts(True))
        assert rs and all(r.status == ValidationStatus.PASSED for r in rs)

    def test_fx_below_the_net_change(self):
        rs = self._results(self._facts(False))
        assert rs and all(r.status == ValidationStatus.PASSED for r in rs)

    def test_a_statement_that_adds_up_neither_way_is_still_reported(self):
        facts = self._facts(True)
        facts[-1] = _fact("n", "Net increase (decrease) in cash", "net_change_in_cash", -500000.0)
        rs = self._results(facts)
        assert len(rs) == 1 and rs[0].rule == "cash_flow_movement" and rs[0].status != ValidationStatus.PASSED


class TestAStatementAfterAnotherIsNotItsContinuation:
    """p22e: SAP's year-to-date statement (A.2) followed its quarter statement
    (A.1) with the same column count; its head row -- "€ millions, unless
    otherwise stated | Q1-Q2 | 2026 | ..." -- was taken for data, and it
    inherited the quarter's heads: 41 half-year figures dated to a quarter."""

    def test_a_unit_declaration_row_is_head(self):
        from app.tables.header_block import looks_like_a_data_row
        assert not looks_like_a_data_row(["Q1-Q2", "2026", "Q1-Q2", "2025", "A in %"],
                                         "millions, unless otherwise stated")
        assert looks_like_a_data_row(["43 109", "44 228"], "Sales")

    def test_two_titled_statements_are_not_linked(self):
        from app.document.assembly import link_continuations
        from app.models.schemas import ExtractedTable, TableColumn

        def table(tid, page, stub, heads):
            cols = [TableColumn(index=0, header=stub)] + [
                TableColumn(index=i + 1, header=h) for i, h in enumerate(heads)]
            return ExtractedTable(id=tid, document_id="d", page_number=page, region_id=f"r{tid}",
                                  bbox=BBoxModel(x=0, y=100, width=500, height=400), n_rows=5,
                                  n_cols=len(cols), columns=cols, cells=[])
        a = table("a", 8, "(A.1) Consolidated Income Statements - Quarter", ["Q2 2026", "Q2 2025", "Δ in %"])
        b = table("b", 9, "(A.2) Consolidated Income Statements - Year-to-Date", ["", "", ""])
        link_continuations([a, b])
        assert b.continued_from_table_id is None
        assert [c.header for c in b.columns][1:] == ["", "", ""]
