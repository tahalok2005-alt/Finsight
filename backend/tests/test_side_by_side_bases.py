"""A bank's standalone and consolidated results, side by side on one page.

Found in the Phase 21 corpus (a public-sector bank's typeset results). Eight
value columns: four under "Standalone", four under "Consolidated", each four
split "Quarter ended" (three) and "Year ended" (one). Three defects of the
real page are held here, with the real page's geometry (200 dpi OCR boxes):

  * the line finder returned 24 vertical segments, several of them passing
    straight through figures, and the "ruled" reconstruction produced
    seventeen columns for eight: dates were cut away from the figures under
    them and those figures lost their periods;
  * the group heads are CENTRED over groups of different sizes, and no fixed
    rule (left edge, nearest head) assigns all eight columns correctly -- the
    first consolidated column went to "Standalone", the third quarter column
    to "Year ended";
  * the table band starts at the top of the page, so the masthead and the
    title ("...QUARTERENDEDJUNE30,2026", spaces lost) became group heads and
    their words were prepended to every column's dates.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.geometry import BBox
from app.layout.cv_signals import LineSegment
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar, parse_period
from app.ocr.lines import TextLine
from app.tables.header_block import build_header_block
from app.tables.structure import RowBand, detect_columns

MARCH = FiscalCalendar(year_end_month=3)


def _el(i, text, x, y, w, h):
    return OCRElement(id=f"e{i}", document_id="d", page_number=1, text=text,
                      confidence=0.95, bbox=BBoxModel(x=x, y=y, width=w, height=h))


#: The head as the page prints it: masthead, title, unit, bases, spans, dates.
HEAD = [("STATE BANK OFINDIA", 1055, 61, 278, 25),
        ("CORPORATECENTRE,MUMBAIL400021", 1001, 94, 388, 23),
        ("UNAUDITEDFINANCIALRESULTSFORTHEQUARTERENDEDJUNE30,2026", 830, 122, 728, 27),
        ("( in Crore)", 2056, 157, 95, 24),
        ("Standalone", 1209, 184, 110, 25), ("Consolidated", 1793, 186, 125, 20),
        ("Quarterended", 1131, 210, 118, 20), ("Year ended", 1437, 209, 98, 24),
        ("Quarterended", 1725, 210, 118, 20), ("Year ended", 2032, 209, 97, 24),
        ("Particulars", 563, 217, 106, 25),
        ("30.06.2026", 996, 233, 90, 21), ("31.03.2026", 1145, 233, 89, 21),
        ("30.06.2025", 1292, 229, 92, 25), ("31.03.2026", 1440, 230, 93, 25),
        ("30.06.2026", 1588, 230, 91, 25), ("31.03.2026", 1740, 233, 88, 21),
        ("30.06.2025", 1885, 227, 93, 28), ("31.03.2026", 2032, 229, 93, 25),
        ("(Unaudited)", 993, 255, 97, 20), ("(Audited)", 1149, 249, 81, 30),
        ("(Unaudited)", 1290, 255, 96, 20), ("(Audited)", 1446, 252, 82, 25),
        ("(Unaudited)", 1587, 255, 95, 20), ("(Audited)", 1743, 252, 80, 26),
        ("(Unaudited)", 1885, 255, 96, 20), ("(Audited)", 2041, 255, 78, 20)]

#: Three body rows, right-aligned figures, as printed.
BODY = [
    [("1", 242, 284, 9, 12), ("Interest earned (a)+(b)+(c)+(d)", 260, 275, 254, 27),
     ("1,27,896.46", 999, 277, 110, 25), ("1,23,097.67", 1149, 277, 107, 25),
     ("1,17830.28", 1296, 274, 109, 28), ("4,82,797.17", 1444, 275, 108, 25),
     ("1,36,240.49", 1593, 278, 104, 21), ("1,31,080.12", 1744, 275, 107, 25),
     ("1,25,563.08", 1893, 275, 105, 25), ("5,14,932.59", 2037, 275, 108, 25)],
    [("(a)", 275, 304, 37, 22), ("Interest/discountonadvances/bills", 309, 303, 299, 23),
     ("94,011.38", 1015, 302, 94, 24), ("89.369.91", 1164, 302, 92, 24),
     ("85,437.92", 1312, 302, 93, 24), ("3,50,004.36", 1444, 302, 109, 24),
     ("96.826.98", 1609, 302, 93, 24), ("92.043.47", 1760, 302, 93, 24),
     ("88.146.02", 1906, 299, 95, 29), ("3.61,064.00", 2038, 302, 109, 24)],
    [("(b)", 275, 328, 32, 23), ("Income on investments", 312, 328, 192, 23),
     ("29,510.19", 1013, 320, 97, 33), ("28.579.05", 1162, 323, 96, 29),
     ("27.749.20", 1311, 323, 94, 28), ("1,13,034.49", 1444, 320, 108, 33),
     ("34.531.10", 1607, 323, 96, 29), ("33.399.33", 1760, 323, 94, 29),
     ("32.225.54", 1907, 323, 94, 29), ("1,31,954.32", 2038, 325, 106, 24)],
]

#: Vertical segments as the line finder returned them: genuine rules on the
#: standalone half, and strokes that cut through the consolidated figures.
V_LINES = [228, 264, 966, 1114, 1262, 1410, 1594, 1658, 1678, 1710, 1976, 1984,
           2082, 2098, 2104, 2114, 2126, 2137, 2144]


def _row(i, items):
    els = [_el(100 * i + j, *t) for j, t in enumerate(items)]
    box = BBox.union_all(BBox(e.bbox.x, e.bbox.y, e.bbox.width, e.bbox.height)
                         for e in els)
    return RowBand(index=i, lines=[TextLine(id=f"l{i}", page_number=1,
                                            bbox=box, elements=els)])


def _body_rows():
    return [_row(i, r) for i, r in enumerate(BODY)]


def _band():
    return BBox(228, 60, 2151 - 228, 1164 - 60)


def _segments():
    return [LineSegment(bbox=BBox(x - 1, 60, 2, 1100), orientation="v",
                        length=1100, thickness=2) for x in V_LINES]


class TestRulesThatCrossFiguresAreNotColumns:
    def test_eight_value_columns_not_seventeen(self):
        cols = detect_columns(_body_rows(), _band(), _segments(), page_width=2339)
        value_cols = [c for c in cols if c.index != 0]
        assert len(value_cols) == 8, [(c.x0, c.x1, c.source) for c in cols]

    def test_every_figure_of_a_row_lands_in_its_own_column(self):
        cols = detect_columns(_body_rows(), _band(), _segments(), page_width=2339)
        for row in _body_rows():
            figures = [e for e in row.elements if "," in e.text or "." in e.text]
            homes = set()
            for e in figures:
                right = e.bbox.x1 - 1.0
                homes.add(next(c.index for c in cols if c.contains(right)))
            assert len(homes) == len(figures)


@dataclass
class _Band:
    elements: list
    is_header: bool = False


@dataclass
class _Col:
    index: int
    x0: float
    x1: float


#: The column bands the reconstruction now finds for this page.
COLS = [_Col(0, 228, 982), _Col(1, 982, 1138), _Col(2, 1138, 1286),
        _Col(3, 1286, 1434), _Col(4, 1434, 1581), _Col(5, 1581, 1729),
        _Col(6, 1729, 1880), _Col(7, 1880, 2027), _Col(8, 2027, 2174)]


def _head_block():
    head = _Band([_el(i, *t) for i, t in enumerate(HEAD)])
    first = _Band([_el(900 + j, *t) for j, t in enumerate(BODY[0])])
    return build_header_block([head, first], COLS)


class TestCentredGroupHeadsOfUnequalGroups:
    def test_each_column_has_its_basis(self):
        block = _head_block()
        assert [block.for_column(i).basis for i in range(1, 9)] == \
            ["standalone"] * 4 + ["consolidated"] * 4

    def test_each_column_has_its_span_and_date(self):
        block = _head_block()
        got = []
        for i in range(1, 9):
            p = parse_period(block.for_column(i).qualified_period_text, calendar=MARCH)
            got.append((p.end_date, str(p.period_type)))
        half = [("2026-06-30", "quarter"), ("2026-03-31", "quarter"),
                ("2025-06-30", "quarter"), ("2026-03-31", "fiscal_year")]
        assert got == half + half


class TestTheMastheadIsNotAColumnHead:
    def test_no_masthead_words_reach_a_columns_period_text(self):
        block = _head_block()
        for i in range(1, 9):
            text = block.for_column(i).qualified_period_text.upper()
            assert "STATE BANK" not in text
            assert "CORPORATE" not in text
            assert "UNAUDITEDFINANCIALRESULTS" not in text

    def test_a_title_whose_spaces_were_lost_is_still_a_title(self):
        block = _head_block()
        assert "UNAUDITEDFINANCIALRESULTSFORTHEQUARTERENDEDJUNE30,2026" in block.banners
