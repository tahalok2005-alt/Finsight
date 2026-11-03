"""A bank's nine-month statement: three header lines in one band.

Found in the Phase 21 corpus (a private bank's press table, typeset). The
head prints three lines close together -- span words, dates, audit status --
and a label cell "Sr / No" spans two of them, so row banding merged all three
into ONE header band. Read as one row, the wide "Nine months ended" made it a
group row, every date on it was taken for a title, and no column kept its
date: the nine months to 31-Dec-24 came out as a balance "as at" that day.

The band and column geometry below is exactly what the pipeline produced on
that page (200 dpi OCR boxes), so the test drives the header reader with the
input that broke it.

Two more defects of the same statement are held here:
  * a phantom empty column at the page margin pushed the serial-number column
    to index 1, the serial test (pinned to index 0) missed it, the empty
    column became the label column, and the page produced no facts at all;
  * "Nine months ended" is printed CENTRED over its two columns and starts to
    the right of the first one's centre, so a left-edge rule gave that column
    to "Quarter ended".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.extraction.facts import TableFactContext, _label_columns, extract_from_table
from app.models.enums import CellRole, Currency, MagnitudeUnit
from app.models.schemas import (
    BBoxModel, ExtractedTable, OCRElement, TableCell, TableColumn,
)
from app.normalization.periods import FiscalCalendar, parse_period
from app.tables.header_block import build_header_block

MARCH = FiscalCalendar(year_end_month=3)


@dataclass
class _Band:
    elements: list
    is_header: bool = False


@dataclass
class _Col:
    index: int
    x0: float
    x1: float


def _el(i, text, x, y, w, h):
    return OCRElement(id=f"e{i}", document_id="d", page_number=1, text=text,
                      confidence=0.95, bbox=BBoxModel(x=x, y=y, width=w, height=h))


#: The page's columns as the reconstruction found them (0 is a phantom).
COLS = [_Col(0, 112, 120), _Col(1, 120, 175), _Col(2, 175, 652),
        _Col(3, 652, 810), _Col(4, 810, 968), _Col(5, 968, 1125),
        _Col(6, 1125, 1282), _Col(7, 1282, 1440), _Col(8, 1440, 1593)]

#: The whole head, merged into one band as the pipeline merged it.
HEAD = [('Sr', 120, 424, 32, 31), ('No', 121, 452, 37, 30),
        ('Particulars', 188, 439, 103, 25),
        ('Quarter ended', 818, 410, 143, 29), ('Nine months ended', 1204, 411, 189, 25),
        ('Year ended', 1462, 410, 114, 29),
        ('31-Dec-25', 681, 441, 102, 24), ('30-Sep-25', 837, 439, 104, 29),
        ('31-Dec-24', 997, 441, 102, 24), ('31-Dec-25', 1155, 441, 100, 24),
        ('31-Dec-24', 1310, 441, 103, 24), ('31-Mar-25', 1466, 441, 105, 24),
        ('(Unaudited)', 673, 467, 117, 25), ('(Unaudited)', 831, 467, 117, 25),
        ('(Unaudited)', 989, 467, 117, 25), ('(Unaudited)', 1146, 467, 117, 25),
        ('(Unaudited)', 1304, 467, 115, 25), ('(Audited)', 1471, 465, 97, 30)]
FIRST_ROW = [('1', 132, 496, 21, 25), ('Interest earned (a+b+c+d)', 186, 496, 264, 25),
             ('17,506.80', 688, 493, 111, 30), ('17,198.74', 845, 493, 110, 30),
             ('16,633.14', 1003, 493, 110, 30), ('51,953.85', 1162, 493, 112, 30),
             ('48,896.90', 1319, 493, 111, 30), ('65,668.83', 1477, 493, 111, 30)]


def _bands():
    return [_Band([_el(0, 'crore', 1482, 371, 88, 35)]),
            _Band([_el(10 + i, *t) for i, t in enumerate(HEAD)]),
            _Band([_el(50 + i, *t) for i, t in enumerate(FIRST_ROW)])]


class TestAMergedHeadIsReadLineByLine:
    def test_every_date_reaches_its_column(self):
        block = build_header_block(_bands(), COLS)
        heads = {i: block.for_column(i).qualified_period_text for i in range(3, 9)}
        assert heads == {
            3: "Quarter ended 31-Dec-25", 4: "Quarter ended 30-Sep-25",
            5: "Quarter ended 31-Dec-24", 6: "Nine months ended 31-Dec-25",
            7: "Nine months ended 31-Dec-24", 8: "Year ended crore 31-Mar-25"}
        assert not block.banners

    def test_and_resolves_to_three_durations(self):
        block = build_header_block(_bands(), COLS)
        got = []
        for i in range(3, 9):
            p = parse_period(block.for_column(i).qualified_period_text, calendar=MARCH)
            got.append((p.end_date, str(p.period_type)))
        assert got == [("2025-12-31", "quarter"), ("2025-09-30", "quarter"),
                       ("2024-12-31", "quarter"), ("2025-12-31", "nine_month"),
                       ("2024-12-31", "nine_month"), ("2025-03-31", "fiscal_year")]


class TestACentredGroupHeadGovernsTheColumnsUnderIt:
    def test_a_head_starting_right_of_a_columns_centre_still_governs_it(self):
        cols = [_Col(0, 0, 100), _Col(1, 100, 200), _Col(2, 200, 300),
                _Col(3, 300, 400)]
        bands = [_Band([_el(1, "Quarter ended", 110, 10, 80, 12),
                        _el(2, "Nine months ended", 255, 10, 120, 12)]),
                 _Band([_el(3, "30.06.2026", 110, 30, 80, 12),
                        _el(4, "30.06.2026", 210, 30, 80, 12),
                        _el(5, "30.06.2025", 310, 30, 80, 12)])]
        block = build_header_block(bands, cols)
        # Column 2 (200-300, centre 250) lies under "Nine months ended"
        # (255-375), though the head starts right of its centre.
        assert block.for_column(2).groups == ["Nine months ended"]
        assert block.for_column(3).groups == ["Nine months ended"]
        assert block.for_column(1).groups == ["Quarter ended"]


class TestAPhantomLeadingColumnDoesNotHideTheLabels:
    def _table(self):
        cells = []

        def cell(r, c, text, num=None):
            cells.append(TableCell(
                id=f"c{r}_{c}", table_id="t", document_id="d", page_number=1,
                row=r, col=c, text=text,
                role=CellRole.COLUMN_HEADER if r == 0 else (
                    CellRole.ROW_LABEL if c <= 2 else CellRole.DATA),
                bbox=BBoxModel(x=100 * c, y=20 * r, width=90, height=12),
                confidence=0.9, numeric_value=num,
                is_numeric=num is not None))
        cell(0, 1, "Sr No"); cell(0, 2, "Particulars"); cell(0, 3, "31-Dec-25")
        cell(1, 1, "1", 1.0); cell(1, 2, "Interest earned"); cell(1, 3, "17,506.80", 17506.80)
        cell(2, 2, "(a) Interest/discount on advances/bills"); cell(2, 3, "12,717.93", 12717.93)
        cell(3, 1, "2", 2.0); cell(3, 2, "Other income"); cell(3, 3, "10,343.99", 10343.99)
        q3 = parse_period("Quarter ended 31-Dec-25", calendar=MARCH)
        return ExtractedTable(
            id="t", document_id="d", page_number=1, region_id="r", title="",
            bbox=BBoxModel(x=0, y=0, width=400, height=80), n_rows=4, n_cols=4,
            columns=[TableColumn(index=0, header=""), TableColumn(index=1, header="Sr No"),
                     TableColumn(index=2, header="Particulars"),
                     TableColumn(index=3, header="Quarter ended 31-Dec-25",
                                 normalized_period=q3.label, period_resolved=q3,
                                 numeric_ratio=1.0)],
            cells=cells, confidence=0.9, unit_hint=MagnitudeUnit.CRORE,
            currency_hint=Currency.INR)

    def test_the_serial_column_is_recognised_after_an_empty_one(self):
        assert _label_columns(self._table(), 1) == [1, 2]

    def test_and_the_rows_produce_facts(self):
        ctx = TableFactContext(document_id="d", entity="Consolidated",
                               calendar=MARCH, default_currency=Currency.INR,
                               default_unit=MagnitudeUnit.CRORE)
        facts = extract_from_table(self._table(), ctx)
        assert {f.value for f in facts} >= {17506.80, 10343.99}
