"""Phase 22: what nine documents the rules had never seen got wrong.

The corpus grew by nine filings (HCLTech, L&T, Maruti, NTPC, Axis Bank,
Cholamandalam, ITC) read BEFORE any rule was changed for them. They produced
143 wrong facts, 73 with no review flag. Every class below is one general
cause of those, stated in terms of layout or accounting structure and built
from the page it came from; none names a company.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.extraction.confidence import review_reason_for
from app.extraction.facts import (
    TableFactContext, _bank_meanings, _cash_flow_balances, _head, _same_name_lines,
    extract_from_table, introduced_basis,
)
from app.extraction.taxonomy import match_metric
from app.models.enums import (
    Currency, EntityType, MagnitudeUnit, PeriodType, ReviewReason, ValidationStatus,
)
from app.models.schemas import BBoxModel, FinancialFact, OCRElement, TableColumn
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import build_table

MARCH = FiscalCalendar(year_end_month=3)


def _line(i, y, items, page=1, h=18.0):
    els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=page, text=t,
                      confidence=0.95, bbox=BBoxModel(x=x, y=y, width=w, height=h))
           for j, (t, x, w) in enumerate(items)]
    box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                              height=e.bbox.height) for e in els)
    return TextLine(id=f"l{i}", page_number=page, bbox=box, elements=els)


def _extract(lines, *, unit=MagnitudeUnit.CRORE, width=1000, cal=MARCH):
    table = build_table(document_id="d", page_number=2, region_id="r", lines=lines,
                        v_lines=[], page_width=width, page_height=1400, table_index=0,
                        calendar=cal, ruled=False, base_confidence=0.7)
    assert table is not None
    ctx = TableFactContext(document_id="d", entity="Consolidated", entity_id="e",
                           entity_type=EntityType.CONSOLIDATED, calendar=cal,
                           default_currency=Currency.INR, default_unit=unit)
    return extract_from_table(table, ctx)


def _by_value(facts):
    return {f.value: f for f in facts}


# ------------------------------------------------------------------ holders
class TestAHolderRowTakesTheHeadingsMeasure:
    """HCLTech FY26 and L&T: "Non-controlling interest" two rows under "Other
    comprehensive income (loss) for the period / year attributable to" was
    filed as the minority's share of PROFIT -- its reading alone."""

    def _lines(self, heading):
        return [
            _line(0, 40, [("Quarter ended", 560, 160), ("Year ended", 800, 140)]),
            _line(1, 70, [("31.03.2026", 560, 110), ("31.03.2026", 800, 110)]),
            _line(2, 120, [("Profit for the period", 20, 300), ("4,490", 580, 60), ("16,652", 800, 70)]),
            _line(3, 160, [(heading, 20, 480)]),
            _line(4, 200, [("Owners of the Company", 40, 260), ("914", 590, 40), ("3,705", 810, 60)]),
            _line(5, 240, [("Non-controlling interest", 40, 260), ("2", 600, 20), ("4", 820, 20)]),
        ]

    def test_under_an_oci_heading_it_is_the_minoritys_oci(self):
        f = _by_value(_extract(self._lines(
            "Other comprehensive income (loss) for the period / year attributable to")))
        assert f[914.0].canonical_metric == "oci_attributable_to_owners"
        assert f[2.0].canonical_metric == "oci_attributable_to_nci"

    def test_under_a_total_comprehensive_income_heading(self):
        f = _by_value(_extract(self._lines(
            "Total comprehensive income (loss) for the period / year attributable to")))
        assert f[2.0].canonical_metric == "tci_attributable_to_nci"

    def test_a_heading_run_into_the_row_above_still_attributes(self):
        """The heading merged into the line with the total's figures:
        "Total comprehensive income ... IX Profit (loss) for the period /
        year attributable to" -- it attributes the PROFIT."""
        from app.extraction.facts import _ATTRIBUTION_CLAUSE
        m = _ATTRIBUTION_CLAUSE.search("Total comprehensive income for the period / year "
                                       "Ix Profit (loss) for the period / year attributable to")
        assert m and m.group(0).lower().startswith("profit (loss)")


