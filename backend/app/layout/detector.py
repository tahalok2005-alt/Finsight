"""Layout region detection (heuristic baseline).

Approach
--------
No pretrained document-layout model is used: on this machine (CPU only, 16 GB,
integrated graphics) a DocLayout/LayoutLMv3-class model would add gigabytes of
download and seconds per page for a modest gain on the clean, ruled, single-
column financial releases this system targets.  Instead the detector combines
signals that are individually cheap and jointly strong:

* **typography** - relative font size, weight and case (title vs heading vs body)
* **geometry** - position bands, indentation, line gaps, alignment
* **ruling lines** - horizontal/vertical rules from the page image
* **numeric alignment** - repeated right-aligned numeric columns, the single
  most reliable indicator of a financial table
* **ink/colour** - non-text graphical blocks for charts and figures

The detector is registered behind ``LayoutEngine`` so a learned model can
replace it without touching anything downstream; the ``detector`` field on each
region records which produced it.  Its limitations are documented rather than
hidden -- see docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median

from app.config.settings import settings
from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.layout.cv_signals import (
    GraphicBlob, LineSegment, PageSignals, vertical_whitespace_columns,
)
from app.models.enums import RegionType
from app.models.schemas import BBoxModel, OCRElement, Region
from app.ocr.lines import TextLine, median_line_height
from app.normalization.numbers import looks_numeric

log = get_logger("layout.detector")

DETECTOR_NAME = "heuristic-v1"

BULLET_RE = re.compile(r"^\s*([•▪●·–—*>-]|\(?[a-z]\)|\(?\d{1,2}[.)])\s+")
FOOTNOTE_RE = re.compile(r"^\s*(\*+|#+|\d{1,2}\.|\[\d{1,2}\]|Note\s*\d*[:.]?)\s+", re.I)
PAGE_NUM_RE = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.I)
HEADING_TAIL_RE = re.compile(r"[.!?;,]\s*$")


@dataclass(slots=True)
class LayoutContext:
    """Per-page measurements the classifier reasons against."""
    page_width: int
    page_height: int
    body_font: float
    line_height: float
    columns: list[tuple[float, float]]
    header_band: float
    footer_band: float


def _font_of(line: TextLine) -> float:
    return line.font_size or line.height


def build_context(lines: list[TextLine], width: int, height: int) -> LayoutContext:
    boxes = [ln.bbox for ln in lines]
    fonts = [_font_of(ln) for ln in lines if _font_of(ln) > 0]
    body = median(fonts) if fonts else 12.0
    return LayoutContext(
        page_width=width, page_height=height, body_font=body,
        line_height=median_line_height(lines),
        columns=vertical_whitespace_columns(
            boxes, width, min_gap_ratio=settings.layout_column_gap_ratio),
        header_band=height * 0.075,
        footer_band=height * 0.93,
    )


def _numeric_fraction(line: TextLine) -> float:
    if not line.elements:
        return 0.0
    n = sum(1 for e in line.elements if looks_numeric(e.text))
    return n / len(line.elements)


def _in_columns(line: TextLine, ctx: LayoutContext) -> int:
    for i, (x0, x1) in enumerate(ctx.columns):
        if line.bbox.cx >= x0 - 4 and line.bbox.cx <= x1 + 4:
            return i
    return 0



# ------------------------------------------------------- side-by-side prose
#: The blank strip between two printed columns is narrow -- a press release
#: measured 25px on a 1700px page, a single character width. Width alone
#: therefore cannot tell a gutter from wide word spacing, and the test that
#: does is alignment: in two columns the right-hand side starts at the same x
#: on line after line, where in a paragraph a wide space falls wherever the
#: words happen to break.
_GUTTER_MIN_RATIO = 0.007
#: Most of the block's lines must reach across it. A single wide line in a
#: paragraph is not a second column.
_GUTTER_MIN_CROSSING = 0.55
#: How far the second column's left edge may wander, as a share of the page,
#: before the lines are prose with gaps in them rather than a column.
_GUTTER_ALIGN_RATIO = 0.05


def _prose_side(elements: list[OCRElement]) -> bool:
    """Whether a side of a candidate gutter reads as prose rather than cells.

    This is the guard that keeps statement tables out of the column splitter.
    A table row also has a wide gap in it -- the space between the labels and
    the figures -- and splitting those rows would take every statement apart.
    A column of prose carries words; a column of figures does not.
    """
    words = [e.text for e in elements if e.text and not looks_numeric(e.text)]
    letters = sum(1 for w in words if any(c.isalpha() for c in w))
    return letters >= 2


def _block_gutter(rows: list[TextLine], page_width: int) -> float | None:
    """The x of a blank vertical strip running down a block of lines, if any.

    A press release prints its highlights as two bulleted columns side by
    side. The page as a whole has no gutter -- the headline above and the
    paragraphs below run the full width -- so a page-wide projection sees
    nothing, and the lines of the two columns, which share a y band, were
    grouped into single lines reading "EBITDA (Earnings before Interest, Tax,
    Revenue from operations - Rs. 9,579 Depreciation, & Amortization)". Every
    figure in that block then belonged to whichever column the reader guessed.
    So the gutter is looked for in the block itself.
    """
    if len(rows) < 3:
        return None
    boxes = [e.bbox for ln in rows for e in ln.elements]
    if not boxes:
        return None
    left = min(b.x for b in boxes)
    right = max(b.x + b.width for b in boxes)
    if right - left < page_width * 0.5:
        return None
    occupied = [False] * (page_width + 2)
    for b in boxes:
        x0 = max(0, int(b.x))
        x1 = min(page_width, int(b.x + b.width) + 1)
        for x in range(x0, x1):
            occupied[x] = True

    min_gap = max(8, int(page_width * _GUTTER_MIN_RATIO))
    best: tuple[int, float] | None = None
    run_start = None
    for x in range(int(left), int(right) + 1):
        if not occupied[x]:
            if run_start is None:
                run_start = x
        else:
            if run_start is not None and x - run_start >= min_gap:
                mid = (run_start + x) / 2.0
                # a gutter splits the block near its middle, not at a margin
                if left + (right - left) * 0.25 <= mid <= left + (right - left) * 0.75:
                    width = x - run_start
                    if best is None or width > best[0]:
                        best = (width, mid)
            run_start = None
    if best is None:
        return None

    mid = best[1]
    crossing = 0
    right_starts: list[float] = []
    for ln in rows:
        l_side = [e for e in ln.elements if e.bbox.x + e.bbox.width <= mid]
        r_side = [e for e in ln.elements if e.bbox.x >= mid]
        if l_side and r_side:
            crossing += 1
            right_starts.append(min(e.bbox.x for e in r_side))
    if crossing < len(rows) * _GUTTER_MIN_CROSSING or len(right_starts) < 3:
        return None
    # the second column begins at the same place on every line; a paragraph's
    # wide spaces do not line up down the page
    if max(right_starts) - min(right_starts) > page_width * _GUTTER_ALIGN_RATIO:
        return None

    # both sides must read as prose, or this is a table row and not a column
    left_els = [e for ln in rows for e in ln.elements if e.bbox.x + e.bbox.width <= mid]
    right_els = [e for ln in rows for e in ln.elements if e.bbox.x >= mid]
    if not (_prose_side(left_els) and _prose_side(right_els)):
        return None
    return mid


def _side_rows(rows: list[TextLine], mid: float, left: bool) -> list[TextLine]:
    """The block's lines, cut back to one side of the gutter."""
    out: list[TextLine] = []
    for ln in rows:
        if left:
            els = [e for e in ln.elements if e.bbox.x + e.bbox.width <= mid]
        else:
            els = [e for e in ln.elements if e.bbox.x >= mid]
        if not els:
            continue
        box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                                  height=e.bbox.height) for e in els)
        if box is None:
            continue
        out.append(TextLine(id=f"{ln.id}:{'l' if left else 'r'}",
                            page_number=ln.page_number, elements=els, bbox=box))
    return out


