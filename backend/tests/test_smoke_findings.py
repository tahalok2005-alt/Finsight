"""Defects found by the Phase 21 smoke runs of documents with no golden table.

Standard Chartered's half-year report (a UK bank, dollars, IFRS) and IDFC
FIRST Bank's deck were re-run at the end of the phase to check nothing had
broken a shape no fix targeted. Reading their output turned up these, each
older than Phase 21 and each general:

  * "Change¹ %" -- a footnote marker on a column head -- was read "Changel %",
    missed as a change column, and a change in the total capital ratio
    ("47bps") was reported as the ratio itself;
  * "Leverage ratio" was an alias of net debt to EBITDA. A bank's leverage
    ratio is Basel III Tier 1 capital over exposure, printed in %; an NBFC's
    is assets or debt over equity. Both were reported as net debt to EBITDA.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.extraction.facts import (
    TableFactContext, _column_qualifier, extract_from_table,
)
from app.extraction.taxonomy import match_metric
from app.models.enums import Currency, EntityType, MagnitudeUnit
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import GROWTH_HEADER_RE, build_table


class TestAFootnoteMarkerOnAChangeHead:
    @pytest.mark.parametrize("head", ["Changel %", "Change1 %", "Change¹ %",
                                      "Change %", "Growth"])
    def test_is_still_a_change_column(self, head):
        assert GROWTH_HEADER_RE.search(head)

    @pytest.mark.parametrize("head", ["Exchange rate", "H126 H125", "30 June 2026"])
    def test_other_heads_are_not(self, head):
        assert not GROWTH_HEADER_RE.search(head)


class TestANameThatMeansDifferentMeasuresToDifferentFilers:
    @pytest.mark.parametrize("label", ["Leverage ratio", "Leverage ratio (%)",
                                       "UK leverage ratio (%)"])
    def test_is_not_guessed(self, label):
        m = match_metric(label)
        assert m is None or m.key != "net_debt_to_ebitda"

    def test_the_unambiguous_name_still_matches(self):
        m = match_metric("Net debt to EBITDA")
        assert m is not None and m.key == "net_debt_to_ebitda"


class TestAColumnThatNamesASegment:
    """Standard Chartered, page 14: a segment table whose columns are the
    bank's businesses, "$million" beneath each (read "Smillion"). The
    segments' figures were attributed to the consolidated group."""

    @pytest.mark.parametrize("head,named", [
        ("Retail Banking Smillion", "retail banking"),
        ("Central & other items Smilion", "central other items"),
        ("Level1 Smillion", "level"),
        ("Total Smillion", None),          # the group's total
        ("Reported Smillion", None),       # the group's, as reported
        ("Adjusted! Smillion", None),
        ("Adjustedi Smillion", None),      # "Adjusted¹"
        ("Year ended 31.03.2026 Audited", None),
        ("(Rs. in crore)", None),
        ("Standalone", None),
    ])
    def test_what_the_head_names(self, head, named):
        assert _column_qualifier(head) == named

    @staticmethod
    def _table():
        def line(i, y, items):
            els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=1,
                              text=t, confidence=0.95,
                              bbox=BBoxModel(x=x, y=y, width=w, height=14.0))
                   for j, (t, x, w) in enumerate(items)]
            box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                                      height=e.bbox.height) for e in els)
            return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)

        centres = [520, 700, 880, 1060]
        heads = ["Retail Banking", "Investment Banking", "Central & other items",
                 "Total"]
        rows = [("Profit/(loss) before taxation", ["1,989", "3,296", "(442)", "4,843"]),
                ("Total liabilities", ["250,464", "546,415", "119,298", "937,513"])]
        lines = [line(0, 40, [(h, c - 70.0, 140.0) for h, c in zip(heads, centres)]),
                 line(1, 60, [("Smillion", c - 40.0, 80.0) for c in centres])]
        for i, (label, values) in enumerate(rows, start=2):
            lines.append(line(i, 60 + 25 * i, [(label, 20.0, 300.0)]
                              + [(v, c - 40.0, 80.0) for v, c in zip(values, centres)]))
        return build_table(document_id="d", page_number=14, region_id="r",
                           lines=lines, v_lines=[], page_width=1300,
                           page_height=1000, table_index=0,
                           calendar=FiscalCalendar(year_end_month=12),
                           ruled=False, base_confidence=0.7)

    def test_a_segment_figure_is_not_the_groups(self):
        table = self._table()
        assert table is not None
        ctx = TableFactContext(
            document_id="d", entity="Consolidated", entity_id="ent_cons",
            entity_type=EntityType.CONSOLIDATED,
            calendar=FiscalCalendar(year_end_month=12),
            default_currency=Currency.USD, default_unit=MagnitudeUnit.MILLION)
        facts = {f.value: f for f in extract_from_table(table, ctx)}
        retail = facts.get(250464.0)
        assert retail is not None
        assert retail.entity is None and retail.entity_id is None
        assert retail.meta.get("column_qualifier") == "retail banking"
        total = facts.get(937513.0)
        assert total is not None and total.entity == "Consolidated"