# ------------------------------------------------------------------ per share
class TestAPerShareContextIsNotMoney:
    """HCLTech: "Total dividend paid (in ₹)" under "Dividend per equity share
    of ₹ 2 each" was filed as dividends paid, in crore, three times unflagged."""

    def test_no_monetary_measure_under_a_per_share_heading(self):
        lines = [
            _line(0, 40, [("Quarter ended", 560, 160)]),
            _line(1, 70, [("30.06.2026", 560, 110), ("31.03.2026", 760, 110)]),
            _line(2, 110, [("Diluted EPS (in ₹)", 40, 200), ("17.06", 590, 50), ("16.56", 790, 50)]),
            _line(3, 150, [("Dividend per equity share of ₹ 2 each", 20, 420)]),
            _line(4, 190, [("Interim dividend paid (in ₹)", 40, 300), ("24", 600, 30), ("12", 800, 30)]),
            _line(5, 230, [("Total dividend paid (in ₹)", 40, 300), ("24", 600, 30), ("12", 800, 30)]),
        ]
        facts = _extract(lines)
        assert facts, "the table is read"
        assert not [f for f in facts if f.canonical_metric == "dividends_paid"]

    def test_face_value_per_share_is_not_the_figures_context(self):
        lines = [
            _line(0, 40, [("Quarter ended", 560, 160)]),
            _line(1, 70, [("30.06.2026", 560, 110), ("31.03.2026", 760, 110)]),
            _line(2, 120, [("Profit for the period", 20, 300), ("2,680", 590, 60), ("2,768", 790, 60)]),
            _line(3, 160, [("Paid up equity share capital (face value of Re. 1 per share)", 20, 480),
                           ("235", 600, 40), ("236", 800, 40)]),
        ]
        keys = {f.value: f.canonical_metric for f in _extract(lines)}
        assert keys.get(235.0) == "share_capital"


# ------------------------------------------------------------------ subjects
class TestFuzzyMatchingDoesNotCrossSubjects:
    @pytest.mark.parametrize("label,wrong", [
        # HCLTech's balance sheet: a total of equity filed as the period's profit
        ("Equity attributable to owners of the Company", "pat_attributable_to_owners"),
        # a gain on sale filed as the cash received from the sale
        ("Profit on sale of property, plant and equipment (net)", "proceeds_from_sale_of_ppe"),
    ])
    def test_different_subjects_do_not_match(self, label, wrong):
        m = match_metric(label)
        assert m is None or m.key != wrong

    def test_the_equity_line_reaches_equity(self):
        assert match_metric("Equity attributable to owners of the Company").key == \
            "equity_attributable_to_owners"
        assert match_metric("Total equity attributable to shareholders of the parent").key == \
            "equity_attributable_to_owners"


# ------------------------------------------------------------------ two lines, one name
def _fact(fid, label, key, value, row, col=1, method="exact"):
    return FinancialFact(fact_id=fid, document_id="d", metric=label, canonical_metric=key,
                         value=value, raw_value=str(value),
                         meta={"row": row, "col": col, "metric_match_method": method})


class _Table:
    warnings: list

    def __init__(self):
        self.warnings = []


