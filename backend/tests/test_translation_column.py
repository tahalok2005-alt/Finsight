"""An Indian filer's statement with a convenience-translation column.

Found in the Phase 21 corpus (an IT services company's IFRS statements). The
statement is in rupee millions -- its declaration's glyph lost to OCR -- and
prints a fourth column "Convenience translation into U.S. Dollar in millions
(unaudited) at the rate of ₹94.66". Two defects of that page:

  * layout cut the head ("Three months ended June 30," over the years) into a
    band of its own, narrower than the statement because it has no label
    column, and the fragment test refused to rejoin it: every figure of the
    statement lost its period;
  * the translation column's words, read as the table's own text, made every
    rupee figure dollars.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.models.enums import Currency, RegionType
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.fragment_identity import assess
from app.tables.structure import build_table

MARCH = FiscalCalendar(year_end_month=3)


def _el(i, text, x, y, w=None, h=26):
    return OCRElement(id=f"e{i}", document_id="d", page_number=1, text=text,
                      confidence=0.95,
                      bbox=BBoxModel(x=x, y=y, width=w or max(30, 14 * len(text)),
                                     height=h))


def _line(i, items):
    els = [_el(100 * i + j, *t) for j, t in enumerate(items)]
    box = BBox.union_all(BBox(e.bbox.x, e.bbox.y, e.bbox.width, e.bbox.height)
                         for e in els)
    return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)


@dataclass
class _Region:
    id: str
    type: RegionType
    box: BBox
    text: str
    meta: dict = field(default_factory=dict)

    @property
    def bbox(self):
        return BBoxModel(x=self.box.x, y=self.box.y, width=self.box.width,
                         height=self.box.height)


class TestAHeadBandOverTheValueColumnsIsRejoined:
    def _pieces(self):
        head = [_line(0, [("Three months ended June 30,", 1013, 291, 353)]),
                _line(1, [("2025", 896, 319, 67), ("2026", 1159, 315, 69),
                          ("2026", 1424, 319, 66)])]
        body = [_line(2, [("Revenues", 70, 470, 120), ("229,941", 860, 470, 110),
                          ("236,103", 1120, 470, 110), ("2,494", 1440, 470, 80)]),
                _line(3, [("Cost of revenues", 70, 500, 200),
                          ("(161,418)", 850, 500, 120), ("(165,772)", 1110, 500, 120),
                          ("(1,751)", 1430, 500, 90)]),
                _line(4, [("Gross profit", 70, 530, 150), ("68,523", 860, 530, 110),
                          ("70,331", 1120, 530, 110), ("743", 1450, 530, 70)])]
        lines = {ln.id: ln for ln in head + body}
        upper = _Region("up", RegionType.TABLE, BBox(896, 291, 1490 - 896, 60),
                        "Three months ended June 30,\n2025 2026 2026",
                        {"line_ids": [ln.id for ln in head]})
        lower = _Region("lo", RegionType.TABLE, BBox(59, 411, 1583 - 59, 1000),
                        "Revenues ...", {"line_ids": [ln.id for ln in body]})
        return upper, lower, lines

    def test_it_is_joined(self):
        upper, lower, lines = self._pieces()
        verdict = assess(upper, lower, between=[], page_height=2339,
                         page_width=1654, lines_by_id=lines)
        assert verdict.merged, verdict.explain()
        assert any(e.code == "head_over_value_columns" for e in verdict.evidence)

    def test_a_band_holding_figures_is_not_a_head(self):
        upper, lower, lines = self._pieces()
        lines["l1"] = _line(1, [("229,941", 896, 319, 90), ("2026", 1159, 315, 69)])
        verdict = assess(upper, lower, between=[], page_height=2339,
                         page_width=1654, lines_by_id=lines)
        assert not verdict.merged


class TestATranslationColumnKeepsItsOwnCurrency:
    def _table(self):
        lines = [
            _line(0, [("Three months ended June 30,", 1013, 291, 353)]),
            _line(1, [("2025", 896, 319, 67), ("2026", 1159, 315, 69),
                      ("2026", 1424, 319, 66)]),
            _line(2, [("Convenience", 1378, 349, 164)]),
            _line(3, [("translation into U.S.", 1333, 379, 243)]),
            _line(4, [("Dollar in millions", 1354, 411, 205)]),
            _line(5, [("Revenues", 70, 470, 120), ("229,941", 860, 470, 110),
                      ("236,103", 1120, 470, 110), ("2,494", 1440, 470, 80)]),
            _line(6, [("Cost of revenues", 70, 500, 200),
                      ("(161,418)", 850, 500, 120), ("(165,772)", 1110, 500, 120),
                      ("(1,751)", 1430, 500, 90)]),
            _line(7, [("Gross profit", 70, 530, 150), ("68,523", 860, 530, 110),
                      ("70,331", 1120, 530, 110), ("743", 1450, 530, 70)]),
        ]
        return build_table(
            document_id="d", page_number=1, region_id="r", lines=lines,
            v_lines=[], page_width=1654, page_height=2339, table_index=0,
            calendar=MARCH, ruled=False, base_confidence=0.7,
            context_text="( in millions, except share and per share data, "
                         "unless otherwise stated)")

    def test_the_table_is_not_dollars(self):
        table = self._table()
        assert table is not None
        assert table.currency_hint != Currency.USD

    def test_the_translation_column_is(self):
        table = self._table()
        scoped = table.meta.get("column_currency") or {}
        assert list(scoped.values()) == ["USD"]
        (index,) = scoped
        assert "Dollar" in next(c.header for c in table.columns
                                if c.index == int(index))
