"""Phase 22: the remaining wrong facts, each traced to a general cause.

Every class here produced a wrong fact in the Phase 21 corpus -- a figure read
correctly and filed under the wrong measure, period basis or sign -- most of
them with no review flag. Each test is built from the page it came from.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.geometry import BBox
from app.extraction.confidence import (
    UNPARSED_VALUE_CAP, cap_unparsed_value, score, Signals,
)
from app.extraction.facts import (
    TableFactContext, _BARE_OPERATING_PROFIT, _MOVEMENT_HEADING, _bank_meanings,
    extract_from_table,
)
from app.extraction.taxonomy import match_metric, normalize_label
from app.models.enums import Currency, EntityType, MagnitudeUnit
from app.models.schemas import BBoxModel, FinancialFact, OCRElement
from app.normalization.numbers import looks_numeric, parse_number
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


def _key(label, stock=None):
    m = match_metric(label, stock=stock)
    return m.key if m else None


# ------------------------------------------------------------------ numbers
class TestFullwidthPunctuation:
    """A telecom's loss from associates, "(844)", arrived as "（844)" and was
    reported as +844 with no flag."""

    @pytest.mark.parametrize("text,value", [
        ("（844)", -844.0), ("（828）", -828.0), ("（1,234.56）", -1234.56),
        ("－12.5", -12.5), ("−3.2%", -3.2), ("１２３", 123.0), ("123，162", 123162.0)])
    def test_is_read_as_its_ascii_form(self, text, value):
        assert parse_number(text)[0] == value
        assert looks_numeric(text)

    def test_a_superscript_footnote_marker_is_not_a_digit(self):
        # NFKC would turn "12,351¹" into 123,511.
        assert parse_number("12,351¹")[0] == 12351.0


# ------------------------------------------------------------------ heads
class TestAChangeColumnHeadOverThreeLines:
    """SBI's analyst deck, page 19: "Quarter Ended" over Q1FY26 / Q4FY26 /
    Q1FY27, and "Growth (%)" over "Q1FY27 / over / Q4FY26" and "Q1FY27 /
    over / Q1FY26". Recovery stopped at "over"; each growth column kept only
    its last line and every growth rate became that quarter's figure."""

    def test_recovery_reads_the_whole_head(self):
        """The slide's own geometry, at 200 dpi: the band is cut at the first
        row of figures; the head lies above it."""
        from app.tables.structure import ColumnBand, RowBand, _head_lines_above
        cols = [ColumnBand(0, 123, 458, "numeric"), ColumnBand(1, 1266, 1387, "numeric"),
                ColumnBand(2, 1570, 1687, "numeric"), ColumnBand(3, 1857, 1974, "numeric"),
                ColumnBand(4, 2121, 2247, "numeric"), ColumnBand(5, 2380, 2508, "numeric")]
        head = [
            _line(0, 301, [("Quarter Ended", 1430, 190), ("Growth (%)", 2170, 157)], h=35),
            _line(1, 365, [("Particulars ( in Crores)", 440, 327), ("Q1FY27", 2059, 120),
                           ("Q1FY27", 2325, 117)], h=36),
            _line(2, 403, [("Q1FY26", 1182, 116), ("Q4FY26", 1487, 117),
                           ("Q1FY27", 1780, 115), ("over", 2084, 71), ("over", 2350, 72)], h=34),
            _line(3, 435, [("Q4FY26", 2060, 119), ("Q1FY26", 2325, 120)], h=35)]
        rows = [_line(4 + i, 492 + 62 * i, [(label, 125, 300)] + [
                    (v, x, 110) for v, x in zip(values, (1266, 1570, 1857, 2150, 2410))])
                for i, (label, values) in enumerate([
                    ("Interest Income", ["1,17,830", "1,23,098", "1,27,896", "3.90", "8.54"]),
                    ("Interest Expenses", ["76,923", "78,718", "80,904", "2.78", "5.17"])])]
        band = BBox.union_all(ln.bbox for ln in rows)
        found = _head_lines_above(head + rows, band, cols,
                                  [RowBand(i, [ln]) for i, ln in enumerate(rows)])
        texts = [[e.text for e in ln.elements] for ln in found]
        assert ["Quarter Ended", "Growth (%)"] in texts
        assert any("over" in t for t in texts)
        assert ["Q4FY26", "Q1FY26"] in texts

    def test_connectors_are_head_text(self):
        from app.tables.structure import _is_head_fragment
        for t in ("over", "vs", "Growth (%)", "YoY", "% chg", "(%)"):
            assert _is_head_fragment(t), t
        assert not _is_head_fragment("Interest Income")

    def test_the_whole_head_reaches_the_growth_column(self):
        from app.tables.structure import GROWTH_HEADER_RE
        assert GROWTH_HEADER_RE.search("Growth (%) Q1FY27 over Q4FY26")
        assert GROWTH_HEADER_RE.search("Q1FY27 over Q1FY26")


