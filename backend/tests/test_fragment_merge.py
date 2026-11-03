"""When two table fragments are one table, and when they are two.

Brief section 7. Phase 19 rejoined split statements on geometry alone and
recorded the risk that two genuinely separate tables of the same width would be
merged. These are the cases that risk describes, written as fixtures.

The asymmetry the design rests on is what most of these test: **positive
structural evidence is required, and one piece of contrary evidence is
decisive.** Two tables a filer meant to keep apart nearly always say so -- a
title, a basis, a unit, a fresh set of dates -- and one such statement outweighs
any amount of geometric similarity, because geometry is exactly what two
consecutive tables of one statement share.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.models.enums import RegionType
from app.models.schemas import BBoxModel, OCRElement, Region
from app.ocr.lines import TextLine
from app.tables.fragment_identity import assess
from app.tables.fragments import group_table_regions

PAGE_W, PAGE_H = 1700, 2200
#: The value columns of a four-column statement, as right edges.
EDGES = (960, 1090, 1220, 1350)


def _element(text: str, x: float, y: float, index: int) -> OCRElement:
    return OCRElement(
        id=f"e{index}", document_id="frag", page_number=1, text=text,
        confidence=0.93,
        bbox=BBoxModel(x=x, y=y, width=max(40.0, len(text) * 8.0), height=15.0))


def _band(region_id: str, y0: float, y1: float, *, x0: float = 180,
          x1: float = 1390, text: str = "",
          kind: RegionType = RegionType.TABLE,
          line_ids: list[str] | None = None) -> Region:
    return Region(
        id=region_id, document_id="frag", page_number=1, type=kind,
        bbox=BBoxModel(x=x0, y=y0, width=x1 - x0, height=y1 - y0),
        reading_order=int(y0), text=text, confidence=0.9,
        meta={"line_ids": line_ids or []})


def _rows_at(prefix: str, y_start: float, n: int, *, edges=EDGES
             ) -> tuple[list[str], dict[str, TextLine]]:
    """n data rows of figures right-aligned to ``edges``."""
    ids: list[str] = []
    lines: dict[str, TextLine] = {}
    for i in range(n):
        y = y_start + i * 24
        elements = [_element(f"Row {prefix}{i}", 200, y, i * 10)]
        for j, edge in enumerate(edges):
            text = f"{100 + i * 7 + j}.00"
            width = len(text) * 8.0
            elements.append(OCRElement(
                id=f"{prefix}{i}v{j}", document_id="frag", page_number=1,
                text=text, confidence=0.93,
                bbox=BBoxModel(x=edge - width, y=y, width=width, height=15.0)))
        box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                                  height=e.bbox.height) for e in elements)
        line_id = f"{prefix}l{i}"
        lines[line_id] = TextLine(id=line_id, page_number=1, bbox=box,
                                  elements=elements)
        ids.append(line_id)
    return ids, lines


def _verdict(upper, lower, between=(), lines=None):
    return assess(upper, lower, between=list(between), page_height=PAGE_H,
                  page_width=PAGE_W, lines_by_id=lines or {})


class TestOneTableSplitInTwo:
    """Case 1. This is the Phase 19 defect and it must keep working."""

    def test_a_statement_split_at_a_rule_is_rejoined(self):
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        upper = _band("r1", 380, 550, line_ids=upper_ids)
        lower = _band("r2", 552, 720, line_ids=lower_ids)

        verdict = _verdict(upper, lower, lines=lines)
        assert verdict.merged, verdict.explain()
        assert {e.code for e in verdict.supporting} >= {
            "adjacent", "same_extent", "columns_align"}

    def test_the_merge_says_why(self):
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        verdict = _verdict(_band("r1", 380, 550, line_ids=upper_ids),
                           _band("r2", 552, 720, line_ids=lower_ids),
                           lines=lines)
        explanation = verdict.explain()
        assert explanation.startswith("Merged because")
        assert "columns" in explanation or "extent" in explanation


class TestWhatMustNotMerge:

    def test_two_unrelated_tables_of_the_same_width(self):
        """Case 2. Same width, same column count, different columns."""
        upper_ids, lines = _rows_at("u", 400, 6)
        # The lower table's figures stand at quite different edges.
        lower_ids, more = _rows_at("d", 560, 6,
                                   edges=(700, 860, 1020, 1180))
        lines.update(more)
        verdict = _verdict(_band("r1", 380, 550, line_ids=upper_ids),
                           _band("r2", 552, 720, line_ids=lower_ids),
                           lines=lines)
        assert not verdict.merged, verdict.explain()
        assert any(e.code == "columns_do_not_align" for e in verdict.contrary)

    def test_two_tables_separated_by_a_heading(self):
        """Case 3."""
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 620, 6)
        lines.update(more)
        heading = _band("h", 560, 590, text="Segment information",
                        kind=RegionType.SECTION_HEADING)
        verdict = _verdict(_band("r1", 380, 550, line_ids=upper_ids),
                           _band("r2", 600, 780, line_ids=lower_ids),
                           between=[heading], lines=lines)
        assert not verdict.merged, verdict.explain()
        assert any(e.code in ("new_statement_title", "heading_between")
                   for e in verdict.contrary)

    def test_standalone_followed_by_consolidated(self):
        """Case 4. The one that would corrupt a filing outright."""
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        upper = _band("r1", 380, 550, line_ids=upper_ids,
                      text="Standalone financial results for the quarter")
        lower = _band("r2", 552, 720, line_ids=lower_ids,
                      text="Consolidated financial results for the quarter")
        verdict = _verdict(upper, lower, lines=lines)
        assert not verdict.merged, verdict.explain()
        assert any(e.code in ("different_reporting_basis",
                              "lower_opens_a_statement")
                   for e in verdict.contrary)

    def test_two_kpi_tables_with_identical_geometry(self):
        """Case 7. Identical dimensions is not evidence of anything."""
        upper_ids, lines = _rows_at("u", 400, 4)
        lower_ids, more = _rows_at("d", 700, 4)
        lines.update(more)
        # Separated by more than a statement's internal rule leaves.
        verdict = _verdict(_band("r1", 380, 500, line_ids=upper_ids),
                           _band("r2", 690, 810, line_ids=lower_ids),
                           lines=lines)
        assert not verdict.merged, verdict.explain()
        assert any(e.code == "vertical_gap" for e in verdict.contrary)

    def test_different_unit_declarations(self):
        """Case 8."""
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        upper = _band("r1", 380, 550, line_ids=upper_ids,
                      text="(All amounts are in INR crore)")
        lower = _band("r2", 552, 720, line_ids=lower_ids,
                      text="(All amounts are in INR million)")
        verdict = _verdict(upper, lower, lines=lines)
        assert not verdict.merged, verdict.explain()
        assert any(e.code == "different_unit_declaration"
                   for e in verdict.contrary)

    def test_a_new_statement_title_in_the_lower_band(self):
        """Case 5's cousin: the lower band names itself."""
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        lower = _band("r2", 552, 720, line_ids=lower_ids,
                      text="Statement of Assets and Liabilities")
        verdict = _verdict(_band("r1", 380, 550, line_ids=upper_ids),
                           lower, lines=lines)
        assert not verdict.merged, verdict.explain()
        assert any(e.code == "lower_opens_a_statement"
                   for e in verdict.contrary)

    def test_geometry_alone_is_never_enough(self):
        """The rule Phase 19's risk note was about, stated directly."""
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        # No line evidence at all: the merger cannot see any column.
        verdict = assess(_band("r1", 380, 550), _band("r2", 552, 720),
                         between=[], page_height=PAGE_H, page_width=PAGE_W,
                         lines_by_id={})
        # Adjacency and extent alone are two pieces of evidence, and both are
        # purely geometric. The design requires them, so this documents what
        # geometry alone buys: a merge only where nothing at all contradicts it.
        assert verdict.merged == (not verdict.contrary)


