"""Period-correct table extraction, held to a real statement.

The fixture below is Groww's consolidated statement of profit and loss, page 6,
transcribed from the source document rather than from anything FinSight
produced. Its geometry is the 200dpi render's own pixels and its column rulings
are drawn in the document.

The defect these tests exist to prevent: four figures from four dated columns
were all reported as "not period-specific", at HIGH confidence, with no review
flag. Across the corpus that shape of error affected 260 facts.

Every case here is built from OCR-shaped elements rather than by processing the
PDF, so the suite stays fast and the failure points at the layer that broke.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.extraction.facts import TableFactContext, extract_from_table
from app.models.enums import Currency, MagnitudeUnit, PeriodBasis, ValueType
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.core.geometry import BBox
from app.tables.header_block import build_header_block
from app.tables.shape import TableShape, classify_table
from app.tables.structure import build_rows, build_table, detect_columns

GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / "data" / "golden"
     / "GROWW_p6_consolidated_pl.json").read_text(encoding="utf-8"))

MARCH = FiscalCalendar(year_end_month=3)

#: The page-6 header block and a representative slice of its body, as
#: (text, x, y) at 200dpi. Taken from the source render.
HEADER_AND_BODY: list[tuple[str, float, float]] = [
    ("(All amounts are in INR Crores, unless otherwise stated)", 978, 389),
    ("Quarterended", 1009, 414),
    ("Yearended", 1278, 414),
    ("30 June", 903, 434), ("31 March", 1028, 435),
    ("30 June", 1161, 435), ("31 March", 1285, 435),
    ("Particulars", 187, 454),
    ("2026", 914, 455), ("2026", 1045, 454),
    ("2025", 1173, 455), ("2026", 1301, 454),
    ("(Unaudited)", 888, 479), ("(Audited)", 1030, 479),
    ("(Audited)", 1158, 479), ("(Audited)", 1285, 479),
    ("(Refer Note 3)", 1011, 498), ("(Refer Note 4)", 1137, 498),
    # body
    ("Revenue from operations", 185, 542),
    ("1,501.42", 926, 542), ("1,505.37", 1057, 542),
    ("904.40", 1195, 542), ("4,644.58", 1313, 542),
    ("Profit before tax for the period/year", 185, 862),
    ("992.27", 939, 862), ("935.70", 1069, 862),
    ("503.17", 1196, 862), ("2,821.42", 1313, 862),
    ("Current tax", 188, 930),
    ("260.40", 937, 930), ("268.55", 1069, 930),
    ("133.06", 1196, 930), ("770.24", 1327, 930),
    ("Deferred tax credit", 185, 949),
    ("(3.17)", 952, 949), ("(19.21)", 1073, 949),
    ("(8.24)", 1207, 949), ("(31.83)", 1331, 949),
    # The per-share block. The heading above these rows declares the unit for
    # them alone; the table's own declaration says crore and must not apply.
    ("Earnings per share in INR (Face Value : INR 2/-per share)", 188, 1533),
    ("(not annualised, except for year ended 31 March 2026)", 188, 1556),
    ("a) Basic earnings per share", 187, 1577),
    ("1.19", 958, 1577), ("1.11", 1088, 1577),
    ("0.66", 1212, 1577), ("3.47", 1343, 1577),
    ("b) Diluted earnings per share", 187, 1598),
    ("1.17", 958, 1598), ("1.09", 1088, 1598),
    ("0.63", 1214, 1598), ("3.40", 1343, 1598),
]

#: Widths matter: column membership is decided from the box, not the order.
_WIDTHS = {
    "(All amounts are in INR Crores, unless otherwise stated)": 401,
    "Quarterended": 113, "Yearended": 92,
    "30 June": 67, "31 March": 79, "Particulars": 90,
    "(Unaudited)": 95, "(Audited)": 75,
    "(Refer Note 3)": 114, "(Refer Note 4)": 116,
}


def _element(text: str, x: float, y: float, index: int) -> OCRElement:
    width = _WIDTHS.get(text, max(40.0, len(text) * 8.5))
    return OCRElement(
        id=f"e{index}", document_id="golden", page_number=6, text=text,
        confidence=0.93,
        bbox=BBoxModel(x=x, y=y, width=width, height=15.0))


def _lines(rows: list[tuple[str, float, float]]) -> list[TextLine]:
    buckets: dict[int, list[OCRElement]] = {}
    for i, (text, x, y) in enumerate(rows):
        element = _element(text, x, y, i)
        key = next((k for k in buckets if abs(k - y) < 14), int(y))
        buckets.setdefault(key, []).append(element)
    out = []
    for i, (y, group) in enumerate(sorted(buckets.items())):
        group.sort(key=lambda e: e.bbox.x)
        box = BBox.union_all(
            BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                 height=e.bbox.height) for e in group)
        out.append(TextLine(id=f"l{i}", page_number=6, bbox=box,
                            elements=group))
    return out


@pytest.fixture(scope="module")
def groww_table():
    lines = _lines(HEADER_AND_BODY)
    table = build_table(
        document_id="golden", page_number=6, region_id="r1", lines=lines,
        v_lines=[], page_width=1700, page_height=2200, table_index=0,
        calendar=MARCH, ruled=True, base_confidence=0.7)
    assert table is not None, "the fixture must reconstruct as a table"
    return table


@pytest.fixture(scope="module")
def groww_facts(groww_table):
    ctx = TableFactContext(
        document_id="golden", entity="Consolidated", calendar=MARCH,
        default_currency=Currency.INR, default_unit=None)
    return extract_from_table(groww_table, ctx)


def _by_metric(facts, needle: str):
    return [f for f in facts if needle.lower() in (f.metric or "").lower()]


class TestTheHeaderBlockIsReadAsAStructure:

    def test_the_unit_declaration_is_a_banner_not_a_column_head(self):
        lines = _lines(HEADER_AND_BODY)
        rows = build_rows(lines)
        cols = detect_columns(rows, BBox.union_all(l.bbox for l in lines),
                              [], 1700)
        block = build_header_block(rows, cols)
        assert any("INR Crores" in b for b in block.banners)
        for column in block.columns.values():
            assert not any("INR Crores" in line for line in column.lines), (
                "a declaration spanning every column is about the table, and "
                "must never become part of one column's head")

    def test_each_column_keeps_its_own_date(self):
        lines = _lines(HEADER_AND_BODY)
        rows = build_rows(lines)
        cols = detect_columns(rows, BBox.union_all(l.bbox for l in lines),
                              [], 1700)
        block = build_header_block(rows, cols)
        texts = {i: c.period_text for i, c in block.columns.items()}
        for text in texts.values():
            assert text.count("June") + text.count("March") == 1, (
                f"{text!r} carries more than one column's date -- this is the "
                "concatenation that produced a period in 2031")
        # Columns 2 and 4 share the date 31 March 2026 and that is correct:
        # one is the quarter ended on that day, the other the year ended on it.
        # What separates them is the group head, so it is the *qualified* text
        # that must be unique -- a check that the date alone would fail, and a
        # deduplicating one would collapse two real periods into one.
        qualified = {i: c.qualified_period_text for i, c in block.columns.items()}
        assert len(set(qualified.values())) == 4, (
            f"four columns must stay distinguishable, got {qualified}")
        assert texts[2] == texts[4] == "31 March 2026"

    def test_the_year_line_is_part_of_the_head(self):
        lines = _lines(HEADER_AND_BODY)
        rows = build_rows(lines)
        cols = detect_columns(rows, BBox.union_all(l.bbox for l in lines),
                              [], 1700)
        block = build_header_block(rows, cols)
        assert "2026" in block.for_column(1).period_text, (
            "the day/month and the year sit on different OCR lines; reading "
            "one line at a time cannot recover a date")

    def test_group_heads_reach_the_columns_they_span(self):
        lines = _lines(HEADER_AND_BODY)
        rows = build_rows(lines)
        cols = detect_columns(rows, BBox.union_all(l.bbox for l in lines),
                              [], 1700)
        block = build_header_block(rows, cols)
        assert block.for_column(1).groups == ["Quarterended"]
        assert block.for_column(3).groups == ["Quarterended"]
        assert block.for_column(4).groups == ["Yearended"], (
            "'Year ended' sits over a single column while 'Quarter ended' "
            "covers three; judging each head by its own width alone put the "
            "annual column under 'Quarter ended'")

    def test_audit_status_is_captured_and_kept_out_of_the_date(self):
        lines = _lines(HEADER_AND_BODY)
        rows = build_rows(lines)
        cols = detect_columns(rows, BBox.union_all(l.bbox for l in lines),
                              [], 1700)
        block = build_header_block(rows, cols)
        assert block.for_column(1).audit_status == "unaudited"
        assert block.for_column(2).audit_status == "audited"
        assert "Audited" not in block.for_column(2).period_text


class TestTheReportedDefect:
    """Brief section 9, verbatim: this exact table, these exact four facts."""

    def test_current_tax_produces_four_distinct_dated_facts(self, groww_facts):
        tax = _by_metric(groww_facts, "current tax")
        assert len(tax) == 4, f"expected four columns, got {len(tax)}"
        by_value = {round(f.value, 2): f for f in tax}
        expected = {260.40: "Q1 FY27", 268.55: "Q4 FY26",
                    133.06: "Q1 FY26", 770.24: "FY26"}
        assert set(by_value) == set(expected), (
            f"values changed: {sorted(by_value)}")
        for value, period in expected.items():
            assert by_value[value].period == period, (
                f"{value} should be {period}, got {by_value[value].period}")

    def test_the_four_periods_are_all_different(self, groww_facts):
        tax = _by_metric(groww_facts, "current tax")
        assert len({f.period for f in tax}) == 4, (
            "one period attached to a whole row is the defect itself")

    def test_none_of_them_is_not_period_specific(self, groww_facts):
        for fact in groww_facts:
            basis = getattr(fact.period_normalized, "basis", None)
            assert basis != PeriodBasis.NOT_APPLICABLE, (
                f"{fact.metric} {fact.value}: a dated statement column is "
                "never 'not applicable'")

    def test_the_annual_column_is_not_a_quarter(self, groww_facts):
        annual = [f for f in _by_metric(groww_facts, "current tax")
                  if round(f.value, 2) == 770.24][0]
        assert annual.period == "FY26"
        assert annual.period_normalized.quarter is None

    def test_they_all_carry_the_declared_scale(self, groww_facts):
        for fact in _by_metric(groww_facts, "current tax"):
            assert fact.unit == MagnitudeUnit.CRORE
            assert fact.currency == Currency.INR


class TestUnitScopeVsRowOverride:
    """Brief section 12. The table says crore; the EPS rows do not mean crore."""

    def test_earnings_per_share_is_not_in_crore(self, groww_facts):
        eps = _by_metric(groww_facts, "earnings per share")
        assert eps, "the per-share rows must still produce facts"
        for fact in eps:
            assert fact.value_type == ValueType.PER_SHARE
            assert fact.unit != MagnitudeUnit.CRORE, (
                "applying the table declaration here would report basic EPS "
                "of INR 1.19 as INR 1.19 crore")

    def test_monetary_rows_do_take_the_declaration(self, groww_facts):
        for fact in _by_metric(groww_facts, "current tax"):
            assert fact.unit == MagnitudeUnit.CRORE


class TestTableShape:

    def test_a_statement_of_profit_and_loss_is_period_flow(self, groww_table):
        assert classify_table(groww_table) == TableShape.PERIOD_FLOW

    def test_period_flow_makes_a_period_mandatory(self):
        assert TableShape.PERIOD_FLOW.is_periodized
        assert TableShape.POINT_IN_TIME.is_periodized
        assert TableShape.PERIODIZED_KPI.is_periodized

    def test_a_segment_table_does_not_owe_a_period(self):
        assert not TableShape.NON_PERIODIC.is_periodized

    def test_an_unclassified_table_is_not_forced(self):
        """Demanding a period from a table we could not read would flag every
        unusual layout, and a warning that fires everywhere is read nowhere."""
        assert not TableShape.UNCLASSIFIED.is_periodized


class TestAgainstTheGoldenFile:
    """The fixture on disk is the contract; this checks the code against it."""

    def test_the_golden_columns_match_what_is_extracted(self, groww_table):
        want = {c["index"]: c for c in GOLDEN["table"]["columns"]}
        for column in groww_table.columns:
            if column.index not in want:
                continue
            resolved = column.period_resolved
            assert resolved is not None, (
                f"column {column.index} lost its period")
            assert resolved.end_date == want[column.index]["period_end"], (
                f"column {column.index}: {resolved.end_date} != "
                f"{want[column.index]['period_end']}")

    def test_every_must_produce_entry_is_produced(self, groww_facts):
        columns = {c["id"]: c for c in GOLDEN["table"]["columns"]}
        for want in GOLDEN["acceptance"]["must_produce"]:
            if want["row"] not in ("Current tax",):
                continue          # the rest need the full page fixture
            end = columns[want["column"]]["period_end"]
            hits = [f for f in _by_metric(groww_facts, want["row"])
                    if abs(f.value - want["value"]) < 0.01]
            assert hits, f"{want['row']} {want['value']} was not produced"
            assert hits[0].period_normalized.end_date == end
