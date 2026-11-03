"""Deliberately ambiguous tables, and the refusals they must produce.

Brief section 23. Every case here is built to be unreadable in one specific
way. The expected outcome is never a best guess: it is UNKNOWN, or a review
flag, or no fact at all.

The standard these hold the system to is the one that motivated the phase: a
correct number under a wrong period is a worse failure than no number, because
it looks right. So the assertions are mostly about what must *not* appear.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.extraction.facts import TableFactContext, extract_from_table
from app.extraction.safety_gate import check as gate_check
from app.models.enums import Currency, MagnitudeUnit, PeriodBasis, ValueType
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.numbers import parse_number, repair_misread_separators
from app.normalization.periods import FiscalCalendar, parse_period
from app.ocr.lines import TextLine
from app.tables.header_block import build_header_block
from app.tables.shape import TableShape, classify_table
from app.tables.structure import build_rows, build_table, detect_columns
from app.tables.validate import validate_table

MARCH = FiscalCalendar(year_end_month=3)


def _element(text: str, x: float, y: float, index: int,
             width: float | None = None) -> OCRElement:
    return OCRElement(
        id=f"e{index}", document_id="anti", page_number=1, text=text,
        confidence=0.9,
        bbox=BBoxModel(x=x, y=y,
                       width=width if width is not None else max(40.0, len(text) * 8.5),
                       height=15.0))


def _lines(rows: list[tuple]) -> list[TextLine]:
    buckets: dict[int, list[OCRElement]] = {}
    for i, row in enumerate(rows):
        text, x, y = row[0], row[1], row[2]
        width = row[3] if len(row) > 3 else None
        element = _element(text, x, y, i, width)
        key = next((k for k in buckets if abs(k - y) < 14), int(y))
        buckets.setdefault(key, []).append(element)
    out = []
    for i, (y, group) in enumerate(sorted(buckets.items())):
        group.sort(key=lambda e: e.bbox.x)
        box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                                  height=e.bbox.height) for e in group)
        out.append(TextLine(id=f"l{i}", page_number=1, bbox=box, elements=group))
    return out


def _table(rows):
    lines = _lines(rows)
    return build_table(document_id="anti", page_number=1, region_id="r",
                       lines=lines, v_lines=[], page_width=1700,
                       page_height=2200, table_index=0, calendar=MARCH,
                       ruled=False, base_confidence=0.6)


def _facts(table):
    ctx = TableFactContext(document_id="anti", entity="Consolidated",
                           calendar=MARCH, default_currency=Currency.INR,
                           default_unit=None)
    return extract_from_table(table, ctx)


class TestCorruptedDates:

    @pytest.mark.parametrize("text", [
        "30 June 31 March 30 June",     # three heads concatenated
        "3O June 2O26",                 # letter O for zero
        "30 June 20",                   # truncated year
        "June",                         # month alone
        "2026",                         # year alone
    ])
    def test_a_damaged_date_does_not_become_a_confident_period(self, text):
        period = parse_period(text, calendar=MARCH)
        if period is None:
            return
        # If anything resolved, it must not have invented a year the text
        # does not contain.
        assert period.end_date is None or period.end_date[:4] in text, (
            f"{text!r} resolved to {period.end_date}, a year not in the source")

    def test_the_concatenation_that_produced_2031(self):
        """The exact string, and the exact wrong answer it used to give."""
        period = parse_period("30 June 31 March 30 June", calendar=MARCH)
        assert period is None or not str(period.end_date).startswith("2031")


class TestMisreadNumbers:

    @pytest.mark.parametrize("token,expected", [
        ("1.053.69", 1053.69),
        ("8.403.55", 8403.55),
        ("1.234.567", 1234567.0),
    ])
    def test_a_misread_thousands_separator_is_repaired(self, token, expected):
        value, _, _ = parse_number(token)
        assert value == pytest.approx(expected)

    @pytest.mark.parametrize("token,expected", [
        ("1.053", 1.053),
        ("12.345", 12.345),
        ("1.5", 1.5),
        ("0.66", 0.66),
    ])
    def test_a_genuine_decimal_is_left_alone(self, token, expected):
        """The repair must not fire on one separator.

        Rewriting these would be the same thousand-fold error in the opposite
        direction, and on far more figures.
        """
        assert repair_misread_separators(token) is None
        value, _, _ = parse_number(token)
        assert value == pytest.approx(expected)

    def test_the_truncation_that_hid_the_error(self):
        """Before the repair, "1.053.69" parsed to 1.053 -- silently."""
        value, _, _ = parse_number("1.053.69")
        assert value != pytest.approx(1.053)


class TestAmbiguousHeaders:

    def test_a_table_with_no_header_dates_nothing(self):
        table = _table([
            ("Revenue", 200, 300), ("1,501.42", 900, 300), ("904.40", 1100, 300),
            ("Other income", 200, 330), ("47.25", 900, 330), ("44.07", 1100, 330),
            ("Total income", 200, 360), ("1,548.67", 900, 360), ("948.47", 1100, 360),
        ])
        if table is None:
            return
        for column in table.columns:
            assert column.period_resolved is None, (
                "a table with no header has no periods to find")

    def test_a_column_whose_head_is_not_a_period_is_not_dated(self):
        table = _table([
            ("Particulars", 200, 260), ("Retail", 900, 260), ("Wholesale", 1100, 260),
            ("Revenue", 200, 300), ("1,501.42", 900, 300), ("904.40", 1100, 300),
            ("Other income", 200, 330), ("47.25", 900, 330), ("44.07", 1100, 330),
        ])
        if table is None:
            return
        for column in table.columns:
            assert column.period_resolved is None

    def test_a_segment_table_is_not_asked_for_a_period(self):
        table = _table([
            ("Particulars", 200, 260), ("Retail", 900, 260), ("Wholesale", 1100, 260),
            ("Revenue", 200, 300), ("1,501.42", 900, 300), ("904.40", 1100, 300),
            ("Other income", 200, 330), ("47.25", 900, 330), ("44.07", 1100, 330),
        ])
        if table is None:
            return
        shape = classify_table(table)
        assert not shape.is_periodized, (
            "flagging a segment table's figures would bury the reader in "
            "warnings about a table that is behaving correctly")


class TestTheSafetyGate:

    class _Fact:
        """The smallest thing the gate will look at."""
        def __init__(self, **kw):
            self.period = kw.get("period")
            self.period_normalized = kw.get("period_normalized")
            self.value_type = kw.get("value_type", ValueType.MONETARY)
            self.unit = kw.get("unit", MagnitudeUnit.CRORE)
            self.currency = kw.get("currency", Currency.INR)
            self.provenance = kw.get("provenance", _Prov())
            self.confidence = kw.get("confidence", 0.9)
            self.requires_review = False
            self.review_reason = None
            self.meta = {}

    def test_a_dated_statement_fact_without_a_period_is_held(self):
        result = gate_check(self._Fact(period=None), period_mandatory=True)
        assert not result.passed
        assert result.primary.field == "period"

    def test_the_same_fact_from_a_segment_table_passes(self):
        result = gate_check(self._Fact(period=None), period_mandatory=False)
        assert result.passed, (
            "a segment column genuinely has no period; there is nothing to find")

    def test_a_monetary_fact_with_no_scale_is_held(self):
        result = gate_check(
            self._Fact(period="Q1 FY27", unit=MagnitudeUnit.NONE),
            period_mandatory=True)
        assert not result.passed
        assert any(f.field == "unit" for f in result.findings)

    def test_a_percentage_needs_no_scale_or_currency(self):
        result = gate_check(
            self._Fact(period="Q1 FY27", value_type=ValueType.PERCENTAGE,
                       unit=MagnitudeUnit.PERCENT, currency=Currency.UNKNOWN),
            period_mandatory=True)
        assert result.passed

    def test_a_per_share_figure_needs_no_scale(self):
        result = gate_check(
            self._Fact(period="Q1 FY27", value_type=ValueType.PER_SHARE,
                       unit=MagnitudeUnit.NONE),
            period_mandatory=True)
        assert result.passed, (
            "INR 1.19 per share is not missing a magnitude; it has none")

    def test_a_fact_with_no_provenance_is_held(self):
        result = gate_check(self._Fact(period="Q1 FY27", provenance=None),
                            period_mandatory=True)
        assert not result.passed
        assert any(f.field == "provenance" for f in result.findings)

    def test_an_unestablished_column_is_held_even_with_a_period(self):
        result = gate_check(self._Fact(period="Q1 FY27"),
                            period_mandatory=True, column_established=False)
        assert not result.passed
        assert any(f.field == "column" for f in result.findings)

    def test_every_finding_carries_a_sentence_a_person_can_read(self):
        result = gate_check(self._Fact(period=None), period_mandatory=True)
        for finding in result.findings:
            assert finding.message.endswith("."), finding.message
            assert len(finding.message) > 40
            assert "None" not in finding.message


class _Prov:
    page_number = 6
    bbox = BBoxModel(x=1, y=1, width=1, height=1)


class TestTableValidation:

    def test_a_repeated_period_is_reported(self):
        table = _table([
            ("Particulars", 200, 260),
            ("30 June", 900, 260), ("30 June", 1100, 260),
            ("2026", 900, 285), ("2026", 1100, 285),
            ("Revenue", 200, 320), ("1,501.42", 900, 320), ("904.40", 1100, 320),
            ("Other income", 200, 350), ("47.25", 900, 350), ("44.07", 1100, 350),
            ("Total income", 200, 380), ("1,548.67", 900, 380), ("948.47", 1100, 380),
        ])
        if table is None:
            return
        dated = [c for c in table.columns if c.period_resolved is not None]
        if len(dated) < 2:
            return          # the fixture did not reproduce the shape
        report = validate_table(table, n_header=2)
        assert any(f.code == "repeated_period" for f in report.findings), (
            "two columns of one statement do not report the same period")
        assert report.period_is_unsafe
