"""Geometric grouping over recognised text elements.

Everything here works on the bounding boxes OCR already produced. No pixels are
re-read and no image is re-rendered: the geometry is evidence the pipeline
already has and has so far only used to build tables.

The two operations a spatial layout needs are here, and nothing else:

* **columns** -- elements whose horizontal centres agree, which is what makes a
  card a card rather than a run of unrelated lines that happen to be near each
  other;
* **runs** -- consecutive elements within a column separated by ordinary line
  gaps, broken where the gap is much larger than usual.

Both are measured against the page's own spacing rather than absolute pixel
counts, because a slide at 200 dpi and a statement at 200 dpi have very
different line heights and neither is wrong.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.models.schemas import OCRElement


@dataclass(slots=True)
class Column:
    """Elements sharing a horizontal centre, ordered down the page."""
    centre: float
    elements: list[OCRElement] = field(default_factory=list)

    @property
    def bbox(self) -> BBox | None:
        return BBox.union_all(BBox.from_dict(e.bbox.model_dump())
                              for e in self.elements)

    @property
    def width(self) -> float:
        box = self.bbox
        return box.width if box else 0.0


@dataclass(slots=True)
class Run:
    """A vertical group of elements inside one column, tightly spaced."""
    elements: list[OCRElement]

    @property
    def bbox(self) -> BBox | None:
        return BBox.union_all(BBox.from_dict(e.bbox.model_dump())
                              for e in self.elements)

    @property
    def top(self) -> float:
        return min(e.bbox.y for e in self.elements)


def box_of(element: OCRElement) -> BBox:
    return BBox.from_dict(element.bbox.model_dump())


def median_line_height(elements: list[OCRElement]) -> float:
    """The page's own idea of a line, used to scale every other threshold."""
    heights = [e.bbox.height for e in elements if e.bbox.height > 0]
    return statistics.median(heights) if heights else 1.0


def cluster_columns(elements: list[OCRElement], *,
                    tolerance: float) -> list[Column]:
    """Group elements whose horizontal centres agree within ``tolerance``.

    Centre rather than left edge: a card's label, its value and its change note
    are usually centred on each other but rarely share a left edge, because
    each is a different length. Clustering on the left edge splits one card
    into three.

    The clustering is single-pass over centre-sorted elements, and a cluster's
    centre is the running mean of what it holds. That is enough for a layout
    whose columns are far apart relative to their own width, which is the only
    case this is trusted for -- ``columns_are_well_separated`` is the check.
    """
    if not elements:
        return []
    ordered = sorted(elements, key=lambda e: box_of(e).cx)
    columns: list[Column] = []
    for element in ordered:
        cx = box_of(element).cx
        if columns and abs(cx - columns[-1].centre) <= tolerance:
            col = columns[-1]
            col.elements.append(element)
            col.centre = sum(box_of(e).cx for e in col.elements) / len(col.elements)
        else:
            columns.append(Column(centre=cx, elements=[element]))
    for col in columns:
        col.elements.sort(key=lambda e: (e.bbox.y, e.bbox.x))
    return columns


def columns_are_well_separated(columns: list[Column], *,
                               typical_width: float) -> bool:
    """Whether the column centres are far enough apart to be real columns.

    The test is on the *centres*, not the bounding boxes. Requiring disjoint
    boxes sounds stricter and is simply wrong for this layout: one long tile
    label is wider than the gap to its neighbour, so a page of perfectly clear
    tiles fails a box-overlap test that a page of noise might pass.

    What actually distinguishes columns is that their centres are separated by
    more than the width of an ordinary element. Two clusters closer than that
    are one column the clustering split.
    """
    if len(columns) < 2:
        return True
    centres = sorted(c.centre for c in columns)
    minimum = max(typical_width * 0.75, 1.0)
    return all(right - left >= minimum
               for left, right in zip(centres, centres[1:]))


def typical_element_width(elements: list[OCRElement]) -> float:
    widths = [e.bbox.width for e in elements if e.bbox.width > 0]
    return statistics.median(widths) if widths else 1.0


def split_into_runs(column: Column, *, gap_limit: float) -> list[Run]:
    """Break a column where the vertical gap exceeds ``gap_limit``.

    A card ends where the whitespace says it ends. Within a card the gaps are
    line-sized; between cards they are several lines. Splitting on the page's
    own gap distribution rather than a fixed number is what lets the same code
    read a dense statement and an airy slide.
    """
    runs: list[Run] = []
    current: list[OCRElement] = []
    previous_bottom: float | None = None
    for element in column.elements:
        box = box_of(element)
        if previous_bottom is not None and (box.y - previous_bottom) > gap_limit:
            if current:
                runs.append(Run(elements=current))
            current = []
        current.append(element)
        previous_bottom = max(previous_bottom or box.y1, box.y1)
    if current:
        runs.append(Run(elements=current))
    return runs


def vertical_gaps(column: Column) -> list[float]:
    """Gaps between consecutive elements, for choosing a split threshold."""
    gaps: list[float] = []
    previous_bottom: float | None = None
    for element in column.elements:
        box = box_of(element)
        if previous_bottom is not None:
            gaps.append(max(0.0, box.y - previous_bottom))
        previous_bottom = max(previous_bottom or box.y1, box.y1)
    return gaps


def aligned_across(runs: list[Run], *, tolerance: float) -> int:
    """How many of these runs start at the same height as another.

    This is the repetition signal. One label sitting above one number is a
    guess; four of them starting on the same line across four columns is a
    layout. The count is returned rather than a verdict so the caller can put
    it in the evidence.
    """
    tops = [r.top for r in runs]
    aligned = 0
    for i, top in enumerate(tops):
        if any(abs(top - other) <= tolerance
               for j, other in enumerate(tops) if i != j):
            aligned += 1
    return aligned


@dataclass(slots=True)
class Lattice:
    """How completely a set of boxes fills the grid their positions imply."""
    rows: int
    columns: int
    filled: float

    @property
    def is_a_grid(self) -> bool:
        """Whether these boxes are laid out as a grid rather than scattered.

        Two conditions, and both are about *completeness* rather than size:

        * **More than one column.** A single column of label-above-value pairs
          is a list, and a list of figures on a slide is as likely to be a
          chart's axis as a set of tiles.
        * **The lattice is filled.** Take the distinct row heights and the
          distinct column centres the boxes occupy; a grid puts something in
          almost every cell they define. Boxes scattered across a 3x2 lattice
          filling four of six cells are not a grid -- they are four things that
          happen to be on a page.

        The upper bound matters as much as the lower one: a fill above 1 means
        two boxes share a cell, which a grid does not do either.
        """
        return self.columns >= 2 and 0.9 <= self.filled <= 1.1


def lattice_of(boxes: list[BBox], *, tolerance: float) -> Lattice:
    """Measure the grid that a set of boxes occupies.

    Row and column membership is decided by clustering centres with the same
    single-pass rule used for columns elsewhere, so a layout is judged against
    its own spacing rather than an absolute distance.
    """
    if not boxes:
        return Lattice(rows=0, columns=0, filled=0.0)

    def bands(values: list[float]) -> int:
        ordered = sorted(values)
        count = 1
        last = ordered[0]
        for v in ordered[1:]:
            if v - last > tolerance:
                count += 1
            last = v
        return count

    rows = bands([b.cy for b in boxes])
    columns = bands([b.cx for b in boxes])
    cells = max(1, rows * columns)
    return Lattice(rows=rows, columns=columns, filled=len(boxes) / cells)