class TestTwoLinesOfOneTableReadAsOneMeasure:
    def test_a_fuzzy_near_miss_of_an_exact_line_yields(self):
        """L&T: "Construction materials consumed" matched the cost of materials
        by similarity beside the line that IS that cost."""
        facts = [_fact("a", "Cost of materials consumed", "raw_material_cost", 8170.14, 3),
                 _fact("b", "Construction materials consumed", "raw_material_cost", 11729.13, 4,
                       method="fuzzy")]
        kept = _same_name_lines(facts, _Table())
        assert [f.fact_id for f in kept] == ["a"]

    def test_a_dangling_heading_with_merged_figures_yields(self):
        """Maruti's scanned consolidated page: "Other comprehensive income ...
        attributable to:" carried the profit subtotal's figures, beside the
        line that IS other comprehensive income."""
        facts = [_fact("a", "Total other comprehensive income for the period (i+ii)",
                       "other_comprehensive_income", 1162.0, 20, method="prefix"),
                 _fact("b", "other comprehensive income for the period/year attributable to:",
                       "other_comprehensive_income", 34469.0, 23)]
        kept = _same_name_lines(facts, _Table())
        assert [f.fact_id for f in kept] == ["a"]

    def test_two_exact_lines_are_two_genuine_lines(self):
        """Current and non-current borrowings are both "Borrowings"."""
        facts = [_fact("a", "Borrowings", "debt", 37.0, 10), _fact("b", "Borrowings", "debt", 122.0, 20)]
        assert len(_same_name_lines(facts, _Table())) == 2
        assert not any((f.meta or {}).get("metric_collision") for f in facts)

    def test_two_equally_weak_readings_go_to_review(self):
        facts = [_fact("a", "Investment in equity instruments", "purchase_of_investments", -8.0, 10,
                       method="fuzzy"),
                 _fact("b", "Investments in securities", "purchase_of_investments", -48685.0, 11,
                       method="fuzzy")]
        kept = _same_name_lines(facts, _Table())
        assert len(kept) == 2
        assert all(review_reason_for(f, ValidationStatus.NOT_CHECKED)
                   == ReviewReason.METRIC_AMBIGUOUS for f in kept)

    def test_the_same_figure_printed_twice_is_not_a_collision(self):
        facts = [_fact("a", "Profit for the period", "pat", 100.0, 5),
                 _fact("b", "Profit for the period", "pat", 100.0, 9, method="fuzzy")]
        assert len(_same_name_lines(facts, _Table())) == 2


# ------------------------------------------------------------------ cash flows
class TestBalancesInsideACashFlowStatement:
    def test_are_movements_and_not_extracted(self):
        """NTPC: "Bank balances other than cash and cash equivalents 2351.87"
        in the investing section was filed as the balance."""
        facts = [_fact("a", "Net cash from operating activities - A", "operating_cash_flow", 40999.88, 30),
                 _fact("b", "Net cash from investing activities - B", "investing_cash_flow", -20118.13, 44),
                 _fact("c", "Bank balances other than cash and cash equivalents", "other_bank_balances",
                       2351.87, 42),
                 _fact("d", "Cash and cash equivalents at the end of the year", "cash_and_equivalents",
                       672.50, 60)]
        kept = {f.fact_id for f in _cash_flow_balances(facts, _Table())}
        assert kept == {"a", "b", "d"}

    def test_a_balance_sheet_keeps_its_balances(self):
        facts = [_fact("a", "Bank balances other than cash", "other_bank_balances", 1.0, 3),
                 _fact("b", "Total assets", "total_assets", 9.0, 9)]
        assert len(_cash_flow_balances(facts, _Table())) == 2

    def test_net_change_in_and_after_working_capital_headings_are_movements(self):
        """HCLTech: "Net change in" over "Trade receivables", "Inventories";
        NTPC: "Operating profit before working capital changes" then a heading
        as plain as "Adjustment for:" over the balances that moved."""
        for heading_lines in (
                [_line(3, 160, [("Net change in", 20, 200)])],
                [_line(2, 140, [("Operating profit before working capital changes", 20, 420),
                                ("47620.06", 590, 80), ("47985.81", 790, 80)]),
                 _line(3, 170, [("Adjustment for:", 20, 200)])]):
            lines = ([_line(0, 40, [("Year ended", 560, 140), ("Year ended", 760, 140)]),
                      _line(1, 70, [("31.03.2026", 560, 110), ("31.03.2025", 760, 110)]),
                      _line(9, 110, [("Profit before tax", 20, 260), ("22556.93", 590, 80), ("23636.40", 790, 80)])]
                     + heading_lines
                     + [_line(4, 200, [("Trade receivables", 40, 220), ("(1898.13)", 590, 90), ("(1941.39)", 790, 90)]),
                        _line(5, 240, [("Inventories", 40, 160), ("1778.85", 590, 80), ("881.98", 790, 80)]),
                        _line(6, 280, [("Cash generated from operations", 20, 380),
                                       ("43922.53", 590, 80), ("45226.11", 790, 80)])])
            keys = {f.value: f.canonical_metric for f in _extract(lines)}
            assert keys.get(-1898.13) is None and keys.get(1778.85) is None
            assert keys.get(43922.53) == "cash_generated_from_operations"


