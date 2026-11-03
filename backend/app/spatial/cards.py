"""KPI card detection from recognised text geometry.

An investor presentation states its headline figures in tiles:

    ┌──────────────────────┐
    │ Profit After Tax     │   label
    │ (Q1-FY27)            │   period, sometimes
    │ US$ 114 Mn.          │   value
    │ (24% YoY | 8% QoQ)   │   change, sometimes
    └──────────────────────┘

There is no grid, no ruling and no header row, so table reconstruction has
nothing to work with. What there is instead is geometry: the parts of a card
share a horizontal centre, sit within a line or two of each other, and the
whole arrangement repeats across the page.

This module finds those tiles and says what evidence it found. It does not
decide what they mean -- ``spatial/extract.py`` does that, through the same
taxonomy and number normalisation every other strategy uses.

**A candidate is not a fact.** Detection is deliberately permissive about
shape and strict about content: a run must contain exactly one value and at
least one label-ish line, or it is not offered at all.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.models.schemas import OCRElement
from app.spatial.fragments import (
    is_change, is_label, is_page_heading, is_period, is_value,
)
from app.spatial.geometry import (
    Column, Lattice, Run, aligned_across, box_of, cluster_columns,
    columns_are_well_separated, lattice_of, median_line_height,
    split_into_runs, typical_element_width, vertical_gaps,
)

#: A column's centre tolerance, as a multiple of the median line height. Card
#: parts are centred on each other but never exactly, because each is a
#: different length and OCR boxes are tight to the ink.
_COLUMN_TOLERANCE_LINES = 2.6
#: Where one card ends and the next begins, as a multiple of the median line
#: height. Within a card the gaps are line-sized.
_RUN_GAP_LINES = 2.2
#: A tile longer than this is a paragraph or a list, not a card.
_MAX_TILE_ELEMENTS = 5
#: Where one tile ends and the next begins, as a multiple of the layout's own
#: median line gap. A designer puts visibly more air between groups than
#: within them, so the boundary scales with the page rather than with pixels.
_RUN_GAP_MEDIANS = 1.7
#: Below this many cards, the repetition evidence is absent and the reading is
#: a guess. A single label above a single number is not a KPI grid.
_MIN_CARDS_FOR_A_GRID = 3
#: An element wider than this share of the page spans the layout rather than
#: sitting inside it: a slide title, a banner, a line of prose. Such an element
#: belongs to no tile, and leaving it in drags a column's centre across its
#: neighbours.
_BANNER_WIDTH_RATIO = 0.30


@dataclass(slots=True)
class CardCandidate:
    """One tile, with the parts identified and the evidence for the reading."""
    page_number: int
    bbox: BBox
    label: OCRElement
    value: OCRElement
    period: OCRElement | None = None
    change: OCRElement | None = None
    #: Everything in the run, in reading order, for provenance.
    elements: list[OCRElement] = field(default_factory=list)
    #: Named, measurable reasons this was accepted. Never a bare number.
    evidence: dict[str, float | int | bool | str] = field(default_factory=dict)

    @property
    def label_text(self) -> str:
        return self.label.text.strip()

    @property
    def value_text(self) -> str:
        return self.value.text.strip()

    def element_ids(self) -> list[str]:
        return [e.id for e in self.elements]


@dataclass(slots=True)
class GridFinding:
    """What was found on one page, and what it cost to decide."""
    page_number: int
    cards: list[CardCandidate] = field(default_factory=list)
    columns: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Page-spanning text set aside before clustering: titles, banners and any
    #: prose that runs the width of the page. Not part of any tile.
    banners: list[OCRElement] = field(default_factory=list)
    #: Text that reads as a heading *over* the tiles: above them, at least as
    #: wide as one of them, and not a sentence. Only these may lend a period.
    headings: list[OCRElement] = field(default_factory=list)
    #: Elements skipped because a reconstructed table already covers them.
    excluded_by_tables: int = 0
    #: The grid the surviving tiles occupy, measured rather than assumed.
    lattice: Lattice | None = None

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


#: A heading over a layout is at least this wide relative to the layout's own
#: cells. It is the test that separates a slide's title from the date stamp in
#: the corner beside it, which is narrow and governs nothing.
_HEADING_WIDTH_VS_TILE = 1.0


def _headings_over(elements: list[OCRElement],
                   cards: list["CardCandidate"]) -> list[OCRElement]:
    """Text that heads the tiles, in reading order.

    Three tests, and each rules out something real that was observed:

    * **it reads as a heading, not a sentence.** A footnote reading "In Q1 FY27
      the Bank received a CGFMU claim of US$ 54 Mn." is prose about one figure;
      taking its quarter as the page's gave a bar from a different year a
      period it never had.
    * **it sits above the tiles.** A period printed inside a tile belongs to
      that tile. A period below them belongs to whatever is below them.
    * **it is at least as wide as a tile.** A slide's title spans its layout;
      the "Jun-26" stamp in the top corner spans nothing, and letting it win on
      height alone would replace the stated quarter with a date derived from a
      page decoration.
    """
    if not cards:
        return []
    top = min(card.bbox.y for card in cards)
    minimum = (statistics.median([card.bbox.width for card in cards])
               * _HEADING_WIDTH_VS_TILE)
    over = [e for e in elements
            if box_of(e).y1 <= top
            and box_of(e).width >= minimum
            and is_page_heading(e)]
    return sorted(over, key=lambda e: (box_of(e).y, box_of(e).x))


def detect_cards(elements: list[OCRElement], *, page_number: int,
                 page_width: int,
                 occupied: list[BBox] | None = None) -> GridFinding:
    """Find KPI tiles on one page.

    The order of the checks is the order of the evidence: cluster into
    columns, refuse if the columns are not genuinely separate, split each
    column where the whitespace says a tile ends, then keep only the runs that
    hold exactly one figure and something that names it.

    ``occupied`` is the ground another strategy has already read -- the
    bounding boxes of reconstructed tables. Text inside them is excluded.
    A figure that a table has explained must not be explained a second time
    from its position on the page: on a degraded statement scan that produced
    "Revenue = 2" and "EBITDA = 4" out of stray fragments, which then stood
    beside the real figures as though they were alternatives to them.
    """
    finding = GridFinding(page_number=page_number)
    usable = [e for e in elements if e.text and e.text.strip()]
    if occupied:
        before = len(usable)
        usable = [e for e in usable
                  if not any(b.contains_ratio(box_of(e)) > 0.5
                             for b in occupied)]
        finding.excluded_by_tables = before - len(usable)
    if len(usable) < 4:
        finding.notes.append("too few recognised elements to form a layout")
        return finding

    # Page-spanning text is set aside before clustering. It is still available
    # to the page-level context (a banner may state the period for everything
    # below it) but it is not part of any tile.
    banner_limit = page_width * _BANNER_WIDTH_RATIO
    banners = [e for e in usable if e.bbox.width > banner_limit]
    tile_elements = [e for e in usable if e.bbox.width <= banner_limit]
    finding.banners = banners
    if len(tile_elements) < 4:
        finding.notes.append("nothing left after page-spanning text was set aside")
        return finding

    line_height = median_line_height(tile_elements)
    element_width = typical_element_width(tile_elements)
    columns = cluster_columns(
        tile_elements, tolerance=line_height * _COLUMN_TOLERANCE_LINES)
    finding.columns = len(columns)

    if not columns_are_well_separated(columns, typical_width=element_width):
        finding.notes.append(
            "column clusters overlap horizontally, so the page is not laid "
            "out in separate tiles")
        return finding

    # A page's own gap distribution decides where tiles end.
    #
    # The median gap is the layout's ordinary line spacing. A designer puts
    # visibly more air between groups than within them, so the gap that
    # separates one tile from the next is a multiple of that spacing rather
    # than any particular number of pixels. Measured on a real slide: gaps
    # inside a tile ran 0-92 px and gaps between tiles 120-156 px, against a
    # median of 60.
    gaps = [g for col in columns for g in vertical_gaps(col)]
    gap_limit = line_height * _RUN_GAP_LINES
    if gaps:
        ordered = sorted(gaps)
        median_gap = ordered[len(ordered) // 2]
        gap_limit = max(line_height * 1.5, median_gap * _RUN_GAP_MEDIANS)

    runs: list[Run] = []
    for column in columns:
        runs.extend(split_into_runs(column, gap_limit=gap_limit))

    aligned = aligned_across(runs, tolerance=line_height * 1.5)

    # A tile is believed because the layout repeats it. One label above one
    # number is a guess -- on a chart page it is usually a legend entry beside
    # an axis value. A run that starts level with a run in another column is
    # part of a grid, and that is the evidence being required here.
    #
    # This is what separates a results slide's KPI band from the scattered text
    # of a chart: the slide puts its tiles on a line, the chart does not.
    tops_by_column: list[tuple[Run, float]] = [(r, r.top) for r in runs]
    for run in runs:
        siblings = sum(
            1 for other, top in tops_by_column
            if other is not run and abs(top - run.top) <= line_height * 1.5)
        if siblings == 0:
            finding.reject("tile_not_repeated_across_the_layout")
            continue
        for card in _cards_from_run(run, page_number=page_number,
                                    finding=finding):
            card.evidence["sibling_tiles_on_the_same_line"] = siblings
            finding.cards.append(card)

    if len(finding.cards) < _MIN_CARDS_FOR_A_GRID:
        finding.notes.append(
            f"only {len(finding.cards)} tile(s) found; a repeated layout is "
            f"what makes a label-above-value reading trustworthy")
        finding.rejected["no_repeated_layout"] = len(finding.cards)
        finding.cards = []
        return finding

    # ---- the tiles must actually form a grid --------------------------------
    #
    # Counting tiles is not the same as finding a layout. On a six-panel chart
    # page the reader assembled four "tiles" out of chart titles, axis ticks and
    # bar data labels: enough of them to pass the count, scattered across a 3x2
    # lattice filling four of its six cells. One of the four then reported a
    # bar from the wrong year as the current figure.
    #
    # A designer's KPI band fills its grid. That is the difference, it is
    # measurable, and it is what is required here -- so a page whose tiles do
    # not sit in a complete grid is refused whatever the count.
    finding.lattice = lattice_of([c.bbox for c in finding.cards],
                                 tolerance=line_height * 3.0)
    if not finding.lattice.is_a_grid:
        finding.notes.append(
            f"the {len(finding.cards)} tile(s) occupy "
            f"{finding.lattice.rows}x{finding.lattice.columns} positions and "
            f"fill {finding.lattice.filled:.0%} of them; a KPI band fills its "
            f"grid, so this is scattered text rather than a layout")
        finding.rejected["tiles_do_not_form_a_grid"] = len(finding.cards)
        finding.cards = []
        return finding

    finding.headings = _headings_over(usable, finding.cards)

    # The repetition is recorded on every card: it is the reason each of them
    # is believed, and it belongs in the evidence rather than in a comment.
    for card in finding.cards:
        card.evidence["cards_on_page"] = len(finding.cards)
        card.evidence["columns_on_page"] = len(columns)
        card.evidence["runs_aligned_with_a_sibling"] = aligned
        card.evidence["grid_rows"] = finding.lattice.rows
        card.evidence["grid_columns"] = finding.lattice.columns
        card.evidence["grid_filled"] = round(finding.lattice.filled, 3)
        card.evidence["page_width"] = page_width
    return finding


def _segment_run(elements: list[OCRElement]) -> list[list[OCRElement]]:
    """Cut a vertical run into one segment per figure.

    A tile states one number. Where whitespace alone leaves two tiles joined --
    a footnote between them sits at an ordinary line gap, so nothing separates
    them visually -- the figures themselves mark the boundary.

    Everything accumulated before a figure describes it. Everything after it,
    up to the next descriptive line, qualifies it: that is where a movement
    note like "(24% YoY)" belongs.
    """
    segments: list[list[OCRElement]] = []
    pending: list[OCRElement] = []
    for element in elements:
        if is_value(element):
            pending.append(element)
            segments.append(pending)
            pending = []
        elif is_change(element) and segments and not pending:
            segments[-1].append(element)
        else:
            pending.append(element)
    if pending and segments:
        # A trailing tail with no figure of its own is not a tile; it belongs
        # to nothing and is dropped rather than attached to the tile above.
        pass
    return segments


def _cards_from_run(run: Run, *, page_number: int,
                    finding: GridFinding) -> list[CardCandidate]:
    """Read every tile in one vertical run, explaining what was refused."""
    out: list[CardCandidate] = []
    if len(run.elements) < 2:
        finding.reject("run_too_short")
        return out

    for segment in _segment_run(run.elements):
        if len(segment) < 2:
            finding.reject("figure_with_nothing_naming_it")
            continue
        if len(segment) > _MAX_TILE_ELEMENTS:
            finding.reject("tile_too_long_to_read_confidently")
            continue

        value = next((e for e in segment if is_value(e)), None)
        if value is None:
            finding.reject("no_value_in_tile")
            continue

        periods = [e for e in segment if e is not value and is_period(e)]
        changes = [e for e in segment if e is not value and is_change(e)]
        labels = [e for e in segment
                  if e is not value and e not in periods and e not in changes
                  and is_label(e)]
        if not labels:
            finding.reject("no_label_in_tile")
            continue

        # The label nearest the figure wins. A tile that also caught the line
        # above it -- a footnote, a section heading the run reached -- must not
        # let that further line name the number.
        value_box = box_of(value)
        label = min(labels, key=lambda e: abs(box_of(e).cy - value_box.cy))

        box = BBox.union_all(box_of(e) for e in segment)
        if box is None:
            finding.reject("no_geometry")
            continue

        label_box = box_of(label)
        out.append(CardCandidate(
            page_number=page_number, bbox=box, label=label, value=value,
            period=(periods[0] if periods else None),
            change=(changes[0] if changes else None),
            elements=list(segment),
            evidence={
                "elements_in_tile": len(segment),
                "label_above_value": label_box.cy < value_box.cy,
                "label_value_gap_px": round(abs(value_box.y - label_box.y1), 1),
                "centre_offset_px": round(abs(label_box.cx - value_box.cx), 1),
                "other_labels_in_tile": len(labels) - 1,
                "period_inside_tile": periods[0].text.strip() if periods else "",
                "ocr_confidence_min": round(
                    min(e.confidence for e in segment), 4),
            }))
    return out
