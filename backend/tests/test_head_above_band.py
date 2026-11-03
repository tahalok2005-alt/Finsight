"""A statement whose head sits above the band layout cut.

Found in the Phase 21 corpus (a public-sector bank's typeset results): the
table band starts at the first row of figures, so the whole head -- "Quarter
ended" / "Year ended", the dates, the audit status -- lies above it. The head
was recovered by walking up from the band, but the walk stopped at once:

  * "(Reviewed)" and "(Audited)", the line nearest the band, did not count as
    head text;
  * the date line begins in the label column ("Particulars"), and a line
    beginning there was discarded whole.

Every figure on the page came out undated. And once recovered, the head must
be read as heads are read: "Quarter ended" is centred over three columns and
overlaps one, so reading it by overlap alone would leave the other two quarter
columns with bare dates -- beside a "Year ended" -- to be read as balances.

Geometry is the real page's (200 dpi OCR boxes).
"""
from __future__ import annotations

from app.core.geometry import BBox
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import build_table

MARCH = FiscalCalendar(year_end_month=3)


def _line(i, items):
    els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=1, text=t,
                      confidence=0.95, bbox=BBoxModel(x=x, y=y, width=w, height=h))
           for j, (t, x, y, w, h) in enumerate(items)]
    box = BBox.union_all(BBox(e.bbox.x, e.bbox.y, e.bbox.width, e.bbox.height)
                         for e in els)
    return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)


HEAD = [
    _line(0, [("Reviewed Standalone Financial Results for the Quarter Ended "
               "30th June 2026", 393, 158, 906, 31)]),
    _line(1, [("(Rs.in crores)", 1396, 170, 146, 24)]),
    _line(2, [("Quarter ended", 980, 190, 148, 26), ("Year ended", 1390, 194, 114, 21)]),
    _line(3, [("S..", 161, 202, 40, 28), ("Particulars", 435, 213, 113, 25),
              ("30.06.2026", 801, 213, 116, 25), ("31.03.2026", 996, 216, 115, 21),
              ("30.06.2025", 1193, 213, 115, 25), ("31.03.2026", 1387, 213, 117, 25)]),
    _line(4, [("(Reviewed)", 801, 237, 117, 25), ("(Audited)", 1004, 234, 103, 29),
              ("(Reviewed)", 1194, 238, 115, 21), ("(Audited)", 1396, 234, 101, 25)]),
]
BODY = [
    _line(10, [("1", 171, 273, 24, 27), ("Interest earned (a)+(b)+(c)+(d)", 220, 270, 362, 31),
               ("18090.16", 837, 269, 116, 29), ("17 480.39", 1035, 270, 115, 30),
               ("16282.71", 1231, 272, 114, 25), ("67 450.71", 1426, 269, 116, 29)]),
    _line(11, [("(a) Interest/ discount on advances/bills", 221, 323, 390, 25),
               ("13 123.41", 849, 320, 104, 29), ("12 702.39", 1043, 317, 110, 33),
               ("11 737.80", 1243, 320, 106, 29), ("48 853.54", 1438, 319, 106, 29)]),
    _line(12, [("(b) Income on investments", 221, 360, 250, 25),
               ("4 250.11", 860, 360, 93, 29), ("4 160.02", 1057, 360, 96, 29),
               ("3 990.44", 1253, 360, 96, 29), ("16 402.90", 1440, 360, 104, 29)]),
]


def _table():
    return build_table(document_id="d", page_number=1, region_id="r",
                       lines=BODY, v_lines=[], page_width=1654, page_height=2339,
                       table_index=0, calendar=MARCH, ruled=False,
                       base_confidence=0.7, page_lines=HEAD + BODY)


def test_every_column_is_dated_with_its_span():
    table = _table()
    assert table is not None
    got = [(c.period_resolved.end_date, str(c.period_resolved.period_type))
           if c.period_resolved else None
           for c in table.columns if c.x_start >= 800]   # the value columns
    assert got == [("2026-06-30", "quarter"), ("2026-03-31", "quarter"),
                   ("2025-06-30", "quarter"), ("2026-03-31", "fiscal_year")]