# --------------------------------------------------------------- table bands
@dataclass(slots=True)
class TableBand:
    """A vertical band of the page believed to hold one table."""
    y0: float
    y1: float
    x0: float
    x1: float
    line_indices: list[int]
    ruled: bool
    score: float
    n_numeric_rows: int
    n_columns: int


SECTION_BANNER_RE = re.compile(
    r"^\s*[A-Z]\.\s|^\s*(operational update|financial results?|"
    r"operating (metrics|update)|key (metrics|highlights?))\b", re.I)


def _is_prose_line(line: TextLine) -> bool:
    """True when a line reads as a sentence rather than a table header.

    Judged from the recognised words alone, so it behaves identically with and
    without a PDF text layer.
    """
    text = line.text.strip()
    if not text:
        return True
    words = text.split()
    if len(words) > 12:
        return True
    # a trailing full stop marks a sentence; header cells do not carry one
    return text.endswith((".", "!", "?")) and len(words) > 4


def _is_banner_line(line: TextLine, ctx: LayoutContext) -> bool:
    """A heading-like line that runs across the table's column structure.

    Absorbing one into a table band destroys column detection, because its ink
    covers every vertical gutter the projection relies on.
    """
    text = line.text.strip()
    if not text:
        return False
    if SECTION_BANNER_RE.match(text):
        return True
    if any(ch.isdigit() for ch in text):
        return False
    # a single wide fragment with no internal gaps behaves like a banner
    widest = max((e.bbox.width for e in line.elements), default=0.0)
    return widest > ctx.page_width * 0.4 and len(line.elements) <= 2


