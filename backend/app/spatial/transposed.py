"""Reading a grid of figures whose structure is drawn rather than ruled.

A results deck sets out a small table without drawing one:

    Parameters      CAPITAL FIRST   IDFC Bank    Upon merger    Now
    (US$ Million)     (30-Sep-18)   (30-Sep-18)  (31-Dec-2018)  (30-Jun-2026)
    Loan Book             2,850         5,809          8,954         31,447
    Net Worth               309         1,560          1,940          5,137
    NIM %                  8.2%          1.6%           3.1%           6.0%

There are no ruling lines and no gutters the projection can find -- every cell
is a rounded rectangle -- so ``detect_table_bands`` sees one band and
reconstruction reads nothing from it. The horizontal reader refuses it too, and
correctly: a row carrying four figures under four dated heads has no single
figure to pair with its label, and taking the nearest reports another company's
2018 loan book as this bank's.

What is actually there is a *lattice*: figures at repeated x positions and
repeated y positions, a label axis down one side and a period axis along the
other. That is enough structure to read, and it is what this module looks for.

The other orientation
---------------------
The same lattice appears turned ninety degrees:

    Revenue        PAT          EPS
    ₹10,000 Cr     ₹2,000 Cr    ₹15.20
    Q1 FY27        Q1 FY27      Q1 FY27

Metrics along the top, values beneath, periods beneath those. Both forms reduce
to the same question -- which axis names the measures and which names the
periods -- so both are found by one detector and told apart by what the axes
contain, never by assuming an orientation.

Refusal
-------
The lattice must be *filled*: a grid whose cells are mostly empty is scattered
text, and this is the same test the tile reader uses. Beyond that, everything
this module produces goes through the shared acceptance rules -- a label must
name a metric the taxonomy knows, a figure must parse, a percentage may not
stand for a monetary metric -- so a lattice of ratings or a lattice of page
numbers yields nothing.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.models.schemas import OCRElement
from app.spatial.fragments import is_label, is_period, is_value
from app.spatial.geometry import box_of, median_line_height

#: Rows and columns are formed by clustering centres; two centres within this
#: multiple of the page's line height belong to the same band.
_ROW_TOLERANCE = 0.55
_COLUMN_TOLERANCE = 1.6
#: A lattice smaller than this is not repeated evidence of anything.
_MIN_VALUE_ROWS = 2
_MIN_VALUE_COLUMNS = 2
#: Share of the lattice's cells that must actually hold a figure. A designer's
#: table is full; scattered text is not.
_MIN_FILL = 0.7
#: A period header must line up with the column it heads to this fraction of
#: the column's width. Beyond it the header belongs to a neighbour, and the
#: association is refused rather than shifted to the nearest.
_HEADER_ALIGNMENT = 0.5


@dataclass(slots=True)
class Cell:
    element: OCRElement
    row: int
    column: int


@dataclass(slots=True)
class Lattice:
    """A filled grid of figures, with whatever labels each axis carries."""
    rows: list[float] = field(default_factory=list)
    columns: list[float] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)
    #: index -> the element naming that row / column, where one was found.
    row_labels: dict[int, OCRElement] = field(default_factory=dict)
    column_headers: dict[int, OCRElement] = field(default_factory=dict)

    @property
    def fill(self) -> float:
        cells = len(self.rows) * len(self.columns)
        return len(self.cells) / cells if cells else 0.0

    def bbox(self) -> BBox | None:
        return BBox.union_all(box_of(c.element) for c in self.cells)


@dataclass(slots=True)
class TransposedCandidate:
    """One figure from the grid, with the label and period that locate it."""
    page_number: int
    bbox: BBox
    label: OCRElement
    value: OCRElement
    period: OCRElement | None
    elements: list[OCRElement]
    evidence: dict[str, float | int | bool | str] = field(default_factory=dict)

    @property
    def label_text(self) -> str:
        return self.label.text.strip()

    @property
    def value_text(self) -> str:
        return self.value.text.strip()

    @property
    def period_text(self) -> str:
        return self.period.text.strip() if self.period is not None else ""

    def element_ids(self) -> list[str]:
        return [e.id for e in self.elements]


@dataclass(slots=True)
class TransposedFinding:
    page_number: int
    candidates: list[TransposedCandidate] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rows: int = 0
    columns: int = 0
    fill: float = 0.0
    orientation: str = ""

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def _bands(values: list[float], tolerance: float) -> list[float]:
    """Cluster centres into bands, returning each band's mean."""
    if not values:
        return []
    ordered = sorted(values)
    groups: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        if v - groups[-1][-1] <= tolerance:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _nearest(bands: list[float], value: float, tolerance: float) -> int | None:
    best, best_d = None, None
    for i, b in enumerate(bands):
        d = abs(b - value)
        if d <= tolerance and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


