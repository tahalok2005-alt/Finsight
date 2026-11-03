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
from collections import Counter
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
    currencies_in, holds_more_than_one_number, repair_by_decimals,
    table_decimals, table_grouping,
    detect_currency, detect_magnitude, looks_numeric, parse_number,
    unambiguous_currency,
)
from app.normalization.periods import (
    FiscalCalendar, looks_like_period_fragment, looks_like_period_header,
    parse_period, parse_period_levels,
)
from app.ocr.lines import TextLine
from app.tables.header_block import (
    HeaderBlock, build_header_block, names_more_than_one_date,
)

log = get_logger("tables.structure")

#: A column that compares periods rather than reporting one. Written as a
#: word ("growth", "change", "Y-o-Y"), as a delta sign (SAP heads its change
#: column "∆ in %", which OCR reads as "△ in %"), or as a comparison of two
#: periods ("Q1FY27 over Q4FY26", "Q1 FY27 vs Q1 FY26"). "Y-0-Y" is how the
#: recogniser reads "Y-o-Y" at slide resolution.
#: A footnote marker printed on the word -- "Change¹" -- is read as "Change1",
#: "Change¹" or "Changel", none of which ends at a word boundary.
GROWTH_HEADER_RE = re.compile(
    r"%\s*(chg|change|growth)|y\s*-?\s*[o0]\s*-?\s*y|q\s*-?\s*[o0]\s*-?\s*q|yoy|qoq"
    r"|\b(?:growth|change)(?:[1-9¹²³]|[li])?\b|\bvar\b|[∆Δ△]"
    r"|\b(?:q[1-4]|h[12]|fy|9m)\s*'?\s*\S*\s+(?:over|vs\.?|versus)\s+(?:q[1-4]|h[12]|fy|9m)",
    re.I)
SERIAL_HEADER_RE = re.compile(r"^(sr\.?|s\.?\s*no\.?|no\.?|#|sl\.?)\s*$", re.I)
#: A bare four-digit year, as it appears on the second line of a split
#: date column head ("June 28," / "2025").
_BARE_YEAR = re.compile(r"(?:19|20)\d{2}")
#: An explicit unit declaration above a table.  Both orderings occur in the
#: wild -- "(Rs in crore)" and "(in Rs crore)" -- and the currency symbol may be
#: missing entirely when OCR could not emit it, so every part is optional except
#: the magnitude word itself.
UNIT_DECL_RE = re.compile(
    r"\(\s*(?:in\s+)?(?:rs\.?|inr|₹|\$|usd|us\$)?\s*(?:in\s+)?"
    r"(crore|cr|lakh|lac|million|mn|billion|bn|thousand|000s?)\b[^)]{0,30}\)?",
    re.I)


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
#: How many rows' figures a vertical line may pass through before it is known
#: not to be a column rule. One is tolerated: a figure set too wide for its
#: column, or a misread box, can overhang a genuine rule once.
_MAX_FIGURES_CROSSED = 1


def _figures_crossed(x: float, rows: list[RowBand] | None) -> int:
    """How many rows have a figure whose ink the vertical line at ``x`` cuts."""
    if not rows:
        return 0
    crossed = 0
    for row in rows:
        if any(looks_numeric(el.text) and el.bbox.x + 2.0 < x < el.bbox.x1 - 2.0
               for el in row.elements):
            crossed += 1
    return crossed


