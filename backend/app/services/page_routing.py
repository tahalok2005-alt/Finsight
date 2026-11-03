"""Deciding which extractors a page is worth offering to.

Every spatial strategy already refuses a page that cannot support it. The tile
reader needs three tiles in a filled grid; the grid reader needs a lattice at
least two by two; the row reader needs three rows sharing both edges. Each of
those needs figures, and a page without enough figures fails the check *after*
the detector has clustered every element on it.

This module asks the same questions first, from evidence the page already
carries, and lets the pipeline skip the ones whose answer is already no.

Why this is safe
----------------
Nothing here is a heuristic about what a page probably contains. Each gate is a
**necessary condition** the detector itself enforces further down: a page with
three figures on it cannot produce a 2x2 filled lattice, whatever the geometry
does, so running the detector can only end in a refusal. Skipping it changes
what the run costs and not what it finds -- and the tests hold that line by
asserting the routed and unrouted paths agree.

What it is worth
----------------
Measured on a 165-page interim report before any routing: the grid detector
built 3,587 candidates and produced 26 facts, the tile detector found 318 tiles
and produced none, and 105 of the 165 pages produced nothing at all.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import OCRElement
from app.spatial.fragments import is_value

#: A tile grid needs three tiles, each a label and a figure. Below this many
#: figures the grid cannot fill and `detect_cards` refuses it.
MIN_FIGURES_FOR_TILES = 3
#: The smallest lattice `detect_transposed` will accept is two rows by two
#: columns, and it must be at least 70 % filled.
MIN_FIGURES_FOR_GRID = 3
#: A block needs three rows, each carrying one figure.
MIN_FIGURES_FOR_ROWS = 3
#: Below this the page has no financial content to read by any route.
MIN_ELEMENTS_FOR_ANYTHING = 4


@dataclass(frozen=True, slots=True)
class PagePlan:
    """Which strategies this page can support, and why."""
    elements: int
    figures: int
    tiles: bool
    grid: bool
    rows: bool

    @property
    def any_spatial(self) -> bool:
        return self.tiles or self.grid or self.rows

    def as_dict(self) -> dict:
        return {
            "elements": self.elements,
            "figures": self.figures,
            "spatial_offered": [
                name for name, on in (("tiles", self.tiles),
                                      ("grid", self.grid),
                                      ("rows", self.rows)) if on],
            "spatial_skipped": [
                name for name, on in (("tiles", self.tiles),
                                      ("grid", self.grid),
                                      ("rows", self.rows)) if not on],
        }


def plan_page(elements: list[OCRElement]) -> PagePlan:
    """What this page could possibly yield, from what is already recognised.

    Counting figures costs one pass over the page's own elements, which the
    extractors were about to make anyway. Nothing is rendered, nothing is
    recognised, and no image is touched.
    """
    usable = [e for e in elements if e.text and e.text.strip()]
    if len(usable) < MIN_ELEMENTS_FOR_ANYTHING:
        return PagePlan(elements=len(usable), figures=0,
                        tiles=False, grid=False, rows=False)
    figures = sum(1 for e in usable if is_value(e))
    return PagePlan(
        elements=len(usable),
        figures=figures,
        tiles=figures >= MIN_FIGURES_FOR_TILES,
        grid=figures >= MIN_FIGURES_FOR_GRID,
        rows=figures >= MIN_FIGURES_FOR_ROWS,
    )
