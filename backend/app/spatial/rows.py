"""Reading financial figures laid out side by side.

The other spatial shape a presentation uses is a row: the name of a measure at
the left, its figure at the right, the pair repeated down a panel.

    Total No. of shares                861.47 Cr
    Book Value per Share               Rs. 56.47
    (June 30, 2026)
    Basic EPS (Annualized Q1 FY27)     Rs. 5.01

``cards.py`` cannot see this. It clusters on x-centre and walks down y, so a
label and the figure beside it land in different columns and neither column
holds a pair.

Why this reader is stricter than the vertical one
-------------------------------------------------
A vertical tile is enclosed by whitespace on all four sides. A horizontal pair
is separated along one axis only, and *everything* has that shape: a table row,
a chart legend, a sentence with a number in it. Measured on the document this
was built against, the strongest horizontal geometry on any page was a credit
rating grid --

    CRISIL            AAA
    ICRA              AA+ (stable)
    India Ratings     AA+ (stable)

-- five perfectly aligned repetitions containing no financial value at all.
Geometry alone cannot be the evidence here, so this module requires three
things a legend or a sentence does not have:

* a **gutter**: label and figure separated by real whitespace, not a space;
* **one figure with an unobstructed claim on the label**: a baseline runs the
  whole width of a page and crosses every panel on it, so a pair is only a pair
  when no other label and no other figure stands between the two. Where two
  figures compete for one label -- "Retail  Grew 6.7x  from US$ 2,838 Mn.  to
  US$ 18,920 Mn." -- there is no column head to say which is meant, and taking
  the first produces ``Retail = 6.7``;
* a **block**: several rows whose labels share a left edge and whose figures
  share theirs.

What the module does not decide is what any of it means. As with cards, the
metric, the number, the unit and the period are settled by
``spatial/extract.py`` through the same taxonomy and normalisation every other
strategy uses.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.models.schemas import OCRElement
from app.spatial.fragments import is_change, is_label, is_period, is_value
from app.spatial.geometry import box_of, median_line_height

#: Two elements are on the same row when their vertical centres agree within
#: this multiple of the page's own line height.
_BASELINE_TOLERANCE = 0.55
#: Below this, the space between a label and a figure is a word gap rather
#: than a gutter, and the two are one phrase.
_MIN_GUTTER_LINES = 0.6
#: Beyond this the figure is somewhere else on the page, not beside the label.
_MAX_GUTTER_LINES = 16.0
#: Left edges agreeing within this multiple of the line height are one column.
_EDGE_TOLERANCE_LINES = 0.9
#: Fewer rows than this is not a repeated structure. One label beside one
#: number is the single most common accident in a document.
_MIN_ROWS_IN_A_BLOCK = 3
#: A label reaching further than this across the page is a banner, a sentence
#: or a heading spanning the layout rather than naming one figure in it.
_MAX_LABEL_WIDTH_RATIO = 0.45


@dataclass(slots=True)
class RowCandidate:
    """One label with the figure beside it."""
    page_number: int
    bbox: BBox
    label: OCRElement
    value: OCRElement
    #: A continuation line under the label, where the layout had one. Carries
    #: the period often enough to be worth keeping.
    detail: OCRElement | None = None
    elements: list[OCRElement] = field(default_factory=list)
    evidence: dict[str, float | int | bool | str] = field(default_factory=dict)

    @property
    def label_text(self) -> str:
        text = self.label.text.strip()
        if self.detail is not None:
            return f"{text} {self.detail.text.strip()}".strip()
        return text

    @property
    def value_text(self) -> str:
        return self.value.text.strip()

    def element_ids(self) -> list[str]:
        return [e.id for e in self.elements]


@dataclass(slots=True)
class Block:
    """Rows that share a label edge and a figure edge: one panel."""
    rows: list[RowCandidate] = field(default_factory=list)
    label_edge: float = 0.0
    value_edge: float = 0.0

    @property
    def bbox(self) -> BBox | None:
        return BBox.union_all(r.bbox for r in self.rows)


@dataclass(slots=True)
class RowFinding:
    """What was found on one page, and why the rest was refused."""
    page_number: int
    blocks: list[Block] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Headings that could govern a block's period, in reading order.
    headings: list[OCRElement] = field(default_factory=list)
    #: Rows another strategy had already explained, dropped from what this
    #: one reports but counted in what it saw.
    rows_claimed_by_other_strategies: int = 0

    @property
    def rows(self) -> list[RowCandidate]:
        return [r for b in self.blocks for r in b.rows]

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def _baselines(elements: list[OCRElement], line_height: float,
               ) -> list[list[OCRElement]]:
    """Group elements whose vertical centres agree, ordered left to right."""
    tolerance = line_height * _BASELINE_TOLERANCE
    ordered = sorted(elements, key=lambda e: (box_of(e).cy, box_of(e).x))
    rows: list[list[OCRElement]] = []
    for element in ordered:
        cy = box_of(element).cy
        if rows and abs(cy - box_of(rows[-1][-1]).cy) <= tolerance:
            rows[-1].append(element)
        else:
            rows.append([element])
    for row in rows:
        row.sort(key=lambda e: box_of(e).x)
    return rows


def _pairs_on_a_row(row: list[OCRElement], *, line_height: float,
                    page_width: int, finding: RowFinding,
                    ) -> list[tuple[OCRElement, OCRElement]]:
    """Every label on this baseline that has an unobstructed figure beside it.

    A baseline is not a panel. It runs the full width of the page and crosses
    whatever else sits at that height -- a pie chart's legend on one side, a
    metric panel on the other. So the pairing is local: a label claims the next
    figure to its right, and only if nothing else stands between them.

    Two things break the claim:

    * **another label in between.** On "President of India ... Total No. of
      shares  861.47 Cr" the figure belongs to the nearer name, and the further
      one gets nothing rather than getting the same number.
    * **another figure in between.** Two figures reaching for one label is the
      ambiguous case: there is no column head here to settle which is meant, so
      neither is taken.
    """
    labels = [e for e in row
              if is_label(e) and not is_change(e) and not is_value(e)
              and box_of(e).width <= page_width * _MAX_LABEL_WIDTH_RATIO]
    values = [e for e in row if is_value(e)]
    if not values:
        return []
    if not labels:
        finding.reject("nothing_on_the_row_naming_a_figure")
        return []

    # A baseline carrying several figures that all have a name somewhere to
    # their left is a table row, and this reader has no column heads. Page 7 of
    # the document behind this module is such a row --
    #
    #     Loan Book   2,850   5,809   8,954   31,447   18% CAGR
    #
    # five figures under five dated heads, of which the first belongs to a
    # different entity in a different year. Taking the nearest would report
    # "Loan Book = 2,850" for the bank as it stands today. Refusing the whole
    # baseline is the only answer available without reading the heads, and
    # reading heads is what table reconstruction is for.
    leftmost_label = min(box_of(e).x for e in labels)
    named = [v for v in values if box_of(v).x > leftmost_label]
    if len(named) > 1:
        finding.reject("several_figures_on_the_baseline_no_column_heads")
        return []

    out: list[tuple[OCRElement, OCRElement]] = []
    for label in labels:
        right = box_of(label).x1
        after = [v for v in values if box_of(v).x >= right]
        if not after:
            continue
        value = min(after, key=lambda v: box_of(v).x)
        value_box = box_of(value)

        # Nothing may stand between the two.
        if any(right < box_of(other).x < value_box.x for other in labels):
            continue

        gutter = value_box.x - right
        if gutter < line_height * _MIN_GUTTER_LINES:
            finding.reject("no_gutter_between_the_label_and_the_figure")
            continue
        if gutter > line_height * _MAX_GUTTER_LINES:
            finding.reject("figure_too_far_from_the_label_to_belong_to_it")
            continue
        out.append((label, value))
    return out


def _blocks_from_rows(rows: list[RowCandidate], *, line_height: float,
                      ) -> list[Block]:
    """Group rows whose label edges and figure edges both agree.

    Both edges, not one. A page of prose produces rows sharing a left margin;
    a panel of metrics shares the figure column as well, and that second
    agreement is what says the layout was drawn rather than flowed.
    """
    tolerance = line_height * _EDGE_TOLERANCE_LINES
    blocks: list[Block] = []
    for row in sorted(rows, key=lambda r: box_of(r.label).y):
        label_x = box_of(row.label).x
        value_x = box_of(row.value).x
        for block in blocks:
            if (abs(block.label_edge - label_x) <= tolerance
                    and abs(block.value_edge - value_x) <= tolerance):
                block.rows.append(row)
                break
        else:
            blocks.append(Block(rows=[row], label_edge=label_x,
                                value_edge=value_x))
    return blocks


def detect_rows(elements: list[OCRElement], *, page_number: int,
                page_width: int, occupied: list[BBox] | None = None,
                ) -> RowFinding:
    """Find horizontal metric rows on one page.

    ``occupied`` is ground another strategy has already explained -- tables
    that produced facts, and the tiles a vertical reading accepted. Rows inside
    it are dropped, so no figure is reported twice from two directions, but
    they are dropped *after* the block is measured: a panel does not stop being
    a panel because someone else read one of its lines.
    """
    finding = RowFinding(page_number=page_number)
    usable = [e for e in elements if e.text and e.text.strip()]
    if len(usable) < 6:
        finding.notes.append("too few recognised elements to form a block")
        return finding

    line_height = median_line_height(usable)
    baselines = _baselines(usable, line_height)
    finding.headings = [e for e in usable if is_period(e)]

    candidates: list[RowCandidate] = []
    for index, row in enumerate(baselines):
        for label, value in _pairs_on_a_row(
                row, line_height=line_height, page_width=page_width,
                finding=finding):
            label_box, value_box = box_of(label), box_of(value)

            # A continuation line under the label, inside the label's own
            # column and holding no figure of its own: "(June 30, 2026)"
            # beneath "Book Value per Share". It qualifies the label rather
            # than starting a row, and it is where a stated period often lives.
            detail: OCRElement | None = None
            if index + 1 < len(baselines):
                for other in baselines[index + 1]:
                    other_box = box_of(other)
                    if (abs(other_box.x - label_box.x) <= line_height
                            and other_box.x1 < value_box.x
                            and not is_value(other)
                            and other_box.y - label_box.y1 <= line_height):
                        detail = other
                        break

            parts = [label, value] + ([detail] if detail else [])
            box = BBox.union_all(box_of(e) for e in parts)
            if box is None:
                finding.reject("no_geometry")
                continue
            candidates.append(RowCandidate(
                page_number=page_number, bbox=box, label=label, value=value,
                detail=detail, elements=parts,
                evidence={
                    "gutter_px": round(value_box.x - label_box.x1, 1),
                    "baseline_offset_px": round(
                        abs(label_box.cy - value_box.cy), 1),
                    "label_left_px": round(label_box.x, 1),
                    "value_left_px": round(value_box.x, 1),
                    "has_continuation_line": detail is not None,
                    "ocr_confidence_min": round(
                        min(e.confidence for e in parts), 4),
                }))

    if not candidates:
        finding.notes.append("no baseline held a single label-and-figure pair")
        return finding

    for block in _blocks_from_rows(candidates, line_height=line_height):
        if len(block.rows) < _MIN_ROWS_IN_A_BLOCK:
            finding.reject("block_not_repeated_enough_to_trust")
            continue
        gutters = [float(r.evidence["gutter_px"]) for r in block.rows]
        spread = (statistics.pstdev(gutters) if len(gutters) > 1 else 0.0)
        for row in block.rows:
            row.evidence["rows_in_block"] = len(block.rows)
            row.evidence["label_edge_px"] = round(block.label_edge, 1)
            row.evidence["value_edge_px"] = round(block.value_edge, 1)
            row.evidence["gutter_spread_px"] = round(spread, 1)

        # Routing happens here, *after* the structure has been measured, and
        # only here.
        #
        # Whether another strategy has already read one of these rows says
        # nothing about whether the panel is a panel. Removing its text before
        # the block is formed conflates two different things -- "do not report
        # this figure twice" and "this layout is not evidence" -- and on a real
        # page it cost two facts: a table read the middle row of a panel of
        # three, and the two rows left could no longer reach quorum.
        #
        # So the block keeps its full evidence, and the rows another strategy
        # explained are dropped from what this one reports.
        if occupied:
            keep = [r for r in block.rows
                    if not any(b.contains_ratio(r.bbox) > 0.5
                               for b in occupied)]
            finding.rows_claimed_by_other_strategies += (
                len(block.rows) - len(keep))
            if not keep:
                continue
            block.rows = keep
        finding.blocks.append(block)

    if not finding.blocks:
        finding.notes.append(
            f"{len(candidates)} label-and-figure pair(s) found, none of them "
            f"part of a block of {_MIN_ROWS_IN_A_BLOCK} or more sharing both "
            f"edges; a lone pair is as likely to be a sentence as a metric")
    return finding