def test_the_title_above_is_not_part_of_any_head():
    table = _table()
    for c in table.columns:
        assert "Reviewed Standalone" not in (c.header or "")


class TestAHeadPrintedOverEachColumnIsThatColumnsOwn:
    """A steel maker prints "Quarter ended" over EACH quarter column and
    "Financial year / ended on / 31.03.2026" over the year column. Read as
    group heads -- three heads, four columns -- the year column lost its span
    and its figures became balances "as at" 31 March."""

    def test_the_year_column_keeps_its_span(self):
        from dataclasses import dataclass
        from app.normalization.periods import parse_period
        from app.tables.header_block import build_header_block

        @dataclass
        class Band:
            elements: list
            is_header: bool = False

        @dataclass
        class Col:
            index: int
            x0: float
            x1: float

        def el(i, t, x, y, w, h):
            return OCRElement(id=f"t{i}", document_id="d", page_number=1, text=t,
                              confidence=0.95,
                              bbox=BBoxModel(x=x, y=y, width=w, height=h))

        head = [("Crore", 1395, 313, 75, 33), ("Financial year", 1327, 339, 129, 33),
                ("Quarter ended", 886, 356, 124, 25), ("Quarter ended", 1034, 356, 123, 25),
                ("Quarter ended", 1181, 356, 122, 25), ("Particulars", 237, 367, 99, 25),
                ("ended on", 1351, 369, 83, 25), ("on 30.06.2026", 887, 381, 120, 24),
                ("on 31.03.2026", 1035, 381, 119, 24), ("on 30.06.2025", 1182, 381, 120, 24),
                ("31.03.2026", 1343, 391, 97, 25), ("Audited", 1206, 429, 72, 26),
                ("Audited", 1355, 429, 74, 26), ("Audited", 911, 430, 74, 27),
                ("(refer note 7)", 1038, 430, 113, 25)]
        first = [("Revenue from operations", 237, 470, 250, 25),
                 ("52,468.39", 900, 470, 105, 25), ("55,081.36", 1050, 470, 105, 25),
                 ("51,200.40", 1195, 470, 105, 25), ("2,11,904.11", 1340, 470, 120, 25)]
        # The bands the reconstruction really found: the boundary between
        # columns 3 and 4 falls 2.5px inside "Financial year", and the one
        # between 1 and 2 inside the middle "Quarter ended".
        cols = [Col(0, 230.0, 877.0), Col(1, 877.0, 1036.9), Col(2, 1036.9, 1185.2),
                Col(3, 1185.2, 1329.5), Col(4, 1353.0, 1484.8)]
        block = build_header_block(
            [Band([el(i, *t) for i, t in enumerate(head)]),
             Band([el(100 + i, *t) for i, t in enumerate(first)])], cols)
        got = []
        for i in range(1, 5):
            p = parse_period(block.for_column(i).qualified_period_text, calendar=MARCH)
            got.append((p.end_date, str(p.period_type)) if p else None)
        assert got == [("2026-06-30", "quarter"), ("2026-03-31", "quarter"),
                       ("2025-06-30", "quarter"), ("2026-03-31", "fiscal_year")]


class TestTheYearColumnsOwnWordsDecideItsSpan:
    """Two more ways the same year column lost its span."""

    def test_a_borrowed_span_does_not_override_the_heads_own(self):
        from app.normalization.periods import parse_period
        # The table's head block mentions only "Quarter ended", so a quarter
        # span is offered to every column; this column says otherwise.
        p = parse_period("Crore Financial year ended on 31.03.2026",
                         calendar=MARCH, default_span="quarter")
        assert (p.end_date, str(p.period_type)) == ("2026-03-31", "fiscal_year")

    def test_a_span_phrase_printed_over_two_lines(self):
        from app.normalization.periods import parse_period_levels
        p = parse_period_levels(["Crore", "Financial year", "ended on",
                                 "31.03.2026", "Audited"], calendar=MARCH)
        assert (p.end_date, str(p.period_type)) == ("2026-03-31", "fiscal_year")