def _column_positions(lines: list[TextLine], tol: float) -> list[float]:
    """Cluster the left edges of numeric fragments into candidate columns."""
    xs: list[float] = []
    for ln in lines:
        for e in ln.elements:
            if looks_numeric(e.text):
                xs.append(e.bbox.cx)
    if not xs:
        return []
    xs.sort()
    clusters: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [sum(c) / len(c) for c in clusters if len(c) >= 2]


def detect_table_bands(lines: list[TextLine], signals: PageSignals,
                       ctx: LayoutContext) -> list[TableBand]:
    """Group consecutive lines that behave like table rows.

    A "table row" is a line whose fragments align with page-level numeric
    column positions, or a line enclosed by ruling lines.  Runs of such lines
    (allowing one non-matching line for wrapped labels) become bands.
    """
    if not lines:
        return []
    tol = max(12.0, ctx.page_width * settings.table_column_tolerance_ratio * 2)
    col_centres = _column_positions(lines, tol)

    def row_score(ln: TextLine) -> float:
        if len(ln.elements) < 2:
            return 0.0
        numeric = [e for e in ln.elements if looks_numeric(e.text)]
        if len(numeric) < max(1, settings.table_min_cols - 1):
            return 0.0
        if not col_centres:
            return 0.5 if len(numeric) >= 2 else 0.0
        hits = sum(1 for e in numeric
                   if any(abs(e.bbox.cx - c) <= tol for c in col_centres))
        return hits / max(1, len(numeric))

    scores = [row_score(ln) for ln in lines]
    ruled_ys = sorted(seg.bbox.cy for seg in signals.h_lines
                      if seg.length > ctx.page_width * 0.25)

    bands: list[TableBand] = []
    i = 0
    n = len(lines)
    while i < n:
        if scores[i] < 0.5:
            i += 1
            continue
        j = i
        misses = 0
        last_hit = i
        while j + 1 < n:
            gap = lines[j + 1].bbox.y - lines[j].bbox.y1
            if gap > ctx.line_height * 3.2:
                break
            if scores[j + 1] >= 0.5:
                last_hit = j + 1
                misses = 0
            else:
                misses += 1
                if misses > 1:
                    break
            j += 1
        j = last_hit

        # Pull in the immediately preceding lines: header rows carry period
        # labels and are usually non-numeric, so they score 0 by themselves.
        # A section banner above the table is *not* absorbed -- it spans the
        # full width and would erase every column gutter in the projection.
        start = i
        while start > 0:
            prev = lines[start - 1]
            gap = lines[start].bbox.y - prev.bbox.y1
            if gap > ctx.line_height * 2.6:
                break
            # Stop at prose.  A header is terse -- "Particulars", "Quarter
            # Ended", "1Q FY27" -- whereas a sentence above the table is not.
            # Word count is a purely visual test; the earlier version used the
            # text layer's bold flag here, which silently disabled header
            # absorption whenever no text layer existed.
            if _is_prose_line(prev):
                break
            if _numeric_fraction(prev) > 0.8:
                break
            if _is_banner_line(prev, ctx):
                break
            start -= 1
            if i - start >= 3:
                break

        rows = lines[start:j + 1]
        if len(rows) < settings.table_min_rows:
            i = j + 1
            continue
        box = BBox.union_all(ln.bbox for ln in rows)
        if box is None:
            i = j + 1
            continue
        # Page chrome (the registered-office / contact block that repeats on
        # every page) is column-aligned and contains phone and CIN numbers, so
        # it scores like a table.  Anything living in the footer band is not.
        if box.cy >= ctx.footer_band or box.y >= ctx.page_height * 0.88:
            i = j + 1
            continue
        n_numeric_rows = sum(1 for k in range(start, j + 1) if scores[k] >= 0.5)
        ruled = any(box.y - ctx.line_height <= y <= box.y1 + ctx.line_height
                    for y in ruled_ys)
        score = min(1.0, 0.45 + 0.35 * (n_numeric_rows / max(1, len(rows)))
                    + (0.2 if ruled else 0.0))
        bands.append(TableBand(
            y0=box.y, y1=box.y1, x0=box.x, x1=box.x1,
            line_indices=list(range(start, j + 1)), ruled=ruled, score=score,
            n_numeric_rows=n_numeric_rows,
            n_columns=max(2, len(col_centres) + 1)))
        i = j + 1
    return bands