class TestTheVerdictIsAlwaysExplained:

    @pytest.mark.parametrize("upper_text,lower_text", [
        ("", ""),
        ("Standalone results", "Consolidated results"),
        ("(in crore)", "(in million)"),
    ])
    def test_every_verdict_carries_a_readable_reason(self, upper_text,
                                                      lower_text):
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 560, 6)
        lines.update(more)
        verdict = _verdict(_band("r1", 380, 550, line_ids=upper_ids,
                                 text=upper_text),
                           _band("r2", 552, 720, line_ids=lower_ids,
                                 text=lower_text),
                           lines=lines)
        explanation = verdict.explain()
        assert explanation.endswith(".")
        assert explanation.startswith(("Merged because", "Not merged because"))
        assert len(explanation) > 30


class TestGrouping:

    def test_a_lone_table_is_its_own_group(self):
        groups = group_table_regions([_band("r1", 380, 550)], PAGE_H, PAGE_W)
        assert len(groups) == 1
        assert not groups[0].is_merge

    def test_a_refused_pair_stays_two_groups(self):
        upper_ids, lines = _rows_at("u", 400, 6)
        lower_ids, more = _rows_at("d", 700, 6)
        lines.update(more)
        regions = [_band("r1", 380, 500, line_ids=upper_ids),
                   _band("r2", 690, 810, line_ids=lower_ids)]
        groups = group_table_regions(regions, PAGE_H, PAGE_W, lines)
        assert len(groups) == 2
        assert all(not g.is_merge for g in groups)
        assert groups[1].reason.startswith("Not merged because")