# ------------------------------------------------------------------ labels
class TestRunTogetherLines:
    def test_text_past_a_unit_bracket_is_the_next_line(self):
        # ICICI consolidated: the tail of basic EPS and the head of diluted,
        # on the row holding the BASIC figures, filed as diluted EPS.
        assert _key("expense (not annualised) (in R) Diluted EPs before and after "
                    "extraordinary items, net of tax expense") is None

    @pytest.mark.parametrize("label,key", [
        ("Revenue (in ₹ crore)", "revenue"),
        ("Earnings per share (in ₹)", "eps"),
        ("Paid Up Equity Capital (Face value of ₹ 1 per share) (Refer Note 9)",
         "share_capital")])
    def test_a_unit_bracket_ending_the_label_is_fine(self, label, key):
        assert _key(label, stock=True if key == "share_capital" else None) == key


class TestNamesThatMeanAComponent:
    def test_sales_income_from_operations_is_the_sales_line(self):
        # SEBI format line 1(a), beside 1(b) "Other operating revenues".
        assert _key("a) Sales / income from operations", stock=False) == "sales"
        assert _key("Total revenue from operations [1(a) + 1(b)]", stock=False) == \
            "revenue_from_operations"

    def test_profit_before_the_share_of_an_investee(self):
        assert _key("Profit before exceptional items and tax and before share of "
                    "equity-accounted investee", stock=False) == "pbt_before_associates"
        assert _key("PROFIT BEFORE EXCEPTIONAL ITEMS AND TAX", stock=False) == \
            "pbt_before_exceptional"


class TestAcronymConfusions:
    @pytest.mark.parametrize("label", ["EB/TDA", "EB1TDA", "EBlTDA"])
    def test_an_i_read_as_a_stroke_is_the_acronym(self, label):
        assert _key(label, stock=False) == "ebitda"

    def test_a_rate_alias_needs_a_rate_sign(self):
        assert _key("EB1TDA", stock=False) != "ebitda_margin"
        assert _key("EBITDA %", stock=False) == "ebitda_margin"

    @pytest.mark.parametrize("label", ["P/E", "D/E"])
    def test_real_ratios_are_left_alone(self, label):
        assert _key(label) != "ebitda"


class TestMovementsAreNotBalances:
    @pytest.mark.parametrize("heading", [
        "Movements in working capital:", "Changes in operating assets and liabilities:",
        "(Increase)/decrease in trade receivables", "Working capital adjustments:",
        "Increase/decrease in trade and other receivables"])
    def test_headings_that_announce_movements(self, heading):
        assert _MOVEMENT_HEADING.search(heading)

    def test_the_p_and_l_line_is_not_one(self):
        assert not _MOVEMENT_HEADING.search(
            "Changes in inventories of finished goods, work-in-progress and stock-in-trade")

    def test_a_balance_under_the_heading_is_not_filed_as_the_balance(self):
        """Apple's cash flow: "Accounts receivable, net 5,685" under "Changes in
        operating assets and liabilities:" had been receivables of 5,685."""
        lines = [_line(0, 40, [("Nine Months Ended", 600, 200)]),
                 _line(1, 60, [("June 28, 2025", 560, 110), ("June 29, 2024", 760, 110)]),
                 _line(2, 100, [("Depreciation and amortization", 20, 300),
                                ("8,571", 600, 60), ("8,534", 800, 60)]),
                 _line(3, 140, [("Changes in operating assets and liabilities:", 20, 400)]),
                 _line(4, 180, [("Accounts receivable, net", 40, 260),
                                ("5,685", 600, 60), ("6,697", 800, 60)]),
                 _line(5, 220, [("Inventories", 40, 160), ("1,223", 600, 60), ("41", 820, 30)]),
                 _line(6, 260, [("Cash generated by operating activities", 20, 380),
                                ("81,754", 590, 70), ("91,443", 790, 70)])]
        cal = FiscalCalendar(year_end_month=9)
        table = build_table(document_id="d", page_number=3, region_id="r", lines=lines,
                            v_lines=[], page_width=1000, page_height=900, table_index=0,
                            calendar=cal, ruled=False, base_confidence=0.7)
        assert table is not None
        ctx = TableFactContext(document_id="d", entity="Consolidated", entity_id="e",
                               entity_type=EntityType.CONSOLIDATED, calendar=cal,
                               default_currency=Currency.USD,
                               default_unit=MagnitudeUnit.MILLION)
        keys = {f.value: f.canonical_metric for f in extract_from_table(table, ctx)}
        assert keys.get(5685.0) is None and keys.get(1223.0) is None
        assert keys.get(8571.0) == "depreciation"
        assert keys.get(81754.0) == "operating_cash_flow"