# ------------------------------------------------------------------ periods
class TestAFlowUnderABareDate:
    """Maruti's scanned standalone page: the first column's head read as
    "June 30, 2026 / Unaudited" -- its "Quarter ended" group head lost -- and
    19 lines of a quarter's profit and loss were reported as balances at
    June 30, unflagged."""

    def test_has_an_unknown_span_and_goes_to_review(self):
        lines = [_line(0, 40, [("June 30, 2026", 560, 130), ("June 30, 2025", 760, 130)]),
                 _line(1, 70, [("Unaudited", 570, 100), ("Unaudited", 770, 100)]),
                 _line(2, 120, [("Sale of products", 20, 260), ("499,591", 580, 80), ("366,206", 780, 80)]),
                 _line(3, 160, [("Inventories", 20, 160), ("12,013", 580, 80), ("11,006", 780, 80)])]
        facts = _by_value(_extract(lines, unit=MagnitudeUnit.MILLION))
        sale = facts[499591.0]
        assert sale.period_normalized.end_date == "2026-06-30"
        assert sale.period_normalized.period_type == PeriodType.UNKNOWN
        assert review_reason_for(sale, ValidationStatus.NOT_CHECKED) == ReviewReason.AMBIGUOUS_PERIOD
        # a balance under the same head is a balance at that date
        assert facts[12013.0].period_normalized.period_type == PeriodType.POINT_IN_TIME

    def test_an_as_at_head_is_unaffected(self):
        lines = [_line(0, 40, [("As at 31.03.2026", 560, 160), ("As at 31.03.2025", 760, 160)]),
                 _line(1, 120, [("Goodwill", 20, 160), ("23,888", 580, 70), ("21,756", 780, 70)]),
                 _line(2, 160, [("Inventories", 20, 160), ("2,239", 580, 70), ("1,133", 780, 70)]),
                 _line(3, 200, [("Total assets", 20, 200), ("116,258", 580, 80), ("105,544", 780, 80)])]
        f = _by_value(_extract(lines))
        assert f[2239.0].period_normalized.period_type == PeriodType.POINT_IN_TIME


# ------------------------------------------------------------------ lender meanings
class TestWhatALenderMeansByAName:
    def test_operating_revenue_is_net_total_income(self):
        """Axis Bank's release: "Operating Revenue" (NII + other income) was
        filed as revenue from operations."""
        facts = [_fact("a", "Net Interest Income", "net_interest_income", 14646.0, 1),
                 _fact("b", "Operating Revenue", "revenue_from_operations", 21382.0, 5),
                 _fact("c", "Operating Profit", "pre_provision_operating_profit", 11659.0, 8)]
        _bank_meanings(facts)
        assert facts[1].canonical_metric == "net_total_income"

    def test_net_income_beside_profit_after_tax_is_not_profit(self):
        """A finance company's release: "Net Income" = total income less
        finance cost, printed above "Profit After Tax"."""
        facts = [_fact("a", "Total Income", "total_income", 8933.0, 1),
                 _fact("b", "Finance Cost", "finance_costs", -4003.0, 2),
                 _fact("c", "Net Income", "net_income", 4930.0, 3),
                 _fact("d", "Loan Losses", "impairment_on_financial_instruments", -922.0, 5),
                 _fact("e", "Profit After Tax", "pat", 1654.0, 7),
                 _fact("f", "AUM", "aum", 254392.0, 0)]
        _bank_meanings(facts)
        assert facts[2].canonical_metric == "net_total_income"

    def test_elsewhere_net_income_beside_profit_goes_to_review(self):
        facts = [_fact("c", "Net Income", "net_income", 10.0, 3), _fact("e", "Profit After Tax", "pat", 5.0, 7)]
        _bank_meanings(facts)
        assert facts[0].canonical_metric == "net_income"
        assert review_reason_for(facts[0], ValidationStatus.NOT_CHECKED) == ReviewReason.METRIC_AMBIGUOUS

    def test_net_income_alone_is_net_income(self):
        facts = [_fact("c", "Net Income", "net_income", 10.0, 3)]
        _bank_meanings(facts)
        assert facts[0].canonical_metric == "net_income"
        assert not (facts[0].meta or {}).get("metric_collision")