# ------------------------------------------------------------------- charts
def classify_graphic(blob: GraphicBlob, ctx: LayoutContext,
                     covered_by_text: float) -> tuple[RegionType, float]:
    """Decide whether a non-text ink block is a chart, a figure, or noise."""
    area_ratio = blob.bbox.area / float(ctx.page_width * ctx.page_height)
    if area_ratio < 0.005 or covered_by_text > 0.55:
        return RegionType.MISC, 0.2
    aspect = blob.bbox.width / max(1.0, blob.bbox.height)

    chart_score = 0.0
    if blob.color_ratio > 0.06:
        chart_score += 0.35
    if blob.distinct_colors >= 4:
        chart_score += 0.2
    if 0.25 < aspect < 4.0:
        chart_score += 0.15
    if 0.02 < blob.edge_density < 0.25:
        chart_score += 0.15
    if blob.fill_ratio > 0.05:
        chart_score += 0.15

    if chart_score >= 0.6:
        return RegionType.CHART, min(0.9, chart_score)
    if area_ratio > 0.02 and blob.fill_ratio > 0.02:
        return RegionType.FIGURE, min(0.7, 0.35 + chart_score / 2)
    return RegionType.MISC, 0.25


# ------------------------------------------------------------- main detector
class HeuristicLayoutDetector:
    """Produces typed, ordered regions for one page."""

    name = DETECTOR_NAME

    def detect(self, *, document_id: str, page_number: int,
               lines: list[TextLine], signals: PageSignals,
               elements: list[OCRElement]) -> list[Region]:
        if not lines and not signals.graphics:
            return []
        ctx = build_context(lines, signals.width, signals.height)
        bands = detect_table_bands(lines, signals, ctx)
        in_table = {idx for b in bands for idx in b.line_indices}

        regions: list[Region] = []
        consumed: set[int] = set()

        # 1. tables ---------------------------------------------------------
        for bi, band in enumerate(bands):
            rows = [lines[i] for i in band.line_indices]
            box = BBox.union_all(ln.bbox for ln in rows)
            if box is None:
                continue
            box = box.padded(4).clipped(ctx.page_width, ctx.page_height)
            regions.append(Region(
                id=stable_id("region", document_id, page_number, "table", bi),
                document_id=document_id, page_number=page_number,
                type=RegionType.TABLE, bbox=BBoxModel(**box.as_dict()),
                confidence=round(band.score, 3), reading_order=0,
                column_index=0,
                text="\n".join(ln.text for ln in rows),
                element_ids=[e for ln in rows for e in ln.element_ids()],
                detector=self.name,
                meta={"ruled": band.ruled, "n_rows": len(rows),
                      "n_numeric_rows": band.n_numeric_rows,
                      "line_ids": [ln.id for ln in rows]}))
            consumed.update(band.line_indices)

        # 2. graphics -------------------------------------------------------
        text_boxes = [ln.bbox for ln in lines]
        for gi, blob in enumerate(signals.graphics):
            covered = 0.0
            if blob.bbox.area > 0:
                covered = sum(
                    (blob.bbox.intersection(tb).area if blob.bbox.intersection(tb)
                     else 0.0) for tb in text_boxes) / blob.bbox.area
            # a blob sitting on a detected table is the table, not a figure
            if any(r.bbox.geom.contains_ratio(blob.bbox) > 0.6
                   for r in regions if r.type == RegionType.TABLE):
                continue
            rtype, conf = classify_graphic(blob, ctx, covered)
            if rtype == RegionType.MISC:
                continue
            regions.append(Region(
                id=stable_id("region", document_id, page_number, "gfx", gi),
                document_id=document_id, page_number=page_number, type=rtype,
                bbox=BBoxModel(**blob.bbox.as_dict()), confidence=round(conf, 3),
                reading_order=0, text="", detector=self.name,
                meta={"color_ratio": round(blob.color_ratio, 4),
                      "edge_density": round(blob.edge_density, 4),
                      "distinct_colors": blob.distinct_colors,
                      "text_coverage": round(covered, 3)}))

        # 3. text regions ---------------------------------------------------
        regions.extend(self._text_regions(
            document_id=document_id, page_number=page_number, lines=lines,
            ctx=ctx, skip=consumed, in_table=in_table))

        # 4. KPI blocks -----------------------------------------------------
        regions.extend(self._kpi_blocks(
            document_id=document_id, page_number=page_number, lines=lines,
            ctx=ctx, skip=consumed))

        assign_reading_order(regions, ctx)
        return regions

    # ------------------------------------------------------------- text pass
    def _text_regions(self, *, document_id: str, page_number: int,
                      lines: list[TextLine], ctx: LayoutContext,
                      skip: set[int], in_table: set[int]) -> list[Region]:
        out: list[Region] = []
        buffer: list[tuple[int, TextLine]] = []
        counter = 0

        def emit(rows: list[TextLine], column: int | None = None) -> None:
            nonlocal counter
            box = BBox.union_all(ln.bbox for ln in rows)
            if box is None:
                return
            text = " ".join(ln.text for ln in rows).strip()
            rtype = RegionType.LIST if any(
                BULLET_RE.match(ln.text) for ln in rows) else RegionType.PARAGRAPH
            out.append(Region(
                id=stable_id("region", document_id, page_number, "txt", counter),
                document_id=document_id, page_number=page_number, type=rtype,
                bbox=BBoxModel(**box.as_dict()),
                confidence=round(min(0.95, 0.6 + 0.35 * (
                    sum(ln.confidence for ln in rows) / len(rows))), 3),
                reading_order=0,
                column_index=_in_columns(rows[0], ctx) if column is None else column,
                text=text,
                element_ids=[e for ln in rows for e in ln.element_ids()],
                detector=self.name,
                meta={"n_lines": len(rows)}
                | ({"side_by_side_column": column} if column is not None else {})))
            counter += 1

        def flush() -> None:
            nonlocal buffer
            if not buffer:
                return
            rows = [ln for _, ln in buffer]
            # Two bulleted columns printed side by side share their y bands, so
            # line grouping put one line of each into a single line. Read as one
            # block, the two columns' sentences interleave and every figure in
            # them belongs to whichever column the reader guesses. Where the
            # block has a blank strip down its middle with prose on both sides,
            # it is read as two columns.
            mid = _block_gutter(rows, ctx.page_width)
            if mid is not None:
                left_rows = _side_rows(rows, mid, left=True)
                right_rows = _side_rows(rows, mid, left=False)
                if len(left_rows) >= 2 and len(right_rows) >= 2:
                    log.info("layout.side_by_side_columns",
                             page_number=page_number, lines=len(rows),
                             gutter_x=round(mid, 1))
                    emit(left_rows, column=0)
                    emit(right_rows, column=1)
                    buffer = []
                    return
            emit(rows)
            buffer = []

        for idx, line in enumerate(lines):
            if idx in skip:
                flush()
                continue
            rtype = self._classify_line(line, ctx, lines, idx, in_table)
            if rtype in (RegionType.PARAGRAPH, RegionType.LIST):
                if buffer:
                    prev = buffer[-1][1]
                    gap = line.bbox.y - prev.bbox.y1
                    same_col = _in_columns(prev, ctx) == _in_columns(line, ctx)
                    if gap > ctx.line_height * 1.5 or not same_col:
                        flush()
                buffer.append((idx, line))
                continue

            flush()
            box = line.bbox
            out.append(Region(
                id=stable_id("region", document_id, page_number, "txt", counter),
                document_id=document_id, page_number=page_number, type=rtype,
                bbox=BBoxModel(**box.as_dict()),
                confidence=round(min(0.95, 0.55 + 0.4 * line.confidence), 3),
                reading_order=0, column_index=_in_columns(line, ctx),
                text=line.text, element_ids=line.element_ids(),
                detector=self.name,
                meta={"font_size": round(_font_of(line), 2),
                      "bold": line.is_bold,
                      "font_ratio": round(_font_of(line) / max(1.0, ctx.body_font), 2)}))
            counter += 1
        flush()
        return out

    def _classify_line(self, line: TextLine, ctx: LayoutContext,
                       lines: list[TextLine], idx: int,
                       in_table: set[int]) -> RegionType:
        text = line.text.strip()
        if not text:
            return RegionType.MISC
        font = _font_of(line)
        ratio = font / max(1.0, ctx.body_font)
        y, y1 = line.bbox.y, line.bbox.y1
        words = text.split()

        # Use the centre, not the bottom edge: a tall letterhead logo overhangs
        # the band and would otherwise be promoted to a document title.
        if line.bbox.cy <= ctx.header_band:
            return RegionType.HEADER
        if y >= ctx.footer_band:
            return RegionType.PAGE_NUMBER if PAGE_NUM_RE.match(text) else RegionType.FOOTER
        if PAGE_NUM_RE.match(text) and len(words) <= 4 and y > ctx.page_height * 0.85:
            return RegionType.PAGE_NUMBER
        if FOOTNOTE_RE.match(text) and ratio <= 0.95:
            return RegionType.FOOTNOTE
        if ratio <= 0.82 and y > ctx.page_height * 0.6 and len(words) > 3:
            return RegionType.FOOTNOTE
        if BULLET_RE.match(text):
            return RegionType.LIST

        if ratio >= 1.55 and len(words) <= 24 and y < ctx.page_height * 0.35:
            return RegionType.TITLE
        # A bold, number-bearing sentence is a highlight bullet ("Revenue at
        # X crore, up Y% Y-o-Y"), not a section heading.  Treating it as prose
        # keeps it available to narrative extraction, where its figures belong.
        has_figure = any(ch.isdigit() for ch in text)
        if has_figure and len(words) >= 7 and not text.isupper():
            return RegionType.PARAGRAPH

        is_headingish = (
            len(words) <= 16
            and not HEADING_TAIL_RE.search(text)
            and (ratio >= 1.12 or line.is_bold
                 or (text.isupper() and len(text) > 3 and len(words) <= 12))
        )
        if is_headingish:
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if nxt is not None and (idx + 1) in in_table:
                return RegionType.CAPTION
            return RegionType.SECTION_HEADING
        return RegionType.PARAGRAPH

    # -------------------------------------------------------------- kpi pass
    def _kpi_blocks(self, *, document_id: str, page_number: int,
                    lines: list[TextLine], ctx: LayoutContext,
                    skip: set[int]) -> list[Region]:
        """Detect KPI cards: a prominent number with a small label beside/below.

        These are the side-by-side highlight tiles common on the first page of a
        results release; they are not tables and must not be parsed as one.
        """
        out: list[Region] = []
        for idx, line in enumerate(lines):
            if idx in skip or not line.elements:
                continue
            font = _font_of(line)
            if font < ctx.body_font * 1.25:
                continue
            numeric = [e for e in line.elements if looks_numeric(e.text)]
            if not numeric or len(line.elements) > 4:
                continue
            neighbours = [
                lines[j] for j in (idx - 1, idx + 1)
                if 0 <= j < len(lines) and j not in skip
                and _font_of(lines[j]) < font * 0.8
                and lines[j].bbox.h_overlap_ratio(line.bbox) > 0.35
                and abs(lines[j].bbox.cy - line.bbox.cy) < ctx.line_height * 3.5
            ]
            if not neighbours:
                continue
            box = BBox.union_all([line.bbox] + [n.bbox for n in neighbours])
            if box is None:
                continue
            out.append(Region(
                id=stable_id("region", document_id, page_number, "kpi", idx),
                document_id=document_id, page_number=page_number,
                type=RegionType.KPI_BLOCK, bbox=BBoxModel(**box.as_dict()),
                confidence=0.6, reading_order=0,
                column_index=_in_columns(line, ctx),
                text=" ".join([line.text] + [n.text for n in neighbours]),
                element_ids=line.element_ids() + [
                    e for n in neighbours for e in n.element_ids()],
                detector=self.name,
                meta={"value_line": line.text,
                      "label_lines": [n.text for n in neighbours]}))
        return out


