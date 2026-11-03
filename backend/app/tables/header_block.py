"""The header block of a financial table, read as a structure rather than a string.

A statement's column heads are not one line of text. Groww's consolidated P&L
sets them over five:

    (All amounts are in INR Crores, unless otherwise stated)   <- banner
                Quarter ended                    Year ended    <- group
      30 June      31 March      30 June       |  31 March     <- day/month
         2026          2026         2025       |     2026      <- year
    (Unaudited)    (Audited)    (Audited)      |  (Audited)    <- audit status
                 (Refer Note 3)(Refer Note 4)                  <- note refs

Flattening that into one string per column is what produced the defect this
module exists to prevent: three columns' heads were concatenated into
"30 June 31 March 30 June" and parsed as a date in 2031, a year that appears
nowhere in the document.

Three distinctions carry the whole design, and each is geometric:

**A banner is not a column head.** The unit declaration above spans x 978-1379,
crossing every data column. Text that crosses a column boundary describes the
table, not a column, and must never become part of a column's head.

**A group head spans a set of columns and is placed by partition, not
containment.** "Quarter ended" occupies x 1009-1122, which lies wholly inside
column 2 -- yet it governs columns 1, 2 and 3. Containment cannot express that.
The heads of one level instead partition the columns into contiguous groups:
a column under a head's printed extent belongs to it, and the rest go where
the heads' positions fit best -- centred over their group, or starting at its
left edge, whichever the page shows (see `_assign_group_cells`).

**A column head is assembled down its own column.** "30 June" and "2026" are
separate OCR elements on separate lines; only reading down column 1 recovers
"30 June 2026".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.normalization.numbers import looks_numeric
from app.normalization.periods import looks_like_period_header

log = get_logger("tables.header_block")

#: A bare four-digit year, the second line of a split date head.
_BARE_YEAR = re.compile(r"(?:19|20)\d{2}")
#: Audit status, which financial statements print under the dates.
_AUDIT_STATUS = re.compile(
    r"\(?\s*(un\s*-?\s*audited|audited|reviewed|unreviewed|provisional)\s*\)?",
    re.I)
#: A pointer to a note elsewhere in the filing. Part of the head, but it says
#: nothing about the period and must not reach the period parser.
_NOTE_REF = re.compile(r"\(?\s*refer\s+note\s*\d*\s*\)?", re.I)
#: A complete date -- day and month and year, a month and year, or an all-digit
#: date. Used to tell a group head from a title.
_NAMES_A_DATE = re.compile(
    r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s*[-/ ]?\s*"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
    r"\s*,?\s*['’`]?\s*\d{2,4}\b"
    # No boundary before the month here: a title whose spaces OCR lost runs
    # the month into the word before it -- "QUARTERENDEDJUNE30,2026".
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
    r"\s*,?\s*\d{1,2}\s*,\s*\d{4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
    r"\s*[-/']\s*\d{2,4}\b"
    r"|\b\d{1,2}\s*[.,/-]\s*\d{1,2}\s*[.,/-]\s*\d{2,4}\b", re.I)

#: A reporting basis named in a head: the two group-level bases only.
_BASIS_WORD = re.compile(r"\b(standalone|stand-alone|consolidated)\b", re.I)
#: A head that names a KIND of period and never which one -- "Quarter
#: ended", "Nine months ended", "Year ended" -- or only a basis. Such a head
#: governs every column of its kind; it is a group head by what it says.
_KIND_HEAD = re.compile(
    r"^\s*(?:(?:quarter|three\s*months?|six\s*months?|nine\s*months?"
    r"|half[\s-]*year|year|twelve\s*months?|period)\s*(?:ended|ending)\s*:?"
    r"|standalone|stand-alone|consolidated)\s*$", re.I)
#: Words by which a group head says something about a period: a span, a
#: point in time, a fiscal label, a month or a year.
_PERIOD_WORD = re.compile(
    r"quarter|months?|year|ended|ending|half|nine|\bsix\b|three|twelve|period"
    r"|\bfy|\bytd\b|\bq[1-4]\b|\bh[12]\b|\bas\s*(?:at|on|of)\b"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|(?<!\d)(?:19|20)\d{2}(?!\d)", re.I)


def names_more_than_one_date(text: str) -> bool:
    """Whether this head text names two or more different dates.

    A column has one reporting period, so a head naming two cannot say which
    is the column's. That happens when a head absorbs the sentence above the
    table: one deck's comparative column, headed "Jun-25", was reconstructed as
    "comparable basis, excluding PIDF incentive which was discontinued in
    Dec'25 end Jun-25". The parser took the first date it found and dated the
    column six months wrong -- at high confidence, with no flag, because
    nothing downstream knew the head had said anything else.

    Refusing is the right answer. An undated figure is visibly incomplete; a
    figure under the wrong quarter is not.
    """
    found = {m.group(0).strip().lower()
             for m in _NAMES_A_DATE.finditer(text or "")}
    # Same date written twice -- "30 June 2026" beside "Jun-26" -- is one date.
    # Compare the months and years rather than the spellings.
    months = {(_month_of(t), _year_of(t)) for t in found}
    months.discard((None, None))
    return len(months) > 1


_MONTH_TOKEN = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
_YEAR_TOKEN = re.compile(r"(?:19|20)?(\d{2})\b")


def _month_of(text: str) -> str | None:
    m = _MONTH_TOKEN.search(text)
    if m:
        return m.group(1).lower()
    # An all-digit date: the middle group is the month.
    digits = re.match(r"^\s*(\d{1,2})\s*[.,/-]\s*(\d{1,2})\s*[.,/-]", text)
    return digits.group(2) if digits else None


def _year_of(text: str) -> str | None:
    years = _YEAR_TOKEN.findall(text)
    return years[-1] if years else None


#: The label of the label column itself.
_LABEL_COLUMN_WORD = re.compile(
    r"^\s*\(?\s*(particulars?|description|line\s*items?|sr\.?\s*no\.?|"
    r"s\.?\s*no\.?|items?)\s*\)?\s*$", re.I)

#: How far past the first header row to keep looking. Groww needs six; the
#: limit exists only so a table with no header does not scan its whole body.
MAX_HEADER_ROWS = 8


@dataclass(frozen=True, slots=True)
class HeaderCell:
    """One piece of head text, with the geometry that placed it."""
    text: str
    x0: float
    x1: float
    row: int

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class ColumnHeader:
    """Everything the header block says about one data column."""
    index: int
    #: Head lines belonging to this column alone, top to bottom.
    lines: list[str] = field(default_factory=list)
    #: Group heads above it, outermost first -- e.g. ["Quarter ended"].
    groups: list[str] = field(default_factory=list)
    audit_status: str | None = None
    note_ref: str | None = None

    @property
    def basis(self) -> str | None:
        """The reporting basis a group head above this column declares.

        A bank prints its standalone and consolidated results side by side in
        one table, under two group heads, with identical dates beneath each.
        The head is the only thing that says which basis a column is on."""
        for g in self.groups:
            m = _BASIS_WORD.search(g)
            if m:
                return m.group(1).lower()
        return None

    @property
    def period_groups(self) -> list[str]:
        """Group heads that say something about the period.

        Only those. A table band that starts at the top of the page takes in
        the masthead -- "STATE BANK OF INDIA", "CORPORATE CENTRE, MUMBAI
        400021" -- and each line became a group head over every column, its
        words prepended to each column's dates for the period parser to read.
        A head naming only a basis, a company or an address says nothing
        about when."""
        return [g for g in self.groups
                if _BASIS_WORD.sub("", g).strip(" :-/()") != ""
                and _PERIOD_WORD.search(g)]

    @property
    def period_text(self) -> str:
        """The head text that may name a period, and nothing else.

        Audit status and note references are stripped: "(Audited)" and
        "(Refer Note 3)" are head content but say nothing about *when*, and
        feeding them to a date parser only invites a wrong answer.
        """
        parts = [t for t in self.lines
                 if not _AUDIT_STATUS.fullmatch(t.strip())
                 and not _NOTE_REF.fullmatch(t.strip())]
        return " ".join(p for p in parts if p).strip()

    @property
    def qualified_period_text(self) -> str:
        """The period text with its group heads, which carry the span.

        "30 June 2026" alone does not say whether it is a quarter or a year;
        "Quarter ended" above it does. The group is prepended rather than
        merged so the span word governs the date it actually sits over.
        """
        base = self.period_text
        groups = " ".join(self.period_groups)
        if not base:
            return groups.strip()
        return f"{groups} {base}".strip()

    def as_dict(self) -> dict:
        return {"index": self.index, "lines": list(self.lines),
                "groups": list(self.groups), "audit_status": self.audit_status,
                "note_ref": self.note_ref, "basis": self.basis}


@dataclass
class HeaderBlock:
    """The reconstructed head of one table."""
    n_rows: int
    columns: dict[int, ColumnHeader] = field(default_factory=dict)
    #: Text spanning more than one data column: unit declarations, titles.
    banners: list[str] = field(default_factory=list)
    #: Why a row was classified as it was, for diagnostics.
    row_kinds: list[str] = field(default_factory=list)

    def for_column(self, index: int) -> ColumnHeader:
        return self.columns.get(index) or ColumnHeader(index=index)

    def as_dict(self) -> dict:
        return {"n_rows": self.n_rows, "banners": list(self.banners),
                "row_kinds": list(self.row_kinds),
                "columns": {str(k): v.as_dict()
                            for k, v in sorted(self.columns.items())}}


#: A date written entirely in digits: "30.06.2026", "31/12/25", "31,03.2026"
#: -- the last being what OCR makes of a dotted date when it reads the first
#: separator as a comma. Common as a column head in Indian and UK filings.
_NUMERIC_DATE = re.compile(r"^\d{1,2}\s*[.,/-]\s*\d{1,2}\s*[.,/-]\s*\d{2,4}$")
#: "(773)": a negative figure, which no head prints.
_BRACKETED_FIGURE = re.compile(r"^\(\s*[\d\s]+\)$")
#: "43 109", "1 234 567": thousands grouped by spaces.
_SPACE_GROUPED_FIGURE = re.compile(r"^-?\d{1,3}(?:[\xa0 ]\d{3})+$")
#: A head fragment made only of a period token: "Q1-Q2", "Q2", "H1", "9M".
#: A split head ("Q1-Q2" / "2026" as two words) is still a head.
_PERIOD_TOKEN_ONLY = re.compile(r"(?:Q[1-4](?:\s*[-–—]\s*Q[1-4])?|[1-4]Q|H[12]|9M)", re.I)
#: A stub that declares the table's unit -- "€ millions, unless otherwise
#: stated", "(₹ in crore)" -- names no line: its row is the head's last row.
_UNIT_DECLARATION = re.compile(
    r"\b(?:millions?|billions?|crores?|lakhs?|thousands?|mn|bn)\b|unless\s+otherwise|"
    r"except\s+(?:per|share)", re.I)


def looks_like_a_data_row(texts: list[str], label: str) -> bool:
    """Whether this row is the table's first real data row.

    The header block ends here, so being wrong is expensive in one direction:
    stopping a row early leaves the column dates typed as data, and every
    figure in the table then loses its period.

    What separates a data row from the last line of a head is not the presence
    of numbers -- "2026 2026 2025 2026" is a header row of nothing but numbers
    -- but the *kind* of number. Three shapes are date fragments rather than
    measurements: a bare year, an all-digit date, and anything sitting beside
    the word that names the label column.
    """
    values = [t for t in texts if looks_numeric(t)]
    if not values:
        return False
    # "on 30.06.2026" -- a steel maker's date line -- is a date with a word
    # in front of it, and a period head however it is worded.
    undated = [t for t in values
               if not _BARE_YEAR.fullmatch(t.strip())
               and not _NUMERIC_DATE.match(t.strip())
               and not _PERIOD_TOKEN_ONLY.fullmatch(t.strip())
               and not looks_like_period_header(t)]
    # A measurement: a figure with a separator, a bracketed negative, or one
    # grouped by spaces. A Swiss statement prints "43 109" and "(773)" and
    # never a comma or a stop; read as more head, its first six lines were
    # swallowed into the column heads and lost (Phase 22).
    measured = [t for t in undated
                if any(ch in t for ch in ".,")
                or _BRACKETED_FIGURE.match(t.strip())
                or _SPACE_GROUPED_FIGURE.match(t.strip())]
    # Plain figures beside a named line are data too: "Other operating income
    # 292 183". A row whose label column holds no words -- "1 2 3 4 5", the
    # column serial numbers under a results head -- stays head.
    if (not measured and sum(ch.isalpha() for ch in label) >= 3
            and not _UNIT_DECLARATION.search(label)):
        measured = undated
    if not measured:
        return False
    # "Particulars" names the label column, so its row is part of the head
    # however many numbers share it. Checked against every non-numeric token in
    # the row rather than against the leftmost column: a statement that numbers
    # its lines puts a serial column first, and "Particulars" then sits in the
    # second column, where a positional test cannot see it. That is exactly
    # what happened -- the date row of a bank's results was read as data, and
    # all four of its columns fell back to the period in the document title.
    words = [t for t in texts if not looks_numeric(t)]
    if label.strip():
        words.append(label)
    return not any(_LABEL_COLUMN_WORD.match(w.strip()) for w in words)


#: The share of a head's width that must lie over a column for the head to
#: be over it. A column boundary set from the figures beneath can fall a few
#: pixels inside a head's first letter -- 2.5px of "Financial year" over the
#: column to its left -- and a steel maker's year column then lost its span:
#: the head counted as spanning two columns, became a group, and the year's
#: figures were read as balances.
_MIN_OVERLAP_SHARE = 0.15
#: ...but never more than about half a line of text at the 200dpi render: a
#: wide banner ("All amounts are in INR Crores ...") may reach only 20px into
#: its first column and still be over it.
_MIN_OVERLAP_CAP_PX = 12.0


def _over(x0: float, x1: float, cx0: float, cx1: float) -> bool:
    """Whether text at x0..x1 stands over the column cx0..cx1."""
    need = max(1.0, min(_MIN_OVERLAP_SHARE * (x1 - x0), _MIN_OVERLAP_CAP_PX))
    return min(x1, cx1) - max(x0, cx0) > need


def _spans_multiple_columns(x0: float, x1: float,
                            data_cols: list[tuple[int, float, float]]) -> int:
    """How many data columns this piece of text stands over."""
    return sum(1 for _, cx0, cx1 in data_cols if _over(x0, x1, cx0, cx1))


def _assign_group_cells(group_cells: list[HeaderCell],
                        data_cols: list[tuple[int, float, float]],
                        ) -> dict[int, HeaderCell]:
    """Place one level's group heads over the columns they govern.

    Returns the head itself rather than its text, so that two heads with the
    same words ("Quarter ended" under Standalone and again under Consolidated)
    stay two heads."""
    if not group_cells or not data_cols:
        return {}
    heads = sorted(group_cells, key=lambda c: c.x0)
    cols = sorted(data_cols, key=lambda c: c[1])
    if len(heads) == 1:
        return {index: heads[0] for index, _, _ in cols}

    # A column lying under exactly one head's printed extent belongs to that
    # head: overlap is direct evidence and binds whatever is decided below.
    forced: dict[int, int] = {}
    for pos, (_, cx0, cx1) in enumerate(cols):
        over = [h for h, c in enumerate(heads)
                if _over(c.x0, c.x1, cx0, cx1)]
        if len(over) == 1:
            forced[pos] = over[0]

    # The columns no head overlaps are decided by how the heads are set.
    # Heads partition the columns into contiguous groups, and a statement sets
    # them one of two ways: CENTRED over their group (a merged cell -- the
    # usual way) or starting at its left edge. Neither fixed rule reads both.
    # A bank printing "Standalone" and "Consolidated" centred over four
    # columns each had its fifth column -- the first consolidated one -- given
    # to "Standalone" by the left-edge rule; and "Quarter ended" centred over
    # three columns beside "Year ended" over one is misread by any
    # nearest-head rule, because the narrow group's head sits nearer the third
    # quarter column than the wide group's does. So the best partition is
    # found under each model and the model that fits the printed heads better
    # is believed.
    best: tuple[float, list[tuple[int, int]]] | None = None
    for model in ("centre", "left"):
        found = _best_partition(heads, cols, forced, model)
        if found is not None and (best is None or found[0] < best[0]):
            best = found
    if best is None:
        # No contiguous partition honours the overlap evidence; fall back to
        # overlap first, then each head's left edge.
        out: dict[int, HeaderCell] = {}
        for pos, (index, cx0, cx1) in enumerate(cols):
            if pos in forced:
                out[index] = heads[forced[pos]]
                continue
            centre = (cx0 + cx1) / 2.0
            chosen = None
            for cell in heads:
                if cell.x0 <= centre:
                    chosen = cell
                else:
                    break
            out[index] = chosen or heads[0]
        return out
    out = {}
    for h, (a, b) in enumerate(best[1]):
        for pos in range(a, b):
            out[cols[pos][0]] = heads[h]
    return out


def _best_partition(heads: list[HeaderCell], cols: list[tuple[int, float, float]],
                    forced: dict[int, int], model: str,
                    ) -> tuple[float, list[tuple[int, int]]] | None:
    """The contiguous split of ``cols`` among ``heads`` (in order, each head
    taking at least one column) that best fits the heads' positions under the
    given alignment model, honouring ``forced``. Returns (misfit in pixels,
    [(first, end) per head]) or None when no split honours the evidence."""
    n, k = len(cols), len(heads)
    if k > n:
        return None

    def cost(h: int, a: int, b: int) -> float:
        g0, g1 = cols[a][1], cols[b - 1][2]
        head = heads[h]
        if model == "centre":
            return abs(head.cx - (g0 + g1) / 2.0)
        return abs(head.x0 - g0)

    inf = float("inf")
    # f[j][i]: least misfit placing the first j heads over the first i columns.
    f = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[-1] * (n + 1) for _ in range(k + 1)]
    f[0][0] = 0.0
    for j in range(1, k + 1):
        for i in range(j, n - (k - j) + 1):
            for a in range(j - 1, i):
                if f[j - 1][a] == inf:
                    continue
                if any(forced.get(p, j - 1) != j - 1 for p in range(a, i)):
                    continue
                c = f[j - 1][a] + cost(j - 1, a, i)
                if c < f[j][i]:
                    f[j][i], back[j][i] = c, a
    if f[k][n] == inf:
        return None
    spans: list[tuple[int, int]] = []
    i = n
    for j in range(k, 0, -1):
        a = back[j][i]
        spans.append((a, i))
        i = a
    spans.reverse()
    return f[k][n], spans


def _nests_within(lower: dict[int, HeaderCell], upper: dict[int, HeaderCell]) -> bool:
    """Whether every head of the lower level governs columns that all sit under
    one head of the upper level. A real second level of a header subdivides
    the first; a stray wide cell straddles its boundaries."""
    for head in set(lower.values()):
        columns = [i for i, h in lower.items() if h is head]
        parents = {id(upper.get(i)) for i in columns}
        if len(parents) > 1:
            return False
    return True


class _Level:
    """One printed line of a header band, standing in for the band."""
    __slots__ = ("band", "elements")

    def __init__(self, band, elements):
        self.band = band
        self.elements = elements


def _printed_lines(elements) -> list[list]:
    """Split a band's elements into the printed lines they sit on, top first.

    Elements are grouped by vertical centre; a new line starts where the gap
    to the current line exceeds half the typical element height. A label that
    spans two lines ("Sr" over "No") joins whichever line its centre is on.
    """
    els = [e for e in elements if (e.text or "").strip()]
    if len(els) < 2:
        return [list(elements)]
    heights = sorted(e.bbox.height for e in els)
    tol = max(2.0, 0.5 * heights[len(heights) // 2])
    ordered = sorted(els, key=lambda e: e.bbox.y + e.bbox.height / 2)
    lines: list[list] = []
    centre = None
    for e in ordered:
        c = e.bbox.y + e.bbox.height / 2
        if centre is None or abs(c - centre) > tol:
            lines.append([e])
            centre = c
        else:
            lines[-1].append(e)
            centre = (centre * (len(lines[-1]) - 1) + c) / len(lines[-1])
    for line in lines:
        line.sort(key=lambda e: e.bbox.x)
    return lines


def build_header_block(rows, cols, *, table_id: str = "",
                       label_index: int = 0) -> HeaderBlock:
    """Read the header block of a table into per-column structure.

    ``rows`` are RowBands and ``cols`` ColumnBands from `structure.py`; both are
    duck-typed here to keep this module free of a circular import.
    """
    data_cols = [(c.index, c.x0, c.x1) for c in cols if c.index != label_index]
    if not data_cols:
        return HeaderBlock(n_rows=0)

    block = HeaderBlock(n_rows=0)
    group_cells: list[HeaderCell] = []
    per_column: dict[int, list[str]] = {i: [] for i, _, _ in data_cols}

    label_x1 = next((c.x1 for c in cols if c.index == label_index), data_cols[0][1])
    # The label column's left edge matters only when it is not the first
    # column: with figures to its left, "left of the label column's right
    # edge" is not the label column.
    label_x0 = (next((c.x0 for c in cols if c.index == label_index), float("-inf"))
                if label_index else float("-inf"))

    def in_label(el) -> bool:
        return el.bbox.x1 <= label_x1 + 2.0 and el.bbox.x >= label_x0 - 2.0

    # A header row band can hold several printed lines. Where the lines of a
    # head sit close together -- and a label cell such as "Sr / No" spans two
    # of them -- row banding merges the span words, the dates and the audit
    # status into one band. Read as one row, the wide "Nine months ended" made
    # it a group row, every date on it was then taken for a title, and no
    # column kept its date: the nine months to 31-Dec-24 came out as a balance
    # "as at" that day. Each printed line is therefore read as its own level,
    # in order; a band holding one line reads exactly as before.
    levels_in = []
    for row_index, row in enumerate(rows[:MAX_HEADER_ROWS]):
        for line in _printed_lines(row.elements):
            levels_in.append((row_index, row, line))

    header_rows_seen: set[int] = set()
    for ri, (row_index, band, elements) in enumerate(levels_in):
        row = _Level(band, elements)
        texts = [e.text for e in row.elements]
        if not texts:
            block.row_kinds.append("empty")
            break
        label = " ".join(e.text for e in row.elements
                         if in_label(e))
        if looks_like_a_data_row(texts, label):
            break

        header_rows_seen.add(row_index)
        block.n_rows = len(header_rows_seen)
        band.is_header = True
        kind = "column"
        # A row is a *group* row if any of its cells spans more than one data
        # column. Every cell on such a row is then a group head, however narrow.
        # Groww's "Year ended" sits over a single column while "Quarter ended"
        # beside it covers three; judging each cell by its own width alone made
        # "Year ended" a column line and left the year column grouped under
        # "Quarter ended", which is how an annual figure came to be typed as a
        # quarter. Rows are the unit here because a header level is a row.
        row_is_group = any(
            _spans_multiple_columns(e.bbox.x, e.bbox.x1, data_cols) > 1
            and not in_label(e)
            and not _AUDIT_STATUS.fullmatch((e.text or "").strip())
            and not _NOTE_REF.fullmatch((e.text or "").strip())
            for e in row.elements if (e.text or "").strip())
        # A line of heads that each name a kind of period, fewer than the
        # columns, is a group row even when every head is narrow enough to sit
        # over one column: "Quarter ended" centred over three columns overlaps
        # only the middle one, and read as that column's own head it left the
        # other two with bare dates -- beside a "Year ended", read as
        # balances.
        #
        # A statement that prints "Quarter ended" over EACH quarter column is
        # doing something else: those are the columns' own heads, and read as
        # groups the year column beside them lost its span and became a
        # balance. A group head appears once per group, so the kinds on such
        # a line differ -- or there are at most half as many heads as columns
        # (a bank's "Quarter ended / Year ended" printed once under each of
        # two bases, over eight columns).
        heads_here = [e for e in row.elements
                      if (e.text or "").strip() and not in_label(e)]
        kinds = [re.sub(r"\W+", "", e.text.lower()) for e in heads_here]
        if (not row_is_group and heads_here
                and all(_KIND_HEAD.match(e.text) for e in heads_here)
                and len(heads_here) < len(data_cols)
                and (len(set(kinds)) == len(kinds)
                     or 2 * len(heads_here) <= len(data_cols))):
            row_is_group = True
        for el in row.elements:
            text = (el.text or "").strip()
            if not text:
                continue
            x0, x1 = el.bbox.x, el.bbox.x1
            # Anything living in the label column is the label column's own
            # heading, not a data column's.
            if in_label(el):
                continue
            # Audit status and note references belong to the column they sit
            # over however wide they are. "(Refer Note 3)" is set in small type
            # and drifts across a column boundary, and treating it as a group
            # head let it displace the real one -- three columns ended up
            # grouped under "(Refer Note 4)" instead of "Quarter ended".
            is_metadata = bool(_AUDIT_STATUS.fullmatch(text)
                               or _NOTE_REF.fullmatch(text))
            spanned = _spans_multiple_columns(x0, x1, data_cols)
            if not is_metadata and spanned >= len(data_cols) and len(data_cols) > 1:
                # Crosses every data column: a banner about the whole table.
                block.banners.append(text)
                kind = "banner"
                continue
            if not is_metadata and (spanned > 1 or row_is_group):
                # A group head names the *kind* of period its columns are --
                # "Quarter ended", "Year ended" -- and never which one, because
                # the columns beneath it differ. Text spanning several columns
                # that names a specific date is therefore not a group head but
                # a title, and belongs to the table rather than to any column.
                #
                # One bank's statement is headed "UNAUDITED STANDALONE
                # FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026" across
                # the whole table. Read as a group, it was prepended to every
                # column and its date won over each column's own: four columns
                # covering three different quarters and a full year all
                # resolved to the first quarter. The span word in it still
                # reaches the span hint, which is the part that was useful.
                if _NAMES_A_DATE.search(text):
                    block.banners.append(text)
                    kind = "banner"
                    continue
                group_cells.append(HeaderCell(text, x0, x1, ri))
                kind = "group"
                continue
            # Belongs to exactly one column.
            # Widest overlap wins: a small-type note reference straddling a
            # boundary belongs to the column it mostly sits over.
            target, best = None, 0.0
            for index, cx0, cx1 in data_cols:
                overlap = min(x1, cx1) - max(x0, cx0)
                if overlap > best:
                    target, best = index, overlap
            if target is None or best <= 1.0:
                continue
            per_column[target].append(text)
        block.row_kinds.append(kind)

    # A head can have more than one level of groups. One bank prints its
    # standalone and consolidated results side by side: "Standalone" and
    # "Consolidated" across the top, "Quarter ended" and "Year ended" beneath
    # each, and the dates below that. Keeping only the topmost level kept the
    # basis and threw away the span -- every "31.03.2026" column under
    # "Consolidated" then read as the fiscal year, and the quarter to that date
    # became an annual figure.
    #
    # A lower level is kept only if it NESTS inside the level above: each of
    # its heads governs columns that all sit under one upper head. A stray
    # wide cell straddles an upper boundary and is dropped, as before.
    levels: list[dict[int, HeaderCell]] = []
    for level_row in sorted({c.row for c in group_cells}):
        assignment = _assign_group_cells(
            [c for c in group_cells if c.row == level_row], data_cols)
        if not levels or _nests_within(assignment, levels[-1]):
            levels.append(assignment)
    groups = {index: [lvl[index].text for lvl in levels if index in lvl]
              for index, _, _ in data_cols}
    for index, _, _ in data_cols:
        lines = per_column[index]
        audit = next((_AUDIT_STATUS.search(t).group(1).lower().replace(" ", "")
                      for t in lines if _AUDIT_STATUS.fullmatch(t.strip())), None)
        note = next((t.strip("()") for t in lines
                     if _NOTE_REF.fullmatch(t.strip())), None)
        header = ColumnHeader(index=index, lines=lines,
                              groups=groups.get(index, []),
                              audit_status=audit, note_ref=note)
        block.columns[index] = header

    if table_id:
        log.debug("tables.header_block", table_id=table_id,
                  rows=block.n_rows, banners=len(block.banners),
                  groups=len({g for gs in groups.values() for g in gs}),
                  levels=len(levels))
    return block