# ------------------------------------------------------------------ bases
class TestABasisStatedWhereATableIsIntroduced:
    class _T:
        def __init__(self, title, caption, meta=None):
            self.title, self.caption, self.meta, self.columns = title, caption, meta or {}, []

    def test_a_standalone_table_in_consolidated_notes(self):
        """L&T's consolidated results, note (vi): "... The specified items of
        the standalone financial results of the Company ... are given below:"."""
        t = self._T("Average Inventory",
                    "(vi) The Company reports its consolidated financial results on a quarterly "
                    "basis. The standalone financial results are available on the Company's "
                    "website. The specified items of the standalone financial results of the "
                    "Company for the quarter ended June 30, 2026 are given below:")
        assert introduced_basis(t) == "standalone"

    def test_a_sentence_naming_both_or_introducing_nothing_says_nothing(self):
        assert introduced_basis(self._T("", "Standalone and consolidated results are given below:")) is None
        assert introduced_basis(self._T("", "The consolidated results were approved by the Board.")) is None

    def test_a_titled_table_keeps_its_title(self):
        assert introduced_basis(self._T("Consolidated Balance Sheet",
                                        "The standalone figures are given below:")) is None


class TestNoSilentConsolidatedDefault:
    """Axis Bank's release states no basis on any table; a footnote mentions
    "consolidated financial statements of the Group" for subsidiaries'
    figures. Every figure had been filed under the consolidated group."""

    def test_an_unstated_region_is_unstated(self):
        from app.document.entities import EntityRegistry
        from app.document.assembly import build_sections
        from app.models.enums import RegionType
        from app.models.schemas import Region
        reg = EntityRegistry("d", "Axis Bank Limited")
        region = Region(id="r1", document_id="d", page_number=1, type=RegionType.TABLE,
                        bbox=BBoxModel(x=0, y=0, width=10, height=10), reading_order=1, text="", confidence=0.9)
        _, by_region, _, registry = build_sections({1: [region]}, set(), "Axis Bank Limited", reg)
        ref = registry.get(by_region["r1"])
        assert ref.entity_type == EntityType.UNKNOWN


# ------------------------------------------------------------------ cells and heads
class TestADashAndAPollutedHead:
    def test_a_printed_dash_is_not_a_figure_of_zero(self):
        lines = [_line(0, 40, [("Quarter ended", 560, 160)]),
                 _line(1, 70, [("30.06.2026", 560, 110), ("31.03.2026", 760, 110)]),
                 _line(2, 120, [("Profit for the period", 20, 300), ("4,626", 590, 60), ("4,490", 790, 60)]),
                 _line(3, 160, [("Income tax relating to items that will not be reclassified", 20, 480),
                                ("-", 610, 10), ("147", 790, 40)])]
        values = [f.value for f in _extract(lines)]
        assert 0.0 not in values and 147.0 in values

    def test_a_percent_in_swallowed_data_does_not_make_a_column_a_rate(self):
        col = TableColumn(index=1, header="As on 30th June'26 13,72,936 5,21,698 3,48,312 38%")
        assert "%" not in _head(col)
        assert _head(TableColumn(index=2, header="YoY (%)")) == "YoY (%)"


