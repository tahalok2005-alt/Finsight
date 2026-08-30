"""Table structure reconstruction.

Given a table region (a band of visual lines), rebuild the grid: column
boundaries, header hierarchy, row labels and typed data cells -- keeping every
cell's own bounding box so any number remains traceable to the pixels it came
from.

Column detection strategy, in priority order
--------------------------------------------
1. **Vertical ruling lines** inside the band, when the table is drawn with a
   grid.  These are exact.
2. **Whitespace valleys** in the horizontal ink projection of the band.  This is
   what makes borderless financial tables work: the columns of a well-set table
   are separated by consistent vertical gutters even with no rules drawn.
3. **Numeric alignment clustering** as a fallback, since numeric columns are
   right-aligned to a common edge.

Row detection uses the visual lines directly, with a merge pass for wrapped row
labels (a label spilling onto a second line with no numbers of its own).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

import numpy as np

from app.config.settings import settings
from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.layout.cv_signals import LineSegment
from app.models.enums import CellRole, Currency, GrowthType, MagnitudeUnit
from app.models.schemas import (
    BBoxModel, ExtractedTable, OCRElement, TableCell, TableColumn,
)
from app.normalization.numbers import (
    detect_currency, detect_magnitude, looks_numeric, parse_number,
)
from app.normalization.periods import FiscalCalendar, parse_period
from app.ocr.lines import TextLine

log = get_logger("tables.structure")

GROWTH_HEADER_RE = re.compile(
    r"%\s*(chg|change|growth)|y-?o-?y|q-?o-?q|yoy|qoq|\bgrowth\b|\bchange\b|\bvar\b",
    re.I)
SERIAL_HEADER_RE = re.compile(r"^(sr\.?|s\.?\s*no\.?|no\.?|#|sl\.?)\s*$", re.I)
UNIT_DECL_RE = re.compile(
    r"\(\s*(?:in\s+)?(?:rs\.?|inr|₹|\$|usd|us\$)?\s*"
    r"(crore|cr|lakh|lac|million|mn|billion|bn|thousand|000s?)\s*\)?", re.I)


@dataclass(slots=True)
class ColumnBand:
    index: int
    x0: float
    x1: float
    source: str  # "ruling" | "whitespace" | "numeric"

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    def contains(self, x: float, tol: float = 0.0) -> bool:
        return self.x0 - tol <= x <= self.x1 + tol


@dataclass(slots=True)
class RowBand:
    index: int
    lines: list[TextLine] = field(default_factory=list)
    is_header: bool = False

    @property
    def bbox(self) -> BBox:
        return BBox.union_all(ln.bbox for ln in self.lines) or BBox(0, 0, 0, 0)

    @property
    def elements(self) -> list[OCRElement]:
        return [e for ln in self.lines for e in ln.elements]


# ------------------------------------------------------------------ columns
def _columns_from_rulings(v_lines: list[LineSegment], band: BBox,
                          ) -> list[ColumnBand]:
    xs = sorted({round(seg.bbox.cx, 1) for seg in v_lines
                 if seg.bbox.v_overlap_ratio(band) > 0.55
                 and band.x - 8 <= seg.bbox.cx <= band.x1 + 8})
    if len(xs) < 2:
        return []
    edges = [band.x] + xs + [band.x1]
    edges = sorted(set(round(e, 1) for e in edges))
    out: list[ColumnBand] = []
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        if b - a > 6:
            out.append(ColumnBand(index=len(out), x0=a, x1=b, source="ruling"))
    return out if len(out) >= settings.table_min_cols else []


def _columns_from_whitespace(rows: list[RowBand], band: BBox,
                             min_gap: float) -> list[ColumnBand]:
    """Find gutters: x ranges with no ink across the whole band.

    Banner rows are excluded from the projection.  A section title or a wrapped
    sentence that runs the full width of the table has ink in every gutter, and
    a single such row is enough to merge every column into one.  Only rows that
    actually behave like table rows -- several fragments, none of them spanning
    most of the width -- contribute.
    """
    x0, x1 = int(band.x), int(np.ceil(band.x1))
    width = max(1, x1 - x0)

    def is_banner(row: RowBand) -> bool:
        if len(row.elements) < 2:
            return True
        widest = max(el.bbox.width for el in row.elements)
        if widest > width * 0.55:
            return True
        # a contiguous run of text with no internal gap is prose, not a row
        spans = sorted((el.bbox.x, el.bbox.x1) for el in row.elements)
        biggest_gap = max((b[0] - a[1] for a, b in zip(spans, spans[1:])),
                          default=0.0)
        return biggest_gap < min_gap

    contributing = [r for r in rows if not is_banner(r)]
    if len(contributing) < 2:
        contributing = rows  # nothing better available; use everything

    profile = np.zeros(width + 2, dtype=np.int32)
    for row in contributing:
        for el in row.elements:
            a = max(0, int(el.bbox.x) - x0)
            b = min(width, int(np.ceil(el.bbox.x1)) - x0)
            if b > a:
                profile[a:b] += 1
    occupied = profile > 0
    bands: list[tuple[int, int]] = []
    start = None
    for i in range(width + 1):
        if occupied[i]:
            if start is None:
                start = i
        else:
            if start is not None:
                # look ahead: only close the band if the gap is wide enough
                j = i
                while j < width and not occupied[j]:
                    j += 1
                if j - i >= min_gap or j >= width:
                    bands.append((start, i))
                    start = None
    if start is not None:
        bands.append((start, width))
    if len(bands) < settings.table_min_cols:
        return []
    out: list[ColumnBand] = []
    for i, (a, b) in enumerate(bands):
        out.append(ColumnBand(index=i, x0=x0 + a, x1=x0 + b, source="whitespace"))
    return out


def _columns_from_numeric(rows: list[RowBand], band: BBox,
                          tol: float) -> list[ColumnBand]:
    rights: list[float] = []
    for row in rows:
        for el in row.elements:
            if looks_numeric(el.text):
                rights.append(el.bbox.x1)
    if len(rights) < 3:
        return []
    rights.sort()
    clusters: list[list[float]] = [[rights[0]]]
    for x in rights[1:]:
        if x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    centres = [sum(c) / len(c) for c in clusters if len(c) >= 2]
    if not centres:
        return []
    out = [ColumnBand(index=0, x0=band.x, x1=max(band.x, centres[0] - tol * 2.2),
                      source="numeric")]
    for i, c in enumerate(centres):
        left = out[-1].x1
        out.append(ColumnBand(index=i + 1, x0=left, x1=c + tol, source="numeric"))
    out[-1] = ColumnBand(index=out[-1].index, x0=out[-1].x0,
                         x1=max(out[-1].x1, band.x1), source="numeric")
    return out if len(out) >= settings.table_min_cols else []


def detect_columns(rows: list[RowBand], band: BBox, v_lines: list[LineSegment],
                   page_width: int) -> list[ColumnBand]:
    cols = _columns_from_rulings(v_lines, band)
    if cols:
        return cols
    min_gap = max(6.0, page_width * 0.008)
    cols = _columns_from_whitespace(rows, band, min_gap)
    if cols:
        return cols
    tol = max(10.0, page_width * settings.table_column_tolerance_ratio)
    return _columns_from_numeric(rows, band, tol)


# --------------------------------------------------------------------- rows
def build_rows(lines: list[TextLine]) -> list[RowBand]:
    """One visual line per row, then merge continuation lines into their row.

    A line with no numeric fragment that sits directly under a row whose label
    column is occupied is a wrapped label, not a new row.
    """
    rows = [RowBand(index=i, lines=[ln]) for i, ln in enumerate(lines)]
    if len(rows) < 2:
        return rows
    heights = [r.bbox.height for r in rows if r.bbox.height > 0]
    h = median(heights) if heights else 12.0

    merged: list[RowBand] = [rows[0]]
    for row in rows[1:]:
        prev = merged[-1]
        gap = row.bbox.y - prev.bbox.y1
        cur_numeric = any(looks_numeric(e.text) for e in row.elements)
        prev_numeric = any(looks_numeric(e.text) for e in prev.elements)
        # continuation: no numbers of its own, tight to the row above, and it
        # starts at (or right of) the previous row's left edge
        if (not cur_numeric and prev_numeric and gap < h * 0.55
                and row.bbox.x >= prev.bbox.x - 4
                and len(row.elements) <= 3):
            prev.lines.extend(row.lines)
        else:
            merged.append(row)
    for i, r in enumerate(merged):
        r.index = i
    return merged


def mark_header_rows(rows: list[RowBand], cols: list[ColumnBand]) -> int:
    """Return how many leading rows form the header block.

    Zero is a legitimate answer.  A table fragment whose first row is plainly
    data -- typical of a statement split by a wide gap, or continued from the
    previous page -- has no header of its own, and inventing one would silently
    discard a row of real values and label the columns with numbers.
    ``link_continuations`` later gives such fragments their parent's columns.
    """
    n_header = 0
    for i, row in enumerate(rows[:4]):
        texts = [e.text for e in row.elements]
        if not texts:
            break
        numeric = sum(1 for t in texts if looks_numeric(t))
        ratio = numeric / len(texts)
        has_period = any(parse_period(t) for t in texts)
        has_growth = any(GROWTH_HEADER_RE.search(t) for t in texts)
        # header rows are mostly labels; a row of pure years ("FY27 FY26") is
        # still a header even though the tokens look numeric
        if ratio <= 0.45 or has_period or has_growth:
            n_header = i + 1
            row.is_header = True
        else:
            break
    return n_header


# -------------------------------------------------------------- assignment
def _assign_column(el: OCRElement, cols: list[ColumnBand]) -> int:
    """Pick the column band for a fragment.

    Numbers are right-aligned, so the right edge is the reliable anchor; text is
    left-aligned, so the left edge is.  Falls back to maximum overlap.
    """
    anchor = el.bbox.x1 - 1.0 if looks_numeric(el.text) else el.bbox.x + 1.0
    for c in cols:
        if c.contains(anchor):
            return c.index
    best, best_ov = 0, 0.0
    ebox = el.bbox.geom
    for c in cols:
        cb = BBox(c.x0, ebox.y, max(1.0, c.x1 - c.x0), max(1.0, ebox.height))
        ov = cb.h_overlap(ebox)
        if ov > best_ov:
            best, best_ov = c.index, ov
    return best


def build_table(*, document_id: str, page_number: int, region_id: str | None,
                lines: list[TextLine], v_lines: list[LineSegment],
                page_width: int, page_height: int, table_index: int,
                title: str = "", context_text: str = "",
                calendar: FiscalCalendar | None = None,
                ruled: bool = False, base_confidence: float = 0.6,
                entity_hint: str | None = None) -> ExtractedTable | None:
    """Reconstruct one table from its visual lines."""
    if len(lines) < settings.table_min_rows:
        return None
    band = BBox.union_all(ln.bbox for ln in lines)
    if band is None:
        return None

    rows = build_rows(lines)
    cols = detect_columns(rows, band, v_lines, page_width)
    if len(cols) < settings.table_min_cols or len(rows) < settings.table_min_rows:
        return None

    table_id = stable_id("table", document_id, page_number, table_index,
                         round(band.y, 1))
    n_header = mark_header_rows(rows, cols)

    # ----------------------------------------------------------- cell build
    cells: list[TableCell] = []
    grid: dict[tuple[int, int], list[OCRElement]] = {}
    for r in rows:
        for el in r.elements:
            grid.setdefault((r.index, _assign_column(el, cols)), []).append(el)

    unit_hint = detect_magnitude(context_text) or detect_magnitude(title)
    m = UNIT_DECL_RE.search(context_text or "")
    if m and not unit_hint:
        unit_hint = detect_magnitude(m.group(0))
    currency_hint, _ = detect_currency(f"{context_text} {title}")

    for (r_idx, c_idx), els in sorted(grid.items()):
        els.sort(key=lambda e: (e.bbox.y, e.bbox.x))
        text = " ".join(e.text for e in els).strip()
        if not text:
            continue
        box = BBox.union_all(e.bbox.geom for e in els)
        if box is None:
            continue
        is_num = looks_numeric(text)
        parsed = parse_number(text) if is_num else None
        row_is_header = rows[r_idx].is_header if r_idx < len(rows) else False
        if row_is_header:
            role = CellRole.CORNER if c_idx == 0 else CellRole.COLUMN_HEADER
        elif c_idx == 0 or (c_idx <= 1 and not is_num):
            role = CellRole.ROW_LABEL
        else:
            role = CellRole.DATA
        conf = sum(e.confidence for e in els) / len(els)
        cells.append(TableCell(
            id=stable_id("cell", table_id, r_idx, c_idx),
            table_id=table_id, document_id=document_id, page_number=page_number,
            row=r_idx, col=c_idx, text=text, role=role,
            bbox=BBoxModel(**box.as_dict()), confidence=round(conf, 4),
            numeric_value=parsed[0] if parsed else None,
            raw_numeric_text=text if is_num else None, is_numeric=is_num,
            element_ids=[e.id for e in els]))

    if not cells:
        return None

    # --------------------------------------------------------- column model
    # A statement's "Quarter Ended / Year Ended" banner sometimes sits just
    # above the detected band rather than inside it.  The page context carries
    # it, so it is offered as a fallback span for bare-date column heads.
    columns = _build_columns(cells, cols, rows, n_header, calendar,
                             context_text=context_text)

    n_data_rows = len(rows) - n_header
    numeric_cells = sum(1 for c in cells if c.is_numeric)
    density = numeric_cells / max(1, n_data_rows * max(1, len(cols) - 1))
    confidence = min(0.98, base_confidence + 0.2 * min(1.0, density)
                     + (0.1 if ruled else 0.0)
                     + (0.05 if any(c.normalized_period for c in columns) else 0.0))

    warnings: list[str] = []
    if n_data_rows < 1:
        warnings.append("table has a header but no data rows")
    if density < 0.25:
        warnings.append(f"low numeric density ({density:.2f}); structure uncertain")

    return ExtractedTable(
        id=table_id, document_id=document_id, page_number=page_number,
        region_id=region_id, title=title.strip(), caption=context_text.strip()[:500],
        bbox=BBoxModel(**band.as_dict()), n_rows=len(rows), n_cols=len(cols),
        columns=columns, cells=cells,
        detector=f"{cols[0].source}+lines" if cols else "lines",
        confidence=round(confidence, 3), has_ruling_lines=ruled,
        unit_hint=unit_hint, currency_hint=currency_hint, warnings=warnings)


_SPAN_QUARTER = re.compile(r"quarter\s+ended|three\s+months?\s+ended", re.I)
_SPAN_YEAR = re.compile(r"year\s+ended|twelve\s+months?\s+ended|full\s+year", re.I)


def _build_columns(cells: list[TableCell], cols: list[ColumnBand],
                   rows: list[RowBand], n_header: int,
                   calendar: FiscalCalendar | None,
                   context_text: str = "") -> list[TableColumn]:
    """Derive the column model, including multi-line header paths and periods.

    Two header shapes must both work:

    * stacked labels -- "1Q" over "FY27" -- joined in visual order so the period
      parser sees "1Q FY27";
    * a *spanning* label -- "Quarter Ended" centred above several dated columns,
      each headed only "30th Jun'26".  A spanning header cell is attached to
      every column band its box overlaps, which is what lets a bare date resolve
      to a quarter instead of being dropped.
    """
    out: list[TableColumn] = []
    for c in cols:
        band_w = max(1.0, c.x1 - c.x0)
        parts: list[str] = []
        for r in range(n_header):
            row_cells = [cl for cl in cells if cl.row == r]
            own = next((cl for cl in row_cells if cl.col == c.index), None)
            if own is not None and own.text.strip():
                parts.append(own.text.strip())
                continue
            # no cell of its own in this header row: look for a spanning cell
            for cl in row_cells:
                if cl.col == c.index or not cl.text.strip():
                    continue
                overlap = min(cl.bbox.x1, c.x1) - max(cl.bbox.x, c.x0)
                if overlap / band_w > 0.35 and cl.bbox.width > band_w * 1.3:
                    parts.append(cl.text.strip())
                    break
        header = " ".join(parts).strip()

        # the span word may sit anywhere in this table's header block
        span_hint: str | None = None
        header_blob = " ".join(
            cl.text for cl in cells if cl.row < n_header and cl.text)
        if _SPAN_QUARTER.search(header):
            span_hint = "quarter"
        elif _SPAN_YEAR.search(header):
            span_hint = "year"
        elif _SPAN_QUARTER.search(header_blob) and not _SPAN_YEAR.search(header):
            span_hint = "quarter"
        elif _SPAN_QUARTER.search(context_text or ""):
            # the banner sits above the band: assume the quarterly columns it
            # governs, except where this column names an audited year-end
            span_hint = "year" if _SPAN_YEAR.search(header) else "quarter"
        data = [cl for cl in cells if cl.col == c.index and cl.row >= n_header]
        numeric_ratio = (sum(1 for d in data if d.is_numeric) / len(data)
                         if data else 0.0)
        period = (parse_period(header, calendar=calendar, default_span=span_hint)
                  if header else None)
        is_growth = bool(header and GROWTH_HEADER_RE.search(header))
        gtype: GrowthType | None = None
        if is_growth:
            low = header.lower()
            if "q-o-q" in low or "qoq" in low:
                gtype = GrowthType.QOQ
            elif "y-o-y" in low or "yoy" in low:
                gtype = GrowthType.YOY
            else:
                gtype = GrowthType.UNKNOWN
        out.append(TableColumn(
            index=c.index, header=header, header_path=parts,
            x_start=round(c.x0, 1), x_end=round(c.x1, 1),
            period_label=period.raw_text if period else None,
            normalized_period=period.label if period else None,
            is_growth_column=is_growth, growth_type=gtype,
            numeric_ratio=round(numeric_ratio, 3)))
    return out
