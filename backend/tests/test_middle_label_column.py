"""Row labels printed in the middle of a statement, figures on both sides.

Found in the Phase 21 corpus (a consumer-goods company's consolidated
results): the two June quarters stand left of "Particulars", the March quarter
and the year to its right. Everything assumed the labels were the first
column, so the June 2026 figures were read as every row's label, and a page
of 178 facts produced none.
"""
from __future__ import annotations

from app.core.geometry import BBox
from app.extraction.facts import TableFactContext, extract_from_table
from app.models.enums import Currency, MagnitudeUnit
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import build_rows, build_table, detect_columns
from app.tables.structure import _label_column_index

MARCH = FiscalCalendar(year_end_month=3)


def _line(i, items):
    els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=1, text=t,
                      confidence=0.95, bbox=BBoxModel(x=x, y=y, width=w, height=h))
           for j, (t, x, y, w, h) in enumerate(items)]
    box = BBox.union_all(BBox(e.bbox.x, e.bbox.y, e.bbox.width, e.bbox.height)
                         for e in els)
    return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)


# Rows as the page prints them (200 dpi boxes), label in the middle.
BODY = [
    _line(1, [("17,149", 316, 461, 53, 23), ("15,552", 455, 461, 56, 23),
              ("Sale of products", 556, 465, 111, 19),
              ("16,172", 1271, 461, 53, 23), ("63,636", 1408, 461, 55, 23)]),
    _line(2, [("157", 336, 501, 33, 21), ("178", 477, 501, 32, 21),
              ("Other operating revenue", 554, 499, 169, 22),
              ("144", 1291, 501, 34, 21), ("705", 1430, 501, 33, 21)]),
    _line(3, [("188", 338, 520, 31, 20), ("201", 475, 520, 34, 22),
              ("Other income", 533, 517, 105, 23),
              ("264", 1290, 520, 35, 20), ("751", 1430, 520, 33, 22)]),
    _line(4, [("17,529", 316, 538, 55, 24), ("15,958", 456, 539, 55, 23),
              ("TOTAL INCOME", 534, 536, 121, 24),
              ("16,615", 1269, 536, 57, 28), ("65,219", 1408, 539, 56, 23)]),
    _line(5, [("5,429", 321, 573, 50, 25), ("5,264", 463, 575, 45, 20),
              ("Cost of materials consumed", 554, 575, 192, 23),
              ("5,205", 1278, 575, 46, 20), ("20,981", 1407, 573, 56, 24)]),
]


def test_the_label_column_is_found_in_the_middle():
    rows = build_rows(BODY)
    band = BBox.union_all(ln.bbox for ln in BODY)
    cols = detect_columns(rows, band, [], 1654)
    index = _label_column_index(rows, cols)
    label_col = next(c for c in cols if c.index == index)
    assert label_col.x0 <= 556 <= label_col.x1


def test_a_first_column_of_labels_is_still_the_first_column():
    rows = build_rows([
        _line(1, [("Revenue from operations", 60, 100, 200, 20),
                  ("1,000", 400, 100, 60, 20), ("900", 520, 100, 40, 20)]),
        _line(2, [("Other income", 60, 130, 120, 20),
                  ("50", 420, 130, 30, 20), ("45", 530, 130, 30, 20)]),
        _line(3, [("Total income", 60, 160, 120, 20),
                  ("1,050", 400, 160, 60, 20), ("945", 520, 160, 40, 20)]),
    ])
    band = BBox(60, 100, 500, 80)
    cols = detect_columns(rows, band, [], 1654)
    assert _label_column_index(rows, cols) == 0


def test_every_row_reads_its_figures_on_both_sides():
    table = build_table(document_id="d", page_number=1, region_id="r",
                        lines=BODY, v_lines=[], page_width=1654, page_height=2339,
                        table_index=0, calendar=MARCH, ruled=False,
                        base_confidence=0.7)
    assert table is not None
    ctx = TableFactContext(document_id="d", entity="Consolidated", calendar=MARCH,
                           default_currency=Currency.INR,
                           default_unit=MagnitudeUnit.CRORE)
    facts = extract_from_table(table, ctx)
    by_metric = {}
    for f in facts:
        by_metric.setdefault(f.canonical_metric, set()).add(f.value)
    assert by_metric.get("sale_of_products") == {17149.0, 15552.0, 16172.0, 63636.0}
    assert by_metric.get("total_income") == {17529.0, 15958.0, 16615.0, 65219.0}
    assert by_metric.get("raw_material_cost") == {5429.0, 5264.0, 5205.0, 20981.0}
    # no figure was taken for a label
    assert not any(f.metric.strip()[:1].isdigit() for f in facts)