def _columns_from_rulings(v_lines: list[LineSegment], band: BBox,
                          rows: list[RowBand] | None = None,
                          ) -> list[ColumnBand]:
    xs = sorted({round(seg.bbox.cx, 1) for seg in v_lines
                 if seg.bbox.v_overlap_ratio(band) > 0.55
                 and band.x - 8 <= seg.bbox.cx <= band.x1 + 8})
    # A column rule runs between figures, never through them. The line finder
    # also returns strokes that are not rules -- a border of the next table,
    # a scanner streak, part of a glyph -- and on a bank's side-by-side
    # standalone and consolidated statement a cluster of them produced
    # seventeen columns for eight: the consolidated dates were cut away from
    # the figures beneath them, and those figures lost their periods. A line
    # that passes through the ink of figures on several rows is not a rule.
    xs = [x for x in xs if _figures_crossed(x, rows) <= _MAX_FIGURES_CROSSED]
    if len(xs) < 2:
        return []
    edges = [band.x] + xs + [band.x1]
    edges = sorted(set(round(e, 1) for e in edges))
    # A band too narrow to hold a character is the space between two strokes
    # of one rule, not a column.
    min_width = 6.0
    if rows:
        heights = sorted(el.bbox.height for r in rows for el in r.elements)
        if heights:
            min_width = max(min_width, 0.8 * heights[len(heights) // 2])
    out: list[ColumnBand] = []
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        if b - a > min_width:
            out.append(ColumnBand(index=len(out), x0=a, x1=b, source="ruling"))
        elif out:
            # Absorbed by its left neighbour, so no strip of the band is lost.
            out[-1] = ColumnBand(index=out[-1].index, x0=out[-1].x0, x1=b,
                                 source="ruling")
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


def _columns_from_numeric(rows: list[RowBand], band: BBox, tol: float,
                          expected: int = 0) -> list[ColumnBand]:
    # Keep each figure's left edge alongside its right edge.  The right edges
    # are what cluster (the column is right-aligned), but the left edges say
    # where the column's ink actually begins -- which is what stops the label
    # column's band from reaching under the first value column's header date.
    edges: list[tuple[float, float]] = []
    for row in rows:
        for el in row.elements:
            if _is_amount(el.text):
                edges.append((el.bbox.x1, el.bbox.x))
    if len(edges) < 3:
        return []
    edges.sort()
    clusters: list[list[tuple[float, float]]] = [[edges[0]]]
    for pair in edges[1:]:
        if pair[0] - clusters[-1][-1][0] <= tol:
            clusters[-1].append(pair)
        else:
            clusters.append([pair])
    kept = [c for c in clusters if len(c) >= 2]
    centres = [sum(right for right, _ in c) / len(c) for c in kept]
    lefts = [min(left for _, left in c) for c in kept]
    if not centres:
        return []

    # One column can split into two clusters when some of its figures carry a
    # trailing currency symbol ("66,613 $"), pushing their right edge past the
    # tolerance.  The data says how many value columns there are -- a data row
    # holds one figure per column -- so where clustering produced more, the
    # closest neighbours are merged until the count matches.  Merging is driven
    # by that evidence, never by a target taken from anywhere else.
    while expected and len(centres) > expected >= 1:
        gaps = [(centres[i + 1] - centres[i], i) for i in range(len(centres) - 1)]
        _, i = min(gaps)
        centres[i:i + 2] = [(centres[i] + centres[i + 1]) / 2.0]
        lefts[i:i + 2] = [min(lefts[i], lefts[i + 1])]

    # The label column stops where the first value column's ink starts.  Using
    # a fixed offset back from the first right edge instead left the label band
    # reaching under the value column's centred header date, which then
    # attached to the label column rather than to the figures it heads.
    first_left = max(band.x, lefts[0] - tol * 0.5)
    out = [ColumnBand(index=0, x0=band.x, x1=max(band.x, first_left),
                      source="numeric")]
    for i, c in enumerate(centres):
        left = max(out[-1].x1, lefts[i] - tol * 0.5) if i else out[-1].x1
        out.append(ColumnBand(index=i + 1, x0=left, x1=c + tol, source="numeric"))
    out[-1] = ColumnBand(index=out[-1].index, x0=out[-1].x0,
                         x1=max(out[-1].x1, band.x1), source="numeric")
    return out if len(out) >= settings.table_min_cols else []


#: A serial number ("1", "12") printed in its own column beside the labels.
_SERIAL = re.compile(r"^\s*\(?\d{1,2}[.)]?\s*$")


def _is_amount(text: str) -> bool:
    """A figure of the table's body, as opposed to a row's serial number."""
    return looks_numeric(text) and not _SERIAL.match(text or "")


def _value_bands(cols: list[ColumnBand], rows: list[RowBand]) -> int:
    """How many of the bands hold figures, by where the figures' right edges
    fall (figures are right-aligned)."""
    held = set()
    for row in rows:
        for el in row.elements:
            if _is_amount(el.text):
                x = el.bbox.x1 - 1.0
                for c in cols:
                    if c.contains(x):
                        held.add(c.index)
                        break
    return len(held)


def detect_columns(rows: list[RowBand], band: BBox, v_lines: list[LineSegment],
                   page_width: int) -> list[ColumnBand]:
    # How many value columns should there be?  A data row answers it directly:
    # it carries one figure per value column.  The modal count across data rows
    # is the document's own statement of its width. Serial numbers are not
    # figures: a statement numbering some rows and not others ("1 Interest
    # earned" over "(a) Interest on advances") otherwise states two widths.
    counts = [sum(1 for el in row.elements if _is_amount(el.text))
              for row in rows]
    counts = [n for n in counts if n >= 2]
    expected = Counter(counts).most_common(1)[0][0] if counts else 0

    # Rulings are the strongest evidence of a column -- where they are all
    # there. A line finder that caught some rules and missed others (a bank's
    # consolidated half, printed with fainter rules than its standalone half)
    # leaves bands holding two and three columns of figures each. Rulings are
    # taken only when they give every column of figures a band of its own.
    cols = _columns_from_rulings(v_lines, band, rows)
    if cols and (not expected or _value_bands(cols, rows) >= expected):
        return cols
    if cols:
        log.info("tables.rulings_under_segmented",
                 expected_value_columns=expected,
                 ruled_value_columns=_value_bands(cols, rows))

    min_gap = max(6.0, page_width * 0.008)
    tol = max(10.0, page_width * settings.table_column_tolerance_ratio)
    whitespace = _columns_from_whitespace(rows, band, min_gap)
    numeric = _columns_from_numeric(rows, band, tol, expected=expected)

    # Whitespace projection is the better detector in general, but it fails on
    # one common layout: where figures are right-aligned and some rows carry
    # much longer numbers (share counts beside dollar amounts), the long rows
    # reach across the gutters the short rows leave, so the union of ink is
    # continuous and every value column merges into one.  Clustering the right
    # edges survives that, because right-alignment is exactly what the layout
    # guarantees.  Pick whichever candidate matches the width the data implies.
    # The switch is deliberately one-directional.  Whitespace projection is the
    # better detector wherever it works, and it is what the validated benchmark
    # relies on; preferring numeric clustering whenever it merely scored better
    # cost four points of F1 on that benchmark.  So numeric alignment is used
    # only for the specific failure it repairs -- whitespace welding value
    # columns *together* and returning fewer than the data has -- and never to
    # second-guess a whitespace reading that already covers the data's width.
    ws_value_cols = len(whitespace) - 1 if whitespace else 0
    num_value_cols = len(numeric) - 1 if numeric else 0
    if expected and whitespace and ws_value_cols < expected <= num_value_cols:
        log.info("tables.columns_from_numeric_alignment",
                 reason="whitespace under-segmented the value columns",
                 expected_value_columns=expected,
                 whitespace_value_columns=ws_value_cols,
                 numeric_value_columns=num_value_cols)
        return numeric
    return whitespace or numeric


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
        # Header *shape*, not a resolved period: a split date head whose
        # year is on the next line still marks a header row.
        has_period = any(looks_like_period_header(t) for t in texts)
        has_growth = any(GROWTH_HEADER_RE.search(t) for t in texts)
        # A row made up entirely of bare four-digit years is the second line of
        # a split date header -- "June 28," on one line and "2025" on the next
        # is how US statements set their column heads.  A bare year is
        # deliberately not parsed as a period on its own (too ambiguous), so
        # this has to be recognised structurally.  It is safe: a data row of
        # nothing but bare years carries no label and no separators, which real
        # financial data always has.
        year_row = (len(texts) >= 2
                    and all(_BARE_YEAR.fullmatch(t.strip()) for t in texts))
        if ratio <= 0.45 or has_period or has_growth or year_row:
            n_header = i + 1
            row.is_header = True
        else:
            break
    return n_header


def _label_column_index(rows: list[RowBand], cols: list[ColumnBand]) -> int:
    """Which column holds the row labels.

    Almost always the first. But a statement can print its labels in the
    middle, with figures on both sides -- one consumer-goods filer sets the
    current quarters to the left of "Particulars" and the prior quarter and
    year to the right -- and read as the first column, the June figures
    became every row's label and the page produced nothing.

    The middle is chosen only on the page's own evidence: the column holding
    the most words, with figures and no labels to its left and figures to its
    right. Anything else is the first column, as before.
    """
    if len(cols) < 3:
        return 0
    words: Counter = Counter()
    figures: Counter = Counter()
    for row in rows:
        for el in row.elements:
            index = _assign_column(el, cols)
            if _is_amount(el.text):
                figures[index] += 1
            elif sum(ch.isalpha() for ch in el.text or "") >= 3:
                words[index] += 1
    if not words:
        return 0
    best = max(words, key=lambda i: (words[i], -i))
    if best == 0:
        return 0
    left = [c.index for c in cols if c.index < best]
    right = [c.index for c in cols if c.index > best]
    if (right and any(figures[i] for i in right)
            and any(figures[i] for i in left)
            and all(figures[i] >= 3 * words[i] for i in left
                    if figures[i] or words[i])):
        return best
    return 0


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
                page_lines: list[TextLine] | None = None,
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
    # The header block is read as a structure before anything else looks at it.
    # `mark_header_rows` stops after four rows, and at the first row carrying
    # numbers -- one row too early for a statement that sets its dates over
    # "30 June" / "2026" / "(Unaudited)". The year never became a header level,
    # so no column could resolve a period. `mark_header_rows` stays as the
    # fallback for tables the block declines to read.
    label_index = _label_column_index(rows, cols)
    header_block = build_header_block(rows, cols, table_id=table_id,
                                      label_index=label_index)
    n_header = header_block.n_rows or mark_header_rows(rows, cols)
    for row in rows[:n_header]:
        row.is_header = True

    # ----------------------------------------------------------- cell build
    cells: list[TableCell] = []
    grid: dict[tuple[int, int], list[OCRElement]] = {}
    for r in rows:
        for el in r.elements:
            grid.setdefault((r.index, _assign_column(el, cols)), []).append(el)

    # An explicit unit *declaration* -- "(Rs in crore)" -- is authoritative.
    # A bare magnitude word anywhere in the surrounding prose is not: a page
    # whose bullets mention "533 million subscribers" would otherwise declare
    # its rupee table to be in millions.  The declaration is therefore tried
    # first, and loose prose only as a last resort.
    # Sources are tried most-specific first.  The declaration frequently sits
    # on the table's own first line -- it is part of the band, not of the
    # caption above it -- so the table's own text is searched before the
    # surrounding context, whose window can catch narrative prose ("533
    # million subscribers") that has nothing to do with the table's unit.
    own_text = "\n".join(ln.text for ln in lines[:3])
    unit_hint = None
    currency_hint = None

    # A banner is the strongest unit evidence a table can offer, and it is
    # structural rather than lexical: the header block calls a cell a banner
    # because it spans every data column, which is precisely what a table-wide
    # declaration does. Reading it this way catches wordings the bracketed
    # pattern below misses -- "(All amounts are in INR Crores, unless otherwise
    # stated)" opens with three words of prose before the currency, so
    # UNIT_DECL_RE does not match it, and Groww's entire statement came through
    # with no scale at all.
    #
    # A magnitude word is still required: a banner is often just the table's
    # title, and a title naming no scale declares nothing.
    #: The table's own statement of its unit, when it makes one. Recorded so
    #: that a later stage can tell "this table declared its unit and its
    #: currency glyph was illegible" from "this table declared nothing" --
    #: only the second may borrow the document's currency.
    declaration_text: str | None = None
    for banner in header_block.banners:
        magnitude = detect_magnitude(banner)
        if magnitude is not None:
            unit_hint = magnitude
            currency_hint = unambiguous_currency(banner)
            declaration_text = banner
            break

    decl = None
    if unit_hint is None:
        for source in (own_text, context_text or "", title or ""):
            decl = UNIT_DECL_RE.search(source)
            if decl:
                unit_hint = detect_magnitude(decl.group(0))
                currency_hint, _ = detect_currency(decl.group(0))
                declaration_text = decl.group(0)
                break
    if unit_hint is None:
        unit_hint = detect_magnitude(title) or detect_magnitude(context_text)
    if currency_hint is None:
        # Currency follows the same discipline as magnitude, and one rule more:
        # a source naming two currencies decides nothing.  Segment captions in
        # Indian filings routinely print the rupee figure with its dollar
        # equivalent in brackets -- "201,803 crore ($ 21.3 billion)" -- and the
        # rupee glyph is frequently lost in extraction, leaving only the "$".
        # Reading that caption would stamp a rupee table as dollars.  So only
        # the table's own text and its title are consulted, and only when they
        # are unambiguous; otherwise the document-level currency applies.
        hint_from_declaration = False
        for source in (own_text, title or ""):
            currency_hint = unambiguous_currency(source)
            if currency_hint is not None:
                break
    else:
        hint_from_declaration = True
    # The table's own figures are table-scoped evidence. A US statement prints
    # "$" on its first row and its totals and nowhere else; the symbol is the
    # statement's currency, and every figure in the table is in it. But a
    # table whose figures carry TWO currencies -- a rupee segment table with a
    # dollar equivalent -- has no single currency to lend its unmarked rows,
    # whatever its first lines happened to show. Figures that carry a symbol
    # keep their own; the rest are unknown.
    cell_currencies = currencies_in(
        " ".join(e.text for els in grid.values() for e in els))
    if currency_hint is None and len(cell_currencies) == 1:
        currency_hint = next(iter(cell_currencies))
    elif (currency_hint is not None and len(cell_currencies) > 1
          and not hint_from_declaration):
        currency_hint = None

    # A currency named in the heads of some columns and not the others
    # belongs to those columns. An Indian filer prints a convenience
    # translation beside its rupee statement -- "Convenience translation into
    # U.S. Dollar in millions" over one column -- and read as the table's own
    # text, those words made every rupee figure dollars.
    column_currency: dict[str, str] = {}
    if header_block is not None and len(header_block.columns) >= 2:
        named = {}
        for index, head in header_block.columns.items():
            found = unambiguous_currency(" ".join(head.lines))
            if found is not None:
                named[index] = found
        if named and len(named) < len(header_block.columns):
            column_currency = {str(i): c.value for i, c in named.items()}
            if (currency_hint is not None and not hint_from_declaration
                    and currency_hint in named.values()):
                currency_hint = None

    # Cross-check the two against each other.  "Crore" and "lakh" are used only
    # for rupees, so a table whose magnitude is one of them cannot be reporting
    # dollars unless a declaration said so outright.  Segment pages pair every
    # rupee figure with a dollar equivalent in brackets, and with the rupee
    # glyph routinely lost in extraction the stray "$" is the only symbol left
    # to find.  Rather than assert the wrong currency, drop it and let the
    # document-level currency apply.
    if (currency_hint is not None and currency_hint != Currency.INR
            and unit_hint in (MagnitudeUnit.CRORE, MagnitudeUnit.LAKH)
            and not (decl and unambiguous_currency(decl.group(0)) == currency_hint)):
        log.info("tables.currency_rejected", reason="india_only_magnitude",
                 rejected=str(currency_hint), unit=str(unit_hint))
        currency_hint = None

    # The table's thousands convention, from its unambiguous figures. It is
    # what tells "166 632" (two columns' figures run together, in a table that
    # groups with commas) from "18 090.16" (one figure, in a table that groups
    # with spaces).
    grouping = table_grouping(" ".join(e.text for e in els)
                              for els in grid.values())
    decimals = table_decimals(" ".join(e.text for e in els)
                              for els in grid.values())
    for (r_idx, c_idx), els in sorted(grid.items()):
        els.sort(key=lambda e: (e.bbox.y, e.bbox.x))
        text = " ".join(e.text for e in els).strip()
        if not text:
            continue
        box = BBox.union_all(e.bbox.geom for e in els)
        if box is None:
            continue
        # A period head -- "1Q FY27", "30.06.2026", "FY26" -- starts with digits
        # but is not a figure. Flagged numeric, "1Q FY27" was parsed as the
        # number 1.
        is_num = looks_numeric(text) and not looks_like_period_header(text)
        # A cell holding two columns' figures has no single value. Parsing it
        # returned their concatenation -- 20,211 and 17,292 as 2,021,117,292 --
        # and that number was then a cell value like any other.
        repaired = repair_by_decimals(text, decimals) or text
        parsed = (parse_number(repaired)
                  if is_num and not holds_more_than_one_number(repaired, grouping)
                  else None)
        row_is_header = rows[r_idx].is_header if r_idx < len(rows) else False
        if row_is_header:
            role = CellRole.CORNER if c_idx == label_index else CellRole.COLUMN_HEADER
        elif (c_idx == label_index
              or (label_index == 0 and c_idx <= 1 and not is_num)):
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
    # A statement's column head is often printed above the band the layout
    # stage cut, so look for it there before giving up on dating the columns.
    inside_band = {ln.id for ln in lines}
    head_above = _head_lines_above(
        [ln for ln in (page_lines or []) if ln.id not in inside_band],
        band, cols, rows)
    levels_above = [[(e.bbox.geom, e.text.strip()) for e in ln.elements]
                    for ln in head_above]
    # A band cut at its first row of figures has no head of its own, and the
    # head recovered above it must be read the way any head is: group heads
    # partitioning the columns they govern, only period words reaching the
    # period. Read by overlap alone, "Quarter ended" centred over three
    # columns reached only the one it overlapped, and the other two -- bare
    # dates, beside a "Year ended" -- would have been read as balances.
    if header_block.n_rows == 0 and head_above:
        above_rows = [RowBand(index=-1 - i, lines=[ln])
                      for i, ln in enumerate(head_above)]
        recovered = build_header_block(above_rows + rows, cols, table_id=table_id,
                                       label_index=label_index)
        for row in rows:
            row.is_header = row.index < n_header
        if any(h.qualified_period_text for h in recovered.columns.values()):
            header_block = recovered
    columns = _build_columns(cells, cols, rows, n_header, calendar,
                             context_text=context_text,
                             levels_above=levels_above,
                             header_block=header_block)

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
        unit_hint=unit_hint, currency_hint=currency_hint, warnings=warnings,
        meta=_table_meta(declaration_text, header_block, grouping, decimals,
                         column_currency, label_index))


def _table_meta(declaration_text: str | None, header_block,
                grouping: str | None = None,
                decimals: str | None = None,
                column_currency: dict | None = None,
                label_index: int = 0) -> dict:
    """What the reconstruction learned that the schema has no field for."""
    meta: dict = {"grouping": grouping, "decimals": decimals}
    if label_index:
        meta["label_column"] = label_index
    if column_currency:
        meta["column_currency"] = dict(column_currency)
    if declaration_text:
        meta["unit_declaration"] = declaration_text.strip()[:120]
    if header_block is not None:
        # Keys are strings so the meta survives a JSON round trip unchanged.
        bases = {str(i): h.basis for i, h in header_block.columns.items()
                 if h.basis}
        if bases:
            meta["column_basis"] = bases
    return meta


# The whitespace is optional throughout: OCR loses the space inside a tightly
# set column head ("Quarterended", "Yearended") often enough that requiring it
# costs real periods.
_SPAN_QUARTER = re.compile(r"quarter\s*ended|three\s*months?\s*ended", re.I)
_SPAN_YEAR = re.compile(r"year\s*ended|twelve\s*months?\s*ended|full\s*year", re.I)
#: Any span other than a quarter or a year, which makes a header block
#: ambiguous rather than merely quarterly.
_OTHER_SPAN = re.compile(
    r"(?:six|nine|half[- ]?year)\s*(?:months?)?\s*end(?:ed|ing)", re.I)


#: Head text that is not a period but belongs to a column head all the same.
_HEAD_METADATA = re.compile(
    r"^\(?\s*(?:un\s*-?\s*audited|audited|reviewed|unreviewed|provisional"
    r"|refer\s+note\s*\w*)\s*\)?$", re.I)


#: Head text of a column that compares periods: the connector between them
#: ("Q1FY27 / over / Q4FY26", "vs") and the change head itself ("Growth (%)",
#: "YoY", "% chg"). A bank's slide printed its growth heads over three lines;
#: recovery stopped at "over", kept only "Q4FY26", and every growth rate was
#: reported as that quarter's value -- interest income of 3.90 for Q4 FY26.
_HEAD_COMPARISON = re.compile(
    r"^\(?\s*(?:over|vs\.?|versus|v/s|compared\s+(?:to|with)|growth|change|chg\.?"
    r"|variance|var\.?|y\s*-?\s*o\s*-?\s*y|q\s*-?\s*o\s*-?\s*q|%\s*(?:chg|change|growth)"
    r"|(?:growth|change)\s*\(\s*%\s*\)|\(\s*%\s*\)|%)\s*\)?$", re.I)


def _is_head_fragment(text: str) -> bool:
    """A short piece of text that reads as part of a column head: a piece of
    a period, the audit status and note reference printed under one, or the
    words of a comparison between periods."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 60 and (looks_like_period_fragment(t)
                                         or bool(_HEAD_METADATA.match(t))
                                         or bool(_HEAD_COMPARISON.match(t)))


def _head_lines_above(lines: list[TextLine], band: BBox,
                      cols: list[ColumnBand],
                      rows: list[RowBand] | None = None) -> list[TextLine]:
    """Recover column-head levels printed *above* the detected table band.

    Layout analysis cuts a table region at the first row of figures, so a
    statement whose head sits in the white space above -- a span banner, a date
    row and a wrapped year row -- loses its head entirely and every column comes
    out undated.  The head is still on the page; it is simply above the cut.

    This walks upward from the top of the band, taking each text line that lies
    within the table's horizontal extent and reads as a header fragment, and
    stopping at the first line that does not (a title, a unit declaration, or
    body text).  Nothing is invented: the lines returned are lines the OCR
    actually read, in the order they are printed, holding only their pieces
    that stand over the value columns.
    """
    if not lines or not cols:
        return []
    value_x0 = cols[0].x1 if len(cols) > 1 else band.x
    # Where the figures begin -- the first band holding one. A serial-number
    # column at index 0 would otherwise make the label column part of the
    # value area, and "Particulars" part of the head.
    # A column holds figures when they outnumber its words: a label quoting
    # a face value ("... Rs.10/-") does not make the label column a figure
    # column.
    if rows:
        figures: Counter = Counter()
        words: Counter = Counter()
        for r in rows:
            for e in r.elements:
                home = next((c.index for c in cols
                             if c.contains(e.bbox.x1 - 1.0)), None)
                if home is None:
                    continue
                if _is_amount(e.text):
                    figures[home] += 1
                elif sum(ch.isalpha() for ch in e.text or "") >= 3:
                    words[home] += 1
        holding = [c for c in cols
                   if figures[c.index] >= 2 and figures[c.index] > words[c.index]]
        if holding:
            value_x0 = min(c.x0 for c in holding)
    # A column head sits over the columns it heads.  Text that starts inside
    # the row-label column and runs on into the value columns is addressing
    # the whole page -- a statement title such as "Results for the quarter
    # ended 30 June 2026" -- and reading it as a banner would give every
    # column the same period; it ends the head. Text lying wholly inside the
    # label column is the label column's own head ("S. No.", "Particulars")
    # and is set aside: the dates printed on the same line as "Particulars"
    # are exactly the head being looked for, and discarding the line for its
    # first word lost a bank's whole head.
    left_limit = value_x0 - 0.10 * max(1.0, band.x1 - value_x0)
    candidates = [ln for ln in lines
                  if ln.bbox.y1 <= band.y + 2 and ln.elements
                  and ln.bbox.x1 >= left_limit and ln.bbox.x1 <= band.x1 + 8]
    candidates.sort(key=lambda ln: ln.bbox.y, reverse=True)

    found: list[TextLine] = []
    previous_top = band.y
    for line in candidates:
        # A gap much larger than the line's own height means the head ended and
        # something unrelated is being read.
        if previous_top - line.bbox.y1 > max(24.0, line.bbox.height * 2.5):
            break
        pieces = [e for e in line.elements if e.text and e.text.strip()]
        if any(e.bbox.x < left_limit and e.bbox.x1 > value_x0 for e in pieces):
            break
        kept = [e for e in pieces if e.bbox.x >= left_limit]
        if not kept:
            previous_top = line.bbox.y
            continue
        if not all(_is_head_fragment(e.text) for e in kept):
            break
        found.append(TextLine(id=line.id, page_number=line.page_number,
                              elements=kept,
                              bbox=BBox.union_all(e.bbox.geom for e in kept)))
        previous_top = line.bbox.y
    found.reverse()          # printed order: banner first, year last
    return found


def _build_columns(cells: list[TableCell], cols: list[ColumnBand],
                   rows: list[RowBand], n_header: int,
                   calendar: FiscalCalendar | None,
                   context_text: str = "",
                   levels_above: list[list[tuple[BBox, str]]] | None = None,
                   header_block: HeaderBlock | None = None,
                   ) -> list[TableColumn]:
    """Derive the column model, including multi-line header paths and periods.

    Two header shapes must both work:

    * stacked labels -- "1Q" over "FY27" -- joined in visual order so the period
      parser sees "1Q FY27";
    * a *spanning* label -- "Quarter Ended" centred above several dated columns,
      each headed only "30th Jun'26".  A spanning header cell is attached to
      every column band its box overlaps, which is what lets a bare date resolve
      to a quarter instead of being dropped.
    """
    # ---- the header stack, as levels of (box, text) ------------------------
    # Levels recovered from above the band come first, because they are printed
    # above the table's own header rows.
    levels: list[list[tuple[BBox, str]]] = list(levels_above or [])
    for r in range(n_header):
        row_level = [(cl.bbox.geom, cl.text.strip()) for cl in cells
                     if cl.row == r and cl.text.strip()]
        if row_level:
            levels.append(row_level)

    # A header fragment that reaches across more than one column band is a
    # span banner ("Three Months Ended" over two dated columns).  Counting the
    # bands it touches is a sounder test than comparing its width to any single
    # band, because band widths follow the figures beneath them: the same
    # banner can be 1.5x one band and 1.2x its neighbour.
    def _touches(box: BBox) -> int:
        n = 0
        for b in cols:
            ov = min(box.x1, b.x1) - max(box.x, b.x0)
            if ov > 0.15 * max(1.0, b.x1 - b.x0):
                n += 1
        return n

    banner = {id(box): _touches(box) >= 2
              for level in levels for box, _ in level}

    out: list[TableColumn] = []
    for c in cols:
        band_w = max(1.0, c.x1 - c.x0)
        # ---- header hierarchy, level by level, decided geometrically -------
        # A column head is a stack: a span banner may cover several columns,
        # the date sits under it, and the year often wraps onto a third row.
        # Which fragment belongs to this column is a question about boxes, not
        # about any cell's column index -- dates are centred over their column
        # while the figures beneath them are right-aligned, so a date's index is
        # frequently one band off.  Each level therefore takes the fragment that
        # overlaps this band most, and a banner is shared by every band it
        # covers.
        parts: list[str] = []
        for level in levels:
            best: tuple[float, str] | None = None
            for box, text in level:
                overlap = min(box.x1, c.x1) - max(box.x, c.x0)
                if overlap <= 0:
                    continue
                cell_w = max(1.0, box.width)
                covers = overlap / band_w
                inside = overlap / cell_w
                if banner.get(id(box)) or cell_w > band_w * 1.3:
                    # a banner needs only to cover the band
                    if covers < 0.35:
                        continue
                    score = covers
                elif inside < 0.5 and covers < 0.5:
                    # a leaf fragment must sit substantially inside it
                    continue
                else:
                    score = inside + covers
                if best is None or score > best[0]:
                    best = (score, text)
            if best is not None:
                parts.append(best[1])
        # A banner describes the table, not this column. The unit declaration
        # "(All amounts are in INR Crores...)" spans every data column, so
        # overlap scoring hands it to all of them, and it then appears in each
        # column's heading and in the text offered to the period parser.
        if header_block is not None and header_block.banners:
            banners = set(header_block.banners)
            parts = [p for p in parts if p not in banners]
        # One level's fragment can win two levels of the same stack when the
        # header rows overlap, so the year appears twice: "30 June 2026 2026
        # (Unaudited)". Harmless to the parser, but this string is what a
        # reader is shown as the source heading, and a doubled year reads as a
        # transcription error in the document rather than in us.
        deduped: list[str] = []
        for part in parts:
            if not deduped or deduped[-1] != part:
                deduped.append(part)
        parts = deduped
        header = " ".join(parts).strip()

        # the span word may sit anywhere in this table's header block
        span_hint: str | None = None
        header_blob = " ".join(
            text for level in levels for _, text in level)
        if _SPAN_QUARTER.search(header):
            span_hint = "quarter"
        elif _SPAN_YEAR.search(header):
            span_hint = "year"
        elif (_SPAN_QUARTER.search(header_blob)
              and not _SPAN_YEAR.search(header_blob)
              and not _OTHER_SPAN.search(header_blob)):
            # The table's header block may be borrowed only when it names one
            # span and one alone.  A statement showing "Three Months Ended"
            # beside "Nine Months Ended" says nothing about a column that sits
            # under neither, and guessing between them would put a quarter's
            # figures under a nine-month heading.
            span_hint = "quarter"
        # A span word from the surrounding prose is deliberately *not* used.
        # A banner printed above the band is now recovered as a header level,
        # so page context adds nothing but error: it would stamp a quarter onto
        # a balance sheet whose columns are instants, which is exactly the kind
        # of confident wrong answer this system must not give.
        data = [cl for cl in cells if cl.col == c.index and cl.row >= n_header]
        numeric_ratio = (sum(1 for d in data if d.is_numeric) / len(data)
                         if data else 0.0)
        # Read the header *stack* first: a statement column head is spread over
        # several header rows ("Three Months Ended" / "June 28," / "2025"), and
        # flattening it to one string only works when the fragments happen to
        # land in a parseable order.  The flat parse stays as the fallback so
        # every form that already resolved keeps resolving.
        # The header block is tried first because it is the only source built
        # per column by geometry alone. `parts` is assembled by overlap scoring
        # and a wide head can win a band it merely touches; that is how three
        # columns' text became "30 June 31 March 30 June" and parsed as a date
        # in 2031, a year the document never mentions. The block's text for a
        # column contains only cells that sit in that column, with audit status
        # and note references removed.
        period = None
        #: The text the period was read from, which the two-date guard below
        #: must judge -- not some other assembly of the head.
        read_from = header
        col_header = header_block.for_column(c.index) if header_block else None
        if col_header is not None:
            block_text = col_header.qualified_period_text
            if block_text:
                period = parse_period(block_text, calendar=calendar,
                                      default_span=span_hint)
                if period is None:
                    period = parse_period_levels(
                        col_header.lines, calendar=calendar,
                        default_span=span_hint)
                if period is not None:
                    read_from = block_text
                    # The head path is what any later re-reading of this column
                    # starts from (a calendar established after the fact
                    # re-reads every column from it), so it must be the
                    # evidence the period came from. The overlap-built path of
                    # a steel maker's year column had lost "Financial year" to
                    # the column beside it, and re-read, the year became a
                    # balance.
                    parts = [*col_header.period_groups, *col_header.lines]
                    # And the head a reader is shown is the head it was read
                    # from, not the overlap assembly that put "Financial year"
                    # over the quarter beside it.
                    header = " ".join(
                        t for t in [*col_header.groups, *col_header.lines] if t)
        if period is None:
            period = parse_period_levels(parts, calendar=calendar,
                                         default_span=span_hint)
        if period is None and header:
            period = parse_period(header, calendar=calendar,
                                  default_span=span_hint)

        # A head naming two different dates cannot say which is this column's.
        # It happens when a head absorbs the sentence introducing the table: a
        # comparative column headed "Jun-25" was reconstructed as "comparable
        # basis, excluding PIDF incentive which was discontinued in Dec'25 end
        # Jun-25", and the parser took the first date it met and dated the
        # column six months wrong -- confidently, and with nothing to show a
        # reader that the head had said anything else.
        #
        # Refusing leaves the figures undated, which is visible, rather than
        # under the wrong quarter, which is not.
        # The guard reads the text the period came from. The overlap-built
        # head of a bank's standalone columns carried the page title's "JUNE
        # 30, 2026" beside each column's own date; the header block, which
        # sets titles aside, had dated all four correctly, and judging the
        # other text threw its answer away.
        if period is not None and names_more_than_one_date(read_from):
            log.info("tables.ambiguous_column_head",
                     column=c.index, header=header[:120])
            period = None
        # The group head counts as well: "Growth (%)" is often printed once
        # over the two comparison columns it governs.
        growth_text = " ".join([header or ""] + (col_header.groups if col_header else []))
        is_growth = bool(growth_text.strip() and GROWTH_HEADER_RE.search(growth_text))
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
            period_resolved=period,
            is_growth_column=is_growth, growth_type=gtype,
            numeric_ratio=round(numeric_ratio, 3)))
    return out
