"""Page routing: skipping work that was going to be refused anyway.

The claim these tests defend is narrow and it is the whole reason routing is
safe: a page the router skips is a page every spatial detector would have
refused. So the routed pipeline and the unrouted one must find the same facts,
and the only difference is what the run costs.
"""
from __future__ import annotations

import pytest

from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.services.page_routing import (
    MIN_ELEMENTS_FOR_ANYTHING, MIN_FIGURES_FOR_GRID, PagePlan, plan_page,
)
from app.spatial.cards import detect_cards
from app.spatial.rows import detect_rows
from app.spatial.transposed import detect_transposed

MARCH = FiscalCalendar(year_end_month=3)
PAGE_WIDTH = 2667


def el(text: str, x: float, y: float, *, w: float | None = None,
       h: float = 40.0) -> OCRElement:
    return OCRElement(
        id=f"e{int(x)}_{int(y)}_{abs(hash(text)) % 99999}", document_id="d1",
        page_number=1, text=text, confidence=0.92,
        bbox=BBoxModel(x=x, y=y, width=w if w is not None else len(text) * 17.0,
                       height=h))


def prose(n: int) -> list[OCRElement]:
    """A page of narrative: words, no figures."""
    words = ["The", "Group", "continues", "to", "manage", "its", "exposure",
             "to", "credit", "risk", "through", "the", "committee"]
    return [el(words[i % len(words)], 200 + (i % 5) * 300, 200 + (i // 5) * 60)
            for i in range(n)]


class TestPlanning:

    def test_a_page_of_prose_offers_nothing_to_the_spatial_readers(self):
        plan = plan_page(prose(40))
        assert plan.figures == 0
        assert not plan.any_spatial

    def test_a_nearly_empty_page_offers_nothing(self):
        plan = plan_page([el("Appendix", 200, 200)])
        assert plan.elements < MIN_ELEMENTS_FOR_ANYTHING
        assert not plan.any_spatial

    def test_a_page_with_figures_offers_every_reader(self):
        page = prose(20) + [el("US$ 1,000 Mn.", 900, 300),
                            el("US$ 2,000 Mn.", 900, 400),
                            el("US$ 3,000 Mn.", 900, 500),
                            el("US$ 4,000 Mn.", 900, 600)]
        plan = plan_page(page)
        assert plan.figures >= MIN_FIGURES_FOR_GRID
        assert plan.tiles and plan.grid and plan.rows

    def test_two_figures_cannot_fill_the_smallest_lattice(self):
        page = prose(20) + [el("1,000", 900, 300), el("2,000", 900, 400)]
        plan = plan_page(page)
        assert plan.figures == 2
        assert not plan.grid

    def test_the_plan_says_what_it_skipped(self):
        d = plan_page(prose(30)).as_dict()
        assert set(d["spatial_skipped"]) == {"tiles", "grid", "rows"}
        assert d["spatial_offered"] == []


class TestRoutingChangesNothing:
    """A skipped page is one the detectors would have refused."""

    @pytest.mark.parametrize("page", [
        prose(10), prose(40), prose(80),
        [el("Appendix", 200, 200)],
        prose(20) + [el("1,000", 900, 300)],
        prose(20) + [el("1,000", 900, 300), el("2,000", 900, 400)],
    ])
    def test_every_skipped_page_would_have_produced_nothing(self, page):
        plan = plan_page(page)
        if not plan.tiles:
            assert detect_cards(page, page_number=1,
                                page_width=PAGE_WIDTH).cards == []
        if not plan.grid:
            assert detect_transposed(page, page_number=1,
                                     page_width=PAGE_WIDTH).candidates == []
        if not plan.rows:
            assert detect_rows(page, page_number=1,
                               page_width=PAGE_WIDTH).blocks == []

    def test_a_page_that_can_produce_is_never_skipped(self):
        """The routed path must not drop a real KPI grid."""
        tiles: list[OCRElement] = []
        for row, band in enumerate((300, 700)):
            for col, (label, value) in enumerate([
                    ("Total Deposits", "US$ 32,931 Mn."),
                    ("CASA Deposits", "US$ 16,734 Mn."),
                    ("Advances", "US$ 31,447 Mn.")]):
                cx = 400 + col * 700
                tiles.append(el(f"{label}", cx, band, w=380))
                tiles.append(el(f"{value}", cx, band + 60, w=380))
        plan = plan_page(tiles)
        assert plan.tiles
        finding = detect_cards(tiles, page_number=1, page_width=PAGE_WIDTH)
        assert finding.cards, "a real grid must survive routing"