class TestALendersOperatingProfit:
    def _fact(self, fid, label, key, value=1.0):
        return FinancialFact(fact_id=fid, document_id="d", metric=label, canonical_metric=key,
                             value=value, raw_value=str(value), meta={"table_id": "t"})

    def test_in_a_banks_summary_it_is_profit_before_provisions(self):
        facts = [self._fact("a", "Interest Income", "interest_earned"),
                 self._fact("b", "Net Interest Income", "net_interest_income"),
                 self._fact("c", "Operating Profit", "ebit")]
        _bank_meanings(facts)
        assert facts[2].canonical_metric == "pre_provision_operating_profit"

    def test_elsewhere_it_is_still_ebit(self):
        facts = [self._fact("a", "Revenue", "revenue"),
                 self._fact("c", "Operating Profit", "ebit")]
        _bank_meanings(facts)
        assert facts[1].canonical_metric == "ebit"

    def test_only_the_bare_name(self):
        assert _BARE_OPERATING_PROFIT.match(normalize_label("Operating Profit (3-6)"))
        assert not _BARE_OPERATING_PROFIT.match(normalize_label("Operating profit margin"))


class TestAnUnparsedValueCannotOutrankAParsedOne:
    def test_the_cap(self):
        bd = score(Signals(ocr=0.95, layout=0.9, structure=0.9, semantic=1.0))
        assert bd.overall > UNPARSED_VALUE_CAP
        capped = cap_unparsed_value(bd)
        assert capped.overall == UNPARSED_VALUE_CAP
        assert "not established" in capped.notes[-1]


# ------------------------------------------------------------------ number forms
class TestEveryPrintedForm:
    """Section 7 of the Phase 22 brief: every form, with its evidence."""

    @pytest.mark.parametrize("text,value", [
        ("1,234.56", 1234.56), ("1234.56", 1234.56), ("12,34,567.89", 1234567.89),
        ("1234,56", 1234.56), ("123，162", 123162.0), ("62005,98", 62005.98),
        ("(123.45)", -123.45), ("-123.45", -123.45), ("123.45-", -123.45),
        ("₹123.45", 123.45), ("$123.45", 123.45), ("123.45%", 123.45),
        ("14272.21", 14272.21), ("1.053.69", 1053.69),
        # European: a comma after grouped digits can only be the decimal mark
        ("1.234,56", 1234.56), ("1.234.567,89", 1234567.89), ("12 345,67", 12345.67),
        ("(1.234,56)", -1234.56)])
    def test_the_value(self, text, value):
        assert parse_number(text)[0] == value

    def test_two_figures_in_one_cell_are_never_one_number(self):
        from app.normalization.numbers import holds_more_than_one_number
        assert holds_more_than_one_number("20,211 17,292")
        assert not holds_more_than_one_number("62005,98")
        assert not holds_more_than_one_number("1.234,56")

    def test_the_evidence_says_how_it_was_read(self):
        from app.normalization.numbers import describe_number
        assert describe_number("12,34,567.89")["grouping"] == "indian"
        assert describe_number("1.234,56")["decimal"] == "comma"
        assert describe_number("(844)")["sign"] == "parentheses"
        assert describe_number("123.45-")["sign"] == "trailing minus"
        assert describe_number("1.053.69")["repairs"] == ["stops read as thousands separators"]
        assert describe_number("（844)")["repairs"]