def _build_lattice(values: list[OCRElement], line_height: float) -> Lattice:
    rows = _bands([box_of(e).cy for e in values], line_height * _ROW_TOLERANCE)
    columns = _bands([box_of(e).cx for e in values],
                     line_height * _COLUMN_TOLERANCE)
    lattice = Lattice(rows=rows, columns=columns)
    for e in values:
        b = box_of(e)
        r = _nearest(rows, b.cy, line_height * _ROW_TOLERANCE)
        c = _nearest(columns, b.cx, line_height * _COLUMN_TOLERANCE)
        if r is not None and c is not None:
            lattice.cells.append(Cell(element=e, row=r, column=c))
    return lattice


def _label_for_each_row(lattice: Lattice, labels: list[OCRElement],
                        line_height: float) -> dict[int, OCRElement]:
    """The nearest label to the left of each row of figures."""
    out: dict[int, OCRElement] = {}
    leftmost = min((box_of(c.element).x for c in lattice.cells), default=0.0)
    for i, cy in enumerate(lattice.rows):
        on_row = [e for e in labels
                  if abs(box_of(e).cy - cy) <= line_height * _ROW_TOLERANCE
                  and box_of(e).x1 <= leftmost + line_height]
        if on_row:
            out[i] = max(on_row, key=lambda e: box_of(e).x1)
    return out


def _header_for_each_column(lattice: Lattice, headers: list[OCRElement],
                            line_height: float) -> dict[int, OCRElement]:
    """The header sitting above each column, where it lines up with it.

    Alignment is the whole rule. A header that does not sit over the column it
    would head belongs to a neighbour, and taking the nearest one instead is
    how a balance-sheet date came to be attached to a percentage in an earlier
    phase. Where the header does not line up, the column gets none.
    """
    out: dict[int, OCRElement] = {}
    top = min((box_of(c.element).y for c in lattice.cells), default=0.0)
    above = [e for e in headers if box_of(e).y1 <= top + line_height]
    if not above:
        return out

    spacing = (statistics.median(
        [b - a for a, b in zip(lattice.columns, lattice.columns[1:])])
        if len(lattice.columns) > 1 else line_height * 6)
    limit = spacing * _HEADER_ALIGNMENT

    for i, cx in enumerate(lattice.columns):
        aligned = [e for e in above if abs(box_of(e).cx - cx) <= limit]
        if len(aligned) == 1:
            out[i] = aligned[0]
        elif len(aligned) > 1:
            # Several headers over one column. Where they sit at different
            # heights this is a stacked head -- "Quarter Ended" above a date --
            # and the lowest is the one that names the column. Where they sit
            # at the same height they are two different heads competing for one
            # column, nothing distinguishes them, and the column gets none.
            lowest = max(box_of(e).y for e in aligned)
            bottom = [e for e in aligned
                      if abs(box_of(e).y - lowest) <= line_height * 0.5]
            if len(bottom) == 1:
                out[i] = bottom[0]

    # A header heads one column. Where the same element is the best match for
    # two of them, it identifies neither: on one filing a quarter-ended head
    # sat between the quarter column and the year-ended column beside it, and
    # taking it for both would have dated a full year's figure as a quarter.
    claimed: dict[str, list[int]] = {}
    for i, e in out.items():
        claimed.setdefault(e.id, []).append(i)
    for ids in claimed.values():
        if len(ids) > 1:
            for i in ids:
                out.pop(i, None)
    return out


