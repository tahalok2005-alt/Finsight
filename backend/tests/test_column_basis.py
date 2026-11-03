"""Standalone and consolidated in one table: the basis is a property of the column.

Found in the Phase 21 corpus: a PSU bank prints its standalone and consolidated
results side by side in ONE table -- eight value columns under two group heads,
"Standalone" and "Consolidated", with "Quarter ended" / "Year ended" beneath
each and the same four dates beneath those. Two things went wrong:

  * the header block kept only its topmost group level, so the span words were
    lost and a bare "31.03.2026" column under "Consolidated" was read as the
    fiscal year -- the quarter to that date became an annual figure (12 facts);
  * nothing read the group head as a reporting basis, so every column took the
    page's basis and the standalone quarter was reported as consolidated
    (45 facts, most of them unflagged).

The rules held here are general: a lower level of group heads is kept when it
NESTS inside the level above (a stray wide cell that straddles an upper
boundary is still dropped), and a group head naming a basis governs every
column beneath it.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.extraction.facts import TableFactContext, extract_from_table
from app.models.enums import Currency, EntityType, MagnitudeUnit
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import build_table

MARCH = FiscalCalendar(year_end_month=3)
CENTRES = [400, 520, 640, 760, 880, 1000, 1120, 1240]


def _line(i: int, y: float, items: list[tuple[str, float, float]]) -> TextLine:
    els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=1, text=t,
                      confidence=0.95,
                      bbox=BBoxModel(x=x, y=y, width=w, height=14.0))
           for j, (t, x, w) in enumerate(items)]
    box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                              height=e.bbox.height) for e in els)
    return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)


def _figures(y: float, values: list[str], i: int, label: str) -> TextLine:
    items = [(label, 20.0, 260.0)]
    items += [(v, c - 40.0, 80.0) for v, c in zip(values, CENTRES)]
    return _line(i, y, items)


def _sbi_lines() -> list[TextLine]:
    dates = ["30.06.2026", "31.03.2026", "30.06.2025", "31.03.2026"] * 2
    return [
        _line(0, 10, [("(₹ in Crore)", 1180.0, 100.0)]),
        _line(1, 40, [("Standalone", 350.0, 460.0), ("Consolidated", 830.0, 460.0)]),
        _line(2, 60, [("Quarter ended", 360.0, 330.0), ("Year ended", 725.0, 80.0),
                      ("Quarter ended", 840.0, 330.0), ("Year ended", 1205.0, 80.0)]),
        _line(3, 80, [("Particulars", 20.0, 120.0)]
              + [(d, c - 40.0, 80.0) for d, c in zip(dates, CENTRES)]),
        _figures(110, ["1,27,896.46", "1,23,097.67", "1,17,830.28", "4,82,797.17",
                       "1,36,240.49", "1,31,080.12", "1,25,563.08", "5,14,932.59"],
                 4, "Interest earned"),
        _figures(130, ["15,922.69", "17,314.10", "17,511.28", "68,850.28",
                       "43,821.37", "49,998.90", "41,428.74", "1,94,684.37"],
                 5, "Other income"),
        _figures(150, ["1,43,819.15", "1,40,411.77", "1,35,341.56", "5,51,647.45",
                       "1,80,061.86", "1,81,079.02", "1,66,991.82", "7,09,616.96"],
                 6, "Total income"),
    ]


@pytest.fixture(scope="module")
def sbi_table():
    table = build_table(document_id="d", page_number=1, region_id="r",
                        lines=_sbi_lines(), v_lines=[], page_width=1700,
                        page_height=1200, table_index=0, calendar=MARCH,
                        ruled=True, base_confidence=0.7)
    assert table is not None
    return table


@pytest.fixture(scope="module")
def sbi_facts(sbi_table):
    ctx = TableFactContext(
        document_id="d", entity="Consolidated", entity_id="ent_cons",
        entity_type=EntityType.CONSOLIDATED, calendar=MARCH,
        default_currency=Currency.INR, default_unit=MagnitudeUnit.CRORE,
        basis_entities={
            "standalone": ("Standalone", "ent_sa", EntityType.STANDALONE, "root"),
            "consolidated": ("Consolidated", "ent_cons", EntityType.CONSOLIDATED, "root")})
    return extract_from_table(sbi_table, ctx)


class TestBothGroupLevelsAreKept:
    def test_the_basis_is_recorded_per_column(self, sbi_table):
        bases = sbi_table.meta.get("column_basis", {})
        assert len(bases) == 8
        values = [bases[k] for k in sorted(bases, key=int)]
        assert values == ["standalone"] * 4 + ["consolidated"] * 4

    def test_the_quarter_and_the_year_to_the_same_date_stay_apart(self, sbi_table):
        by_idx = {c.index: c for c in sbi_table.columns}
        data = [c for c in sorted(by_idx.values(), key=lambda c: c.index)
                if c.period_resolved is not None]
        assert len(data) == 8
        kinds = [str(c.period_resolved.period_type) for c in data]
        ends = [c.period_resolved.end_date for c in data]
        assert kinds == ["quarter", "quarter", "quarter", "fiscal_year"] * 2
        assert ends == ["2026-06-30", "2026-03-31", "2025-06-30", "2026-03-31"] * 2


class TestTheColumnDecidesTheEntity:
    def test_standalone_columns_are_standalone(self, sbi_facts):
        total = [f for f in sbi_facts if f.value == 127896.46]
        assert total and total[0].entity == "Standalone"
        assert total[0].entity_id == "ent_sa"

    def test_consolidated_columns_are_consolidated(self, sbi_facts):
        total = [f for f in sbi_facts if f.value == 136240.49]
        assert total and total[0].entity == "Consolidated"

    def test_no_column_is_attributed_to_both(self, sbi_facts):
        by_value = {}
        for f in sbi_facts:
            by_value.setdefault(f.value, set()).add(f.entity)
        assert all(len(v) == 1 for v in by_value.values())


class TestAStrayWideCellIsStillDropped:
    def test_a_lower_cell_straddling_an_upper_boundary_is_not_a_level(self):
        lines = _sbi_lines()
        # A wide note that crosses from the standalone half into the
        # consolidated half is not a second level of the head.
        lines[2] = _line(2, 60, [("Quarter ended", 360.0, 330.0),
                                 ("see note 4 regarding comparatives", 700.0, 300.0)])
        table = build_table(document_id="d", page_number=1, region_id="r",
                            lines=lines, v_lines=[], page_width=1700,
                            page_height=1200, table_index=0, calendar=MARCH,
                            ruled=True, base_confidence=0.7)
        assert table is not None
        assert table.meta.get("column_basis", {}).get("1") == "standalone"