# ------------------------------------------------------------------ number formats
class TestASpaceGroupedStatement:
    """Nestlé's half-year statements print "43 109" and "(23 118)" -- a space
    as the thousands separator and no decimals in the thousands. Every figure
    was refused as "two figures in one cell"; the table's own figures are the
    evidence of its convention."""

    NESTLE = ["43 109", "44 228", "191", "182", "(23 118)", "(23 609)", "(3 628)",
              "(3 685)", "(8 700)", "(9 043)", "(773)", "(786)", "16", "73", "6 247", "6 885"]

    def test_the_tables_figures_establish_space_grouping(self):
        from app.normalization.numbers import holds_more_than_one_number, table_grouping
        g = table_grouping(self.NESTLE)
        assert g == "space"
        assert not holds_more_than_one_number("43 109", grouping=g)
        assert not holds_more_than_one_number("(23 118)", grouping=g)

    def test_runs_that_are_two_figures_are_still_refused(self):
        from app.normalization.numbers import holds_more_than_one_number, table_grouping
        g = table_grouping(self.NESTLE)
        for text in ("228 88", "3 551 4 579", "20 211 17 292"):
            assert holds_more_than_one_number(text, grouping=g), text

    def test_a_comma_table_or_a_lone_space_run_is_not_space_grouped(self):
        from app.normalization.numbers import table_grouping
        assert table_grouping(["20,211", "17,292", "228 88", "1,234"]) == "comma"
        assert table_grouping(["43 109", "12", "45", "7", "88", "91", "3", "5", "66", "120"]) is None


class TestSwissFrancs:
    def test_chf_is_a_currency(self):
        from app.models.enums import Currency
        from app.normalization.numbers import detect_currency
        assert detect_currency("In millions of CHF")[0] == Currency.CHF
        assert detect_currency("Swiss francs")[0] == Currency.CHF


class TestANotesColumnIsNotFigures:
    """An IFRS statement's "Notes" column -- "3", "5", "6" between the labels
    and the figures -- was read as figures: a note number 5 filed as other
    operating income."""

    def test_note_references_are_not_extracted(self):
        lines = [
            _line(0, 40, [("Notes", 420, 50), ("January-June", 560, 120), ("January-June", 760, 120)]),
            _line(1, 70, [("2026", 580, 50), ("2025", 780, 50)]),
            _line(2, 120, [("Sales", 20, 100), ("3", 440, 10), ("43 109", 575, 70), ("44 228", 775, 70)]),
            _line(3, 160, [("Cost of goods sold", 20, 220), ("(23 118)", 570, 80), ("(23 609)", 770, 80)]),
            _line(4, 200, [("Other operating income", 20, 260), ("5", 440, 10), ("292", 590, 40), ("183", 790, 40)]),
            _line(5, 240, [("Other operating expenses", 20, 260), ("5", 440, 10), ("(1 520)", 575, 70), ("(244)", 785, 50)]),
            _line(6, 280, [("Operating profit", 20, 200), ("5 019", 580, 60), ("6 824", 780, 60)]),
            _line(7, 320, [("Income from associates", 20, 260), ("6", 440, 10), ("507", 590, 40), ("704", 790, 40)]),
        ]
        facts = _extract(lines, unit=MagnitudeUnit.MILLION)
        values = [f.value for f in facts]
        assert 5.0 not in values and 6.0 not in values and 3.0 not in values
        # and the space-grouped figures are read whole (the table's convention)
        assert 43109.0 in values and -1520.0 in values and 5019.0 in values


class TestAGrossLineIsNotItsNet:
    def test_fuzzy_matching_does_not_add_net(self):
        m = match_metric("Other operating income")
        assert m is None or m.key != "other_operating_income_expense"
        assert match_metric("Other operating income/(expense), net").key == "other_operating_income_expense"
        assert match_metric("Netprofit").key == "pat"