# ------------------------------------------------------------ reading order
def assign_reading_order(regions: list[Region], ctx: LayoutContext) -> None:
    """Column-aware ordering (XY-cut style), applied without moving any box.

    Headers sort first and footers last; body content is ordered by column then
    by vertical position, so a two-column page reads down the left column before
    the right one.  Coordinates are never modified -- order is metadata.
    """
    def column_of(r: Region) -> int:
        box = r.bbox.geom
        # a region spanning >65% of the text width is full-width, not in a column
        if len(ctx.columns) > 1:
            span = box.width / max(1.0, ctx.page_width)
            if span > 0.65:
                return -1
            for i, (x0, x1) in enumerate(ctx.columns):
                if x0 - 6 <= box.cx <= x1 + 6:
                    return i
        return 0

    def band(r: Region) -> int:
        if r.type == RegionType.HEADER:
            return 0
        if r.type in (RegionType.FOOTER, RegionType.PAGE_NUMBER, RegionType.FOOTNOTE):
            return 2
        return 1

    ordered = sorted(
        regions,
        key=lambda r: (band(r), column_of(r) if band(r) == 1 else 0,
                       round(r.bbox.y, 1), round(r.bbox.x, 1)))
    for i, r in enumerate(ordered):
        r.reading_order = i
        if band(r) == 1:
            r.column_index = max(0, column_of(r))