def _row_value_kind(cells: list[Cell]) -> dict[int, str]:
    """The kind of figure each row of the grid is mostly made of.

    A statement's rows are homogeneous -- a row of amounts, a row of rates --
    and the column that breaks the pattern is the growth column a designer
    adds on the right. Measured on real pages: a balance sheet's
    "Shareholders' Funds" row read 4,088 / 5,000 / 5,137 / 25.7 %, and the
    25.7 % is the year-on-year change, not a fourth balance. Reading it as one
    would report a percentage as an amount and date it to the quarter beside
    it.
    """
    kinds: dict[int, list[str]] = {}
    for cell in cells:
        text = cell.element.text or ""
        kind = "rate" if ("%" in text or "bps" in text.lower()) else "amount"
        kinds.setdefault(cell.row, []).append(kind)
    out: dict[int, str] = {}
    for row, seen in kinds.items():
        rates = seen.count("rate")
        out[row] = "rate" if rates * 2 > len(seen) else "amount"
    return out


def detect_transposed(elements: list[OCRElement], *, page_number: int,
                      page_width: int, occupied: list[BBox] | None = None,
                      ) -> TransposedFinding:
    """Find a filled grid of figures with a label axis and a period axis."""
    finding = TransposedFinding(page_number=page_number)
    usable = [e for e in elements if e.text and e.text.strip()]
    if len(usable) < 8:
        finding.notes.append("too few recognised elements to form a grid")
        return finding

    line_height = median_line_height(usable)
    values = [e for e in usable if is_value(e)]
    if len(values) < _MIN_VALUE_ROWS * _MIN_VALUE_COLUMNS:
        finding.notes.append("too few figures on the page to form a grid")
        return finding

    lattice = _build_lattice(values, line_height)
    finding.rows = len(lattice.rows)
    finding.columns = len(lattice.columns)
    finding.fill = round(lattice.fill, 3)
    if (len(lattice.rows) < _MIN_VALUE_ROWS
            or len(lattice.columns) < _MIN_VALUE_COLUMNS):
        finding.notes.append(
            f"the figures occupy {len(lattice.rows)}x{len(lattice.columns)} "
            f"positions, which is not a grid")
        return finding
    if lattice.fill < _MIN_FILL:
        finding.notes.append(
            f"the {len(lattice.rows)}x{len(lattice.columns)} grid is "
            f"{lattice.fill:.0%} filled; a drawn table is full and scattered "
            f"figures are not")
        finding.reject("grid_not_filled")
        return finding

    labels = [e for e in usable if is_label(e) and not is_value(e)]
    periods = [e for e in usable if is_period(e) and not is_value(e)]
    lattice.row_labels = _label_for_each_row(lattice, labels, line_height)
    lattice.column_headers = _header_for_each_column(
        lattice, periods, line_height)

    # Which axis names the measures?  Whichever one carries metric labels.
    # Nothing here assumes an orientation: a deck that puts its metrics along
    # the top and its periods down the side reads the same way round.
    if lattice.row_labels and lattice.column_headers:
        finding.orientation = "metric_rows_period_columns"
    elif lattice.row_labels:
        finding.orientation = "metric_rows"
    else:
        finding.notes.append(
            "neither axis of the grid carries labels that name a measure")
        finding.reject("no_label_axis")
        return finding

    occupied = list(occupied or [])
    row_kind = _row_value_kind(lattice.cells)
    for cell in lattice.cells:
        label = lattice.row_labels.get(cell.row)
        if label is None:
            finding.reject("figure_in_a_row_nothing_names")
            continue
        text = cell.element.text or ""
        kind = "rate" if ("%" in text or "bps" in text.lower()) else "amount"
        if kind != row_kind.get(cell.row, kind):
            # The odd one out in its row: a change column, not a value.
            finding.reject("figure_disagrees_with_its_row")
            continue
        header = lattice.column_headers.get(cell.column)
        parts = [label, cell.element] + ([header] if header else [])
        box = BBox.union_all(box_of(e) for e in parts)
        if box is None:
            continue
        if any(b.contains_ratio(box_of(cell.element)) > 0.5 for b in occupied):
            finding.reject("already_explained_by_another_strategy")
            continue
        finding.candidates.append(TransposedCandidate(
            page_number=page_number, bbox=box, label=label,
            value=cell.element, period=header, elements=parts,
            evidence={
                "grid_rows": len(lattice.rows),
                "grid_columns": len(lattice.columns),
                "grid_filled": round(lattice.fill, 3),
                "row": cell.row, "column": cell.column,
                "orientation": finding.orientation,
                "column_header_aligned": header is not None,
                "ocr_confidence_min": round(
                    min(e.confidence for e in parts), 4),
            }))
    if not finding.candidates:
        finding.notes.append("no figure in the grid had a label naming it")
    return finding
