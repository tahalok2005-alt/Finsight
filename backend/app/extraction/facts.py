"""Financial fact extraction from reconstructed tables.

Each data cell that sits at the intersection of a recognised metric row and a
period column becomes one fact, carrying:

* the value as printed, the unit and currency in force, and the normalised
  base-unit value;
* the period the column refers to;
* growth against a comparison column when the table supplies one, flagged as
  *reported* (from a "% chg" column) versus *derived* (computed here);
* full provenance down to the cell's bounding box.

No value is ever invented.  A cell that cannot be parsed, or a row whose label
is not in the taxonomy, produces no fact rather than a guessed one.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.config.settings import settings
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import confidence as conf_mod
from app.extraction import safety_gate
from app.extraction.taxonomy import MetricMatch, match_metric, normalize_label
from app.extraction.taxonomy import get as get_metric
from app.extraction.qualification import from_column_head
from app.models.enums import (
    CellRole, Currency, DerivationStatus, EntityType, FactKind, GrowthType,
    MagnitudeUnit, PeriodBasis, PeriodType, StatementNature, ValueType,
)
from app.models.schemas import (
    BBoxModel, ExtractedTable, FinancialFact, NormalizedPeriod, Provenance,
    TableCell, TableColumn,
)
from app.normalization.evidence import (
    Claim, Level, RATE_KINDS, resolve_value_type,
)
from app.normalization.numbers import (
    MAGNITUDE_FACTORS, RATE_CEILING, describe_number, detect_value_type,
    holds_more_than_one_number, normalise_separators, normalize_number,
    repair_by_decimals, table_decimals, table_grouping,
    parse_number, states_rate, states_sub_unit,
)
from app.normalization.periods import (
    FiscalCalendar, is_quarter_over_quarter, is_year_over_year,
    looks_like_period_header, parse_period, period_distance,
)
from app.tables.shape import classify_table, period_is_mandatory
from app.tables.validate import validate_table

log = get_logger("extraction.facts")

_BPS_CELL_RE = re.compile(r"bps", re.I)
_UOM_HEADER_RE = re.compile(r"^\s*(uom|unit|units|u\.o\.m\.?)\s*$", re.I)


@dataclass(slots=True)
class TableFactContext:
    """Everything a table needs from its document to interpret its cells."""
    document_id: str
    entity: str | None
    calendar: FiscalCalendar
    default_currency: Currency | None
    default_unit: MagnitudeUnit | None
    page_image_url: str | None = None
    entity_id: str | None = None
    entity_type: EntityType = EntityType.UNKNOWN
    parent_entity_id: str | None = None
    #: The document's registered reporting bases, by basis word: "standalone"
    #: and "consolidated" -> (name, entity id, type, parent id). A table whose
    #: column group heads declare a basis attributes each column's figures to
    #: that basis rather than to the section the table sits in.
    basis_entities: dict = field(default_factory=dict)


def table_decimals_of(table) -> str | None:
    """The table's decimal convention, computed once and kept on the table."""
    meta = getattr(table, "meta", None)
    if meta is None:
        return table_decimals(c.text for c in table.cells if c.is_numeric)
    if "decimals" not in meta:
        meta["decimals"] = table_decimals(c.text for c in table.cells
                                          if c.is_numeric)
    return meta["decimals"]


def table_grouping_of(table) -> str | None:
    """The table's thousands convention, computed once and kept on the table."""
    meta = getattr(table, "meta", None)
    if meta is None:
        return table_grouping(c.text for c in table.cells if c.is_numeric)
    if "grouping" not in meta:
        meta["grouping"] = table_grouping(c.text for c in table.cells
                                          if c.is_numeric)
    return meta["grouping"]


def _cell_at(table: ExtractedTable, row: int, col: int) -> TableCell | None:
    for c in table.cells:
        if c.row == row and c.col == col:
            return c
    return None


def _row_label(table: ExtractedTable, row: int, label_cols: list[int],
               uom_cols: set[int] | None = None) -> tuple[str, TableCell | None]:
    """Join the label columns of a row into the metric label.

    A unit-of-measure column ("UoM: Million") is excluded: it describes the
    value, not the metric, and folding it in turns "Stores" into
    "Stores Number", which no longer matches the taxonomy.
    """
    uom_cols = uom_cols or set()
    parts: list[str] = []
    anchor: TableCell | None = None
    for col in label_cols:
        if col in uom_cols:
            continue
        c = _cell_at(table, row, col)
        if c and c.text.strip() and not c.is_numeric:
            parts.append(c.text.strip())
            if anchor is None:
                anchor = c
    return " ".join(parts).strip(), anchor


def _label_columns(table: ExtractedTable, n_header: int) -> list[int]:
    """Columns that hold row labels rather than data.

    A leading serial-number column and a units-of-measure column are labels for
    this purpose even though the serial column is numeric. A label column the
    reconstruction found in the middle of the table, with figures on both
    sides, is taken as found.
    """
    middle = (getattr(table, "meta", None) or {}).get("label_column")
    if middle:
        return [int(middle)]
    def _ratio(index: int) -> float:
        data = [c for c in table.cells if c.col == index and c.row >= n_header]
        if not data:
            return 0.0
        return sum(1 for c in data if c.is_numeric) / len(data)

    def _cells(index: int) -> list:
        return [c for c in table.cells if c.col == index and c.row >= n_header]

    def _small_integers(data) -> bool:
        values = [c.numeric_value for c in data if c.is_numeric]
        return bool(values) and all(
            v is not None and float(v).is_integer() and 0 <= v < 200 for v in values)

    def _text_follows(position: int) -> bool:
        """Whether a column of words comes to the right of ``position`` before
        any column of figures or any dated column."""
        for later in table.columns[position + 1:]:
            data = _cells(later.index)
            if not data:
                continue
            if later.normalized_period:
                return False
            if _ratio(later.index) < 0.4:
                return True
            if not _small_integers(data):
                return False
        return False

    out: list[int] = []
    seen_data = False
    for position, col in enumerate(table.columns):
        data = _cells(col.index)
        if not data:
            continue
        first_with_data = not seen_data
        seen_data = True
        numeric_ratio = sum(1 for c in data if c.is_numeric) / len(data)
        # A scanned statement can split its serial numbers over two columns --
        # "1" a few pixels left of "2" -- so neither is followed directly by
        # the words. Any column of small whole numbers, undated, with the
        # words to its right before any figure, is part of the serial column.
        # Without this a bank's scanned results took an empty margin column
        # for its labels and produced no facts from a page of 153.
        if (not col.normalized_period and _small_integers(data)
                and _text_follows(position)):
            out.append(col.index)
            continue
        # A serial column is recognised by what it holds and by what follows
        # it, not by how full it is. Requiring most of its rows to be numbered
        # missed a bank's results, where only the top-level lines carry a
        # number and the sub-items ("a) Interest / discount on advances") do
        # not: the ratio came to 0.46, too high to read as a label column and
        # too low to pass a 0.7 threshold, so the loop stopped at column 0 and
        # every real row label -- which sits in column 1 -- went unread. The
        # page produced eight facts out of a hundred and forty.
        #
        # The structural signature is a first column of small integers with a
        # text column beside it. Both halves are needed: the size test alone
        # would swallow a leading column of genuine small figures.
        #
        # "First" means the first column that holds anything. A reconstruction
        # can open with a column that holds nothing at all -- a ruling line at
        # the page margin becomes a boundary -- and a serial test pinned to
        # index 0 then missed the serial column at index 1, stopped there, and
        # fell back to the empty column as the label column: a clean,
        # correctly-dated bank statement produced no facts at all.
        is_serial = (
            first_with_data and numeric_ratio > 0.0
            and all((c.numeric_value or 0) < 200
                    for c in data if c.is_numeric)
            and _ratio(col.index + 1) < 0.4)
        is_uom = bool(_UOM_HEADER_RE.match(col.header or ""))
        if numeric_ratio < 0.4 or is_serial or is_uom:
            out.append(col.index)
        else:
            break
    return out or [0]


def _header_row_count(table: ExtractedTable) -> int:
    """Number of leading header rows; 0 for a headerless continuation fragment."""
    rows = {c.row for c in table.cells
            if c.role in (CellRole.COLUMN_HEADER, CellRole.CORNER)}
    return (max(rows) + 1) if rows else 0


def _period_for(col: TableColumn, calendar: FiscalCalendar) -> NormalizedPeriod | None:
    """The period a column denotes.

    The resolved period object is authoritative and is used as-is.  It carries
    the dates the *source* supports, which re-parsing cannot reproduce: a
    52/53-week filer's "Q3 FY25" ended 28 June 2025, and rebuilding that period
    from its own label yields 30 June -- a date the document never printed.

    Re-parsing remains the fallback for a column whose period was decided by
    the document-level repair pass, which works in labels; there the label is
    the best evidence there is.  A bare header is never re-read here, because
    the repair pass already decided what it meant.
    """
    if col.period_resolved is not None:
        return col.period_resolved
    if not col.normalized_period:
        return None
    return (parse_period(col.normalized_period, calendar=calendar)
            or parse_period(col.header, calendar=calendar))


#: A column head that announces references into the notes, not figures.
_REFERENCE_HEAD = re.compile(
    r"^\s*(?:notes?|note\s*(?:no\.?|ref(?:erence)?)|sch(?:edule)?s?\.?(?:\s*no\.?)?|ref\.?)(?:\s|$)",
    re.I)
#: What a note reference looks like: "3", "12", "3.2", "5a".
_REFERENCE_CELL = re.compile(
    r"^\s*\d{1,2}(?:[.\-]\d{1,2})?[a-z]?(?:\s*[,&]\s*\d{1,2}(?:[.\-]\d{1,2})?[a-z]?)*\s*$", re.I)


def _reference_columns(table: ExtractedTable, n_header: int) -> set[int]:
    """Columns of note numbers: a head "Notes" (or "Note No.", "Schedule",
    "Ref.") over cells that are all references. An IFRS statement puts one
    between its labels and its figures; its "2" beside "Assets held for sale"
    and "5" beside "Other operating income" were read as figures (Phase 22)."""
    out = set()
    for col in table.columns:
        if not _REFERENCE_HEAD.match(col.header or ""):
            continue
        cells = [c.text for c in table.cells
                 if c.col == col.index and c.row >= n_header and (c.text or "").strip()]
        if cells and all(_REFERENCE_CELL.match(t) for t in cells):
            out.add(col.index)
    return out


def _uom_columns(table: ExtractedTable, label_cols: list[int]) -> set[int]:
    return {c.index for c in table.columns
            if c.index in label_cols and _UOM_HEADER_RE.match(c.header or "")}


def _uom_text(table: ExtractedTable, row: int, uom_cols: set[int]) -> str:
    for col in sorted(uom_cols):
        c = _cell_at(table, row, col)
        if c and c.text.strip():
            return c.text
    return ""


#: Column heads that say a statement reports balances, and ones that say it
#: reports flows over a span.
_STOCK_HEAD = re.compile(r"\bas\s*(?:at|on|of)\b", re.I)
_FLOW_HEAD = re.compile(r"ended|ending|\bmonths?\b|year\s*to\s*date|\bytd\b", re.I)


def _statement_kind(period_cols) -> bool | None:
    """True when every dated column is headed as a balance ("As at ..."),
    False when every one is headed as a span ("Quarter ended ..."), None
    otherwise. A bare "Q1 FY27" or a bare date says neither: a deck heads its
    balance-sheet lines with quarters, and a statement's bare date can be the
    end of a span. Only an explicit head decides."""
    kinds = set()
    for col, period in period_cols:
        head = f"{col.header or ''} {getattr(period, 'raw_text', '') or ''}"
        if period.period_type == PeriodType.POINT_IN_TIME and _STOCK_HEAD.search(head):
            kinds.add(True)
        elif period.period_type != PeriodType.POINT_IN_TIME and _FLOW_HEAD.search(head):
            kinds.add(False)
        else:
            return None
    return kinds.pop() if len(kinds) == 1 else None


#: A row printed as a component of the row above it: "(d) Others",
#: "(d)others", "Others (d)" once OCR moved the letter, "- Trade payables",
#: "of which provisions for NPAs".
_ENUMERATED_CHILD = re.compile(
    r"^\s*(?:\(\s*(?:[a-z]|[ivx]{1,4})\s*\)|(?:[a-z]|[ivx]{1,4})\)|[-–—•]|of\s*which\b)"
    # a letter moved to the end -- lower case: "(A)" closing a cash-flow
    # section is that section's total -- and not the last term of a formula,
    # "Interest earned (a)+(b)+(c)+(d)", which is the total itself
    r"|(?<![+\-–)])\s*(?-i:\(\s*[a-z]\s*\))\s*$", re.I)
#: A label that declares its own components: "(a+b+c)", "(a)+(b)+(c)".
_SUM_OF_PARTS = re.compile(
    r"\(\s*[a-z](?:\s*\+\s*[a-z])+\s*\)|\(\s*[a-z]\s*\)\s*[+t]\s*\(\s*[a-z]\s*\)", re.I)
#: The first words of a label line that continues the line above it.
_CONTINUATION = re.compile(r"^\s*(?:\(|[a-z]|(?:and|or|of|to|for|from|in|on|with)\b)")
#: A heading, or a label, that announces movements in balances.
_MOVEMENT_HEADING = re.compile(
    r"\b(?:movements?|changes?)\s+in\s+(?:working\s+capital|operating\s+"
    r"(?:assets|liabilities)|assets\s+and\s+liabilities)"
    r"|\bworking\s+capital\s+(?:changes|adjustments|movements)"
    r"|\(\s*increase\s*\)\s*/\s*decrease|\bincrease\s*/\s*\(?\s*decrease"
    r"|\bdecrease\s*/\s*\(?\s*increase|\badjustments?\s+for\s+(?:changes|\(?\s*increase)",
    re.I)
#: A movements heading closing a label with its colon.
_MOVEMENT_TRAILER = re.compile(
    r"(?:(?:movements?|changes?)\s+in\s+(?:working\s+capital|operating\s+"
    r"(?:assets|liabilities)[\w\s]*|assets\s+and\s+liabilities)|working\s+capital"
    r"\s+(?:changes|adjustments|movements))\s*:\s*$", re.I)
#: A label line that begins mid-phrase, other than "of which".
_OPENS_WITH_CONNECTIVE = re.compile(
    r"^\s*(?:of(?!\s*which\b)|and|or|to|for|from|in|on|with)\s+[a-z]", re.I)
#: A row that says to whom the row above belongs and not what it is:
#: "Attributable to owners of parent" under "Profit (loss) after tax".
_SUBJECTLESS = re.compile(r"^\s*(?:[-–—•]\s*)?attributable\s+to\b", re.I)
#: A row that names only a holder -- "Owners of the Company", "(b) Non-
#: controlling interest". Whose share OF WHAT is the heading's to say.
_HOLDER = re.compile(
    r"^\s*(?:[-–—•]\s*)?(?:\(?\s*(?:[a-z]|[ivx]{1,4})\s*\)\s*)?(?:the\s+)?"
    r"(?:owners?|shareholders?|equity\s+(?:share\s*)?holders?|"
    r"non[-\s]*controlling\s+interests?|minority\s+interests?)\b", re.I)
#: A cell that prints only a dash.
_NIL_CELL = re.compile(r"^\s*[-–—]\s*$")
#: Both holders in one label -- "Owners of the Company Non controlling
#: interest" -- is two printed lines read as one row: whose the figures are
#: is not known (Phase 22).
_OWNERS_WORD = re.compile(r"\b(?:owners?|shareholders?|equity\s+(?:share\s*)?holders?)\b", re.I)
_MINORITY_WORD = re.compile(r"\b(?:non[-\s]*controlling|minority)\b", re.I)


#: A basis a row's own label names: "CRAR (standalone)", "Consolidated
#: Profit". The label is the most specific statement of whose figure it is.
_LABEL_BASIS = re.compile(r"\b(stand[\s-]?alone|consolidated)\b", re.I)
#: A column that is a maturity or ageing bucket -- "1-7 D", ">1-3 Y", ">6 M-1
#: Y", "Over 5 years" -- holds a part of each line's measure, not the measure.
_BUCKET_HEAD = re.compile(
    r"^\s*(?:(?:[<>≤≥]|up\s*to|upto|over|above|more\s+than|less\s+than)\s*\d+(?:\.\d+)?\s*"
    r"(?:d|days?|m|mths?|months?|y|yrs?|years?)\b"
    r"|(?:[<>≤≥]\s*)?\d+(?:\.\d+)?\s*(?:(?:d|days?|m|mths?|months?|y|yrs?|years?)\s*)?(?:-|–|to)\s*"
    r"\d+(?:\.\d+)?\s*(?:d|days?|m|mths?|months?|y|yrs?|years?)\b)"
    r"(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(?:d|days?|m|mths?|months?|y|yrs?|years?)\b)?\s*$",
    re.I)
#: A table whose figures are people, not money: "Full-time equivalents",
#: "Employees by region", "Headcount".
_PEOPLE_TABLE = re.compile(
    r"\b(?:full[-\s]*time\s+equivalents?|headcount|FTEs?|employees\s+by|number\s+of\s+employees)\b",
    re.I)
#: Words a figure's own cell may carry: scales, currencies, nil and rate marks.
#: Any other word of three letters or more -- a stamp's "PUNE-35", a slide's
#: "Improving versus 2025" -- means the cell is text with a number in it.
_CELL_WORDS_ALLOWED = frozenset({
    "crore", "crores", "lakh", "lakhs", "million", "millions", "billion", "billions",
    "thousand", "thousands", "rs", "inr", "usd", "eur", "gbp", "chf", "jpy", "yen",
    "bps", "times", "nil", "cr", "mn", "bn"})
_CELL_WORD = re.compile(r"[A-Za-z]{3,}")


def _basis_entity(basis: str, ctx: TableFactContext):
    """(name, id, type, parent) for a stated basis, as the document declared it."""
    declared = ctx.basis_entities.get(basis)
    if declared is not None:
        return declared
    return (basis.capitalize(), None,
            EntityType.STANDALONE if basis == "standalone" else EntityType.CONSOLIDATED, None)


def _one_basis(text: str) -> str | None:
    words = {re.sub(r"[\s-]", "", m.group(1).lower()) for m in _LABEL_BASIS.finditer(text or "")}
    return words.pop() if len(words) == 1 else None


def _rebase(fact: FinancialFact, basis: str, ctx: TableFactContext, why: str) -> None:
    fact.entity, fact.entity_id, fact.entity_type, fact.parent_entity_id = _basis_entity(basis, ctx)
    fact.meta = {**(fact.meta or {}), "basis_from": why}


def _off_the_label_line(label_cell: TableCell | None, cell: TableCell,
                        line_height: float | None) -> bool:
    """Whether a figure sits on a different printed line than its label.

    On a scanned statement the row clustering joined the heading "Other
    comprehensive income ... attributable to:" to the figures of the
    unlabelled total line just above it, and those figures were filed as other
    comprehensive income (Phase 22). Only a label of one line is judged -- a
    label wrapped over two lines may carry its figures on either -- and the
    figure must stand more than half a line above or below it. On the corpus
    this marks nine figures, eight of them wrong and none correct.
    """
    if label_cell is None or line_height is None:
        return False
    lb, vb = label_cell.bbox, cell.bbox
    if lb is None or vb is None or lb.height <= 0 or vb.height <= 0:
        return False
    if lb.height > 1.5 * line_height:
        return False
    return abs((vb.y + vb.height / 2) - (lb.y + lb.height / 2)) > 0.5 * line_height
#: A heading, or a label, that ends by announcing to whom: "... attributable to:".
_DANGLING_ATTRIBUTION = re.compile(r"\battributable\s+to\s*:?\s*$", re.I)
#: A leading enumeration, dropped when a holder row is read with its heading.
_ENUM_PREFIX = re.compile(r"^\s*(?:[-–—•]|\(?\s*(?:[a-z]|[ivx]{1,4})\s*\))\s*", re.I)
#: The measure a heading attributes, when the heading ran into the row above:
#: "Total comprehensive income for the period IX Profit (loss) for the period
#: attributable to" attributes the PROFIT. The nearest measure before the
#: closing "attributable to", with no other measure between.
_ATTRIBUTION_CLAUSE = re.compile(
    r"(?:(?:net\s+)?profit(?:\s*\(\s*loss\s*\))?|(?:total\s+|other\s+)?comprehensive\s+"
    r"income(?:\s*\(\s*loss\s*\))?|net\s+income|earnings)"
    r"(?:(?!\bprofit\b|\bcomprehensive\b|\bnet\s+income\b).)*?"
    r"\battributable\s+to\s*:?\s*$", re.I)
#: A heading or label that puts the rows beneath it in a per-share context:
#: "Dividend per equity share of ₹ 2 each" over "Interim dividend paid (in ₹)".
#: ("Face value ₹ 2 per share" describes the shares, not the figure.)
_PER_SHARE = re.compile(r"\bper\s+(?:equity\s+|ordinary\s+)?shares?\b", re.I)
_FACE_VALUE = re.compile(r"\bface\s+value\b", re.I)
#: The row after which a cash-flow statement adjusts for working capital:
#: "Operating profit before working capital changes", followed by a heading
#: as plain as "Adjustment for:" and the balances that moved.
_BEFORE_WORKING_CAPITAL = re.compile(
    r"before\s+(?:working\s+capital\s+changes|changes\s+in\s+working\s+capital)", re.I)
#: A label-only heading whose object is the rows below: "Net change in".
_CHANGE_IN_HEADING = re.compile(r"^\s*(?:net\s+)?(?:changes?|movements?)\s+in\s*:?\s*$", re.I)


#: A sentence that introduces what follows it.
_INTRODUCES = re.compile(
    r"(?:given|set\s+out|shown|furnished|presented|stated)\s+below|as\s+follows"
    r"|\bthe\s+following\b|:\s*$", re.I)
_BASIS_WORD = re.compile(r"\b(stand[\s-]?alone|consolidated)\b", re.I)


def introduced_basis(table: ExtractedTable) -> str | None:
    """The basis stated by the sentence that introduces a table, if one is.

    A consolidated results page carries, in its notes, "The specified items of
    the standalone financial results of the Company ... are given below:" and
    then a small table with no title. The page and its section are
    consolidated; the table is standalone, and says so only there. The
    sentence must introduce the table and name exactly one basis.
    """
    if _BASIS_WORD.search(table.title or ""):
        return None
    meta = getattr(table, "meta", None) or {}
    if meta.get("column_basis"):
        return None
    caption = (table.caption or "").strip()
    if not caption:
        return None
    last = re.split(r"(?<=[.;])\s+(?=[A-Z(])", caption)[-1]
    if not _INTRODUCES.search(last):
        return None
    words = {re.sub(r"[\s-]", "", m.group(1).lower()) for m in _BASIS_WORD.finditer(last)}
    return words.pop() if len(words) == 1 else None


def apply_introduced_basis(table: ExtractedTable) -> str | None:
    """Record an introducing sentence's basis as every column's basis."""
    basis = introduced_basis(table)
    if basis is None:
        return None
    table.meta = dict(getattr(table, "meta", None) or {})
    table.meta["column_basis"] = {str(c.index): basis for c in table.columns}
    table.meta["basis_from_introduction"] = basis
    return basis


def _compose(first: str, second: str, stock: bool | None) -> MetricMatch | None:
    """The taxonomy's own reading of two label lines read as one -- only when
    it recognises the whole exactly. A similarity judgement on a label this
    function assembled would be a guess about a guess."""
    m = match_metric(f"{first.rstrip(': ')} {second}".strip(), stock=stock)
    return m if m is not None and m.is_exact else None


#: Below this share of period columns actually carrying data, the header row
#: is not describing the columns underneath it.
_MIN_PERIOD_COLUMNS_WITH_DATA = 0.5
#: Fewer than this many period columns is too small a sample to conclude
#: anything from -- one empty column beside one full one proves nothing.
_MIN_PERIOD_COLUMNS_TO_JUDGE = 3


def _header_row_is_misaligned(table: ExtractedTable,
                              period_cols: list[tuple[TableColumn, object]],
                              n_header: int) -> bool:
    """Whether the header row's columns line up with the data's.

    A well-formed table puts its figures under its headings. A graphic that the
    detector has read as a table often does not: on one results slide the
    headings ``Jun-25``, ``Mar-26`` and ``Jun-26`` were reconstructed at column
    positions holding no cells at all, while the figures sat three positions to
    the right. Pairing heading to figure by index then handed a balance-sheet
    total the year-on-year percentage column's date.

    The evidence is simply whether the dated columns contain anything. When
    most of them are empty, the heading positions cannot be trusted for any
    column of the table, so none is used. The figures survive with no period,
    which is honest and visible, rather than with someone else's.
    """
    if len(period_cols) < _MIN_PERIOD_COLUMNS_TO_JUDGE:
        return False
    populated = {c.col for c in table.cells
                 if c.row >= n_header and c.text and c.text.strip()}
    with_data = sum(1 for col, _ in period_cols if col.index in populated)
    return (with_data / len(period_cols)) < _MIN_PERIOD_COLUMNS_WITH_DATA


def extract_from_table(table: ExtractedTable, ctx: TableFactContext,
                       ) -> list[FinancialFact]:
    """Turn one reconstructed table into facts."""
    if not table.cells or not table.columns:
        return []

    n_header = _header_row_count(table)
    label_cols = _label_columns(table, n_header)
    uom_cols = _uom_columns(table, label_cols)
    reference_cols = _reference_columns(table, n_header)
    if reference_cols:
        table.warnings.append("A column of note references was read as references, "
                              "not figures.")
    # A maturity or ageing bucket is a part of each line's measure: "Advances
    # >2-3 M 21,389" is the advances falling due in two to three months, and
    # was filed as the lender's advances (Phase 22, section-40 audit).
    bucket_cols = {c.index for c in table.columns if _BUCKET_HEAD.match(_head(c) or "")}
    data_cols = [c for c in table.columns
                 if c.index not in label_cols and c.index not in reference_cols
                 and c.index not in bucket_cols]
    if not data_cols:
        return []

    period_cols = [(c, _period_for(c, ctx.calendar)) for c in data_cols
                   if not c.is_growth_column]
    period_cols = [(c, p) for c, p in period_cols if p is not None]
    if _header_row_is_misaligned(table, period_cols, n_header):
        # The header row's columns do not line up with the data's. Pairing
        # them by index would hand each figure the heading of a different
        # column, which is worse than having no heading at all: the figure
        # arrives dated, confidently, and wrong.
        log.info("extraction.header_alignment_rejected",
                 document_id=ctx.document_id, table_id=table.id,
                 page_number=table.page_number,
                 period_columns=len(period_cols))
        table.warnings.append(
            "Column headings did not line up with the data columns, so no "
            "reporting period was taken from them.")
        period_cols = []
    growth_cols = [c for c in data_cols if c.is_growth_column]
    # Columns with no parseable period still carry data; keep them so nothing is
    # dropped, but they will produce facts with period=None.
    plain_cols = [c for c in data_cols
                  if not c.is_growth_column
                  and c.index not in {pc.index for pc, _ in period_cols}]
    # Why a column has no period is not one question but two, and a reader
    # needs them apart.  A head that reads as a period and did not resolve is a
    # gap worth reviewing.  A head that is a segment, a geography or a unit
    # declaration was never offering a period, so there is nothing to find --
    # flagging those is what turns an ordinary segment table into a screen of
    # warnings.  The head's own shape is the evidence.
    # A column with *no head at all* is the third case, and conflating it with
    # the second is what made this phase necessary. An empty head is not a
    # document saying "this column has no period"; it is a head we failed to
    # read -- and when a statement was split into fragments, every data column
    # of every fragment below the first arrived with one. Groww's consolidated
    # P&L produced 89 facts labelled NOT_APPLICABLE, unflagged and at HIGH
    # confidence, from four columns that are plainly dated in the source.
    #
    # Absence of evidence is not evidence of absence. An empty head is UNKNOWN.
    def _basis_for(column) -> PeriodBasis:
        head = (column.header or "").strip()
        if not head:
            return PeriodBasis.UNKNOWN
        if looks_like_period_header(head):
            return PeriodBasis.UNKNOWN
        return PeriodBasis.NOT_APPLICABLE

    unattributed = {c.index: _basis_for(c) for c in plain_cols}

    table_unit = table.unit_hint or ctx.default_unit
    # The table's own declaration governs the table. When it names a currency
    # the figures are in it. When it declares a unit but its currency glyph was
    # not legible -- "(₹ in millions)" read as "( in millions)", "€ millions"
    # as "E millions" -- the currency is unknown: borrowing the document's
    # would be choosing between the currencies the document mentions, which is
    # a guess, and on three filings it chose dollars for rupee and euro
    # statements. Only a table that declares nothing may fall back to the
    # document, and a figure that does so says it was inferred.
    declared_unit = bool((getattr(table, "meta", None) or {}).get("unit_declaration"))
    if table.currency_hint is not None:
        table_currency, currency_from_document = table.currency_hint, False
    elif declared_unit:
        table_currency, currency_from_document = None, False
    else:
        table_currency = ctx.default_currency
        currency_from_document = ctx.default_currency is not None
    context_text = f"{table.title} {table.caption}".strip()
    #: Figures that count people. A software company's "Employees by Region
    #: and Functional Areas" -- full-time equivalents -- was read as its
    #: general and administrative expense, in millions (Phase 22).
    stub_head = (table.columns[0].header or "") if table.columns else ""
    people_table = bool(_PEOPLE_TABLE.search(f"{table.title or ''} {stub_head}"))
    #: A basis stated by a line inside the table that introduces the rows
    #: below it -- "Key standalone financial information is given below:" --
    #: where two tables were stacked into one under a consolidated caption.
    below_basis: str | None = None
    #: Columns whose heads say their figures are rates: "∆ in %" (read "A in
    #: %"), "YoY (%)". Their bare figures are rates like any "12%".
    rate_columns = {c.index for c in table.columns if states_rate(_head(c))}
    #: Columns whose own heads name their currency (a translation column).
    column_currency = (getattr(table, "meta", None) or {}).get("column_currency") or {}

    facts: list[FinancialFact] = []
    data_rows = sorted({c.row for c in table.cells if c.row >= n_header})

    #: The label of a sub-heading row currently in force, and the row it was on.
    #: A statement groups rows under a heading that carries no figures of its
    #: own -- "Profit for the period/year attributable to:" followed by
    #: "Shareholders of the Company" and "Total". Neither child names a measure
    #: by itself, and read alone they produce nothing; read under their parent
    #: they are profit attributable to shareholders, which is a line every
    #: analyst wants.
    heading: str | None = None
    heading_colon = False
    heading_row = -99
    #: The last row that is not itself a component of the row above it.
    parent: str | None = None
    #: The last row whose label declares a sum of lettered components --
    #: "Other income (a+b+c)". A component's figures can sit on a wrapped
    #: line that is not itself enumerated, so the last row read is not always
    #: the total the component belongs to.
    sum_parent: str | None = None
    #: Whether the table's columns say balances or flows (see
    #: `_statement_kind`); it decides the few labels whose words alone do not.
    stock = _statement_kind(period_cols)
    #: Whether a heading announcing MOVEMENTS governs the rows below:
    #: "Movements in working capital:", "(Increase)/decrease in operating
    #: assets". A balance named under it is the movement in that balance.
    movement = False
    #: Whether the rows since "... before working capital changes" are that
    #: statement's working-capital adjustments. Its heading can be as plain
    #: as "Adjustment for:", which on its own announces nothing.
    wc_block = False

    rows_info: list[tuple[int, str, bool]] = []
    anchors: dict[int, TableCell | None] = {}
    for row in data_rows:
        label, anchor = _row_label(table, row, label_cols, uom_cols)
        if not label:
            continue
        anchors[row] = anchor
        has_values = any(c.is_numeric and c.text.strip()
                         and c.col not in label_cols
                         for c in table.cells if c.row == row)
        rows_info.append((row, label, has_values))

    #: One printed line's height here: the median height of the figure cells.
    heights = sorted(c.bbox.height for c in table.cells
                     if c.col not in label_cols and c.is_numeric and c.bbox is not None
                     and c.bbox.height > 0)
    line_height = heights[len(heights) // 2] if heights else None

    continued: set[int] = set()
    for pos, (row, label, has_values) in enumerate(rows_info):
        # A label-only row that reads as an introduction governs what follows.
        # The colon is the reliable signal for a heading that takes rows under
        # it, and such a heading may be read with its child as the taxonomy
        # judges best. A label-only row without one -- the first line of a
        # label whose figures sit on its second ("PROFIT FROM ORDINARY
        # ACTIVITIES BEFORE EXCEPTIONAL" over "ITEMS AND TAX (7)-(8)"), or
        # "Earnings per share" over "Basic" and "Diluted" -- may govern the
        # rows below only where the two together are a label the taxonomy
        # declares word for word.
        if not has_values:
            if _INTRODUCES.search(label) and _one_basis(label):
                below_basis = _one_basis(label)
            if row not in continued:
                heading = label
                heading_colon = label.rstrip().endswith(":")
                heading_row = row
                movement = bool(_MOVEMENT_HEADING.search(label)
                                or _CHANGE_IN_HEADING.match(label)) or wc_block
            continue

        printed = label
        match = match_metric(label, stock=stock)
        # A line that opens with a connective continues the label above it and
        # names nothing alone: "of tax expense (not annualised)" -- the second
        # line of "a) Basic EPS before and after extraordinary items, net" --
        # was filed as the tax charge, carrying the EPS figures. Only the
        # heading composition below may name it. ("Of which ..." is a
        # component and is read as one.)
        if match is not None and _OPENS_WITH_CONNECTIVE.match(printed):
            match = None

        # A label wrapped onto a line of its own below the figures: "Operating
        # profit (3-6)" over "(Profit before provisions and contingencies)".
        # The full label is the row's name. Taken only when the taxonomy
        # recognises the whole exactly.
        nxt = rows_info[pos + 1] if pos + 1 < len(rows_info) else None
        if (nxt is not None and not nxt[2] and nxt[0] - row == 1
                and _CONTINUATION.match(nxt[1])):
            joined = _compose(label, nxt[1], stock)
            if joined is not None:
                match, label = joined, f"{label} {nxt[1]}"
                continued.add(nxt[0])

        if heading and row - heading_row <= 3:
            # Only when the child means nothing alone. A row that names its own
            # measure keeps it: "Revenue from operations" under any heading is
            # still revenue from operations, and prefixing it would invent a
            # measure the statement does not report.
            #
            # With one exception: the label-only line DIRECTLY above, which
            # together with this one is a label the taxonomy declares word for
            # word. That is one label printed over two lines, and its second
            # line alone is a fragment -- "MINORITY INTEREST (9)-(10)+(11)"
            # under "PROFIT FROM ORDINARY ACTIVITIES BEFORE TAX AND" was read
            # as the minority's share of profit, and "of tax expense (in ₹)"
            # under "Basic EPS ..., net" as the tax charge.
            combined = f"{heading.rstrip(': ')} {label}".strip()
            m = match_metric(combined, stock=stock)
            wrapped = row - heading_row == 1 and m is not None and m.is_exact
            if m is not None and (
                    (match is None and (heading_colon or m.is_exact))
                    or (wrapped and (match is None or m.key != match.key))):
                match, label = m, combined

        # A row naming only a holder -- "Non-controlling interest" -- under a
        # heading that attributes something is that holder's share OF WHAT THE
        # HEADING NAMES, however the row reads alone. Read alone, the bare name
        # is the minority's share of profit; two rows under "Other
        # comprehensive income ... attributable to" it was the minority's share
        # of OCI, and was filed as its share of profit. Where the heading and
        # the holder together name nothing the taxonomy knows, the row names
        # nothing.
        if (heading and row - heading_row <= 3 and _HOLDER.match(printed)
                and _DANGLING_ATTRIBUTION.search(heading)):
            clause = _ATTRIBUTION_CLAUSE.search(heading)
            subject = (clause.group(0) if clause else heading).rstrip(": ")
            combined = f"{subject} {_ENUM_PREFIX.sub('', printed)}".strip()
            m = match_metric(combined, stock=stock)
            match, label = (m, combined) if m is not None else (None, label)
        if (match is not None and _HOLDER.match(printed)
                and _OWNERS_WORD.search(printed) and _MINORITY_WORD.search(printed)):
            match = None

        # A per-share context: "Total dividend paid (in ₹)" under "Dividend per
        # equity share of ₹ 2 each" is rupees per share, and was filed as the
        # dividends a cash-flow statement pays, in crore. No monetary measure
        # is read there; the per-share measures are declared as such.
        per_share_heading = (heading and row - heading_row <= 3
                             and _PER_SHARE.search(heading)
                             and not _FACE_VALUE.search(heading)
                             and not re.match(r"^\s*(?:basic\s+and\s+diluted\s+)?earnings",
                                              heading, re.I))
        if (match is not None and match.definition.value_type == ValueType.MONETARY
                and (per_share_heading or (_PER_SHARE.search(printed)
                                           and not _FACE_VALUE.search(printed)))):
            match = None

        if match is None and parent and (_ENUMERATED_CHILD.search(printed)
                                         or _SUBJECTLESS.match(printed)):
            # A component printed under its total: "(d) Others" under
            # "Interest earned (a)+(b)+(c)+(d)" is the interest earned from
            # other sources, and means nothing read alone. So is a share of
            # the row above: "Attributable to owners of parent".
            m = _compose(parent, label, stock)
            if m is not None:
                match, label = m, f"{parent} {label}"

        if not _ENUMERATED_CHILD.search(printed) and not _SUBJECTLESS.match(printed):
            parent = printed
            if _SUM_OF_PARTS.search(printed):
                sum_parent = printed
        # A balance named under a heading that announces movements -- or in a
        # label that says so itself, "Increase/decrease in trade receivables"
        # -- is the period's movement in that balance, not the balance: a
        # cash-flow statement's "- Trade receivables (126)" under "Movements in
        # working capital:" had been filed as receivables of -126. The
        # taxonomy has no key for a movement, so the row is not named. A flow
        # total ("Cash generated from operations") ends the block.
        if match is not None and (movement or _MOVEMENT_HEADING.search(printed)):
            if not match.definition.is_flow:
                match = None
            elif not _ENUMERATED_CHILD.search(printed):
                movement = wc_block = False
        # A movements heading run into the end of the row above -- "... Changes
        # in operating assets and liabilities:" -- still governs what follows.
        if _MOVEMENT_TRAILER.search(printed):
            movement = True
        # So does the line that closes the non-cash adjustments: what follows
        # "Operating profit before working capital changes" is the movement in
        # each working-capital balance, under whatever heading.
        if _BEFORE_WORKING_CAPITAL.search(printed):
            movement = wc_block = True
        # An attributing heading run into the end of a row that has figures of
        # its own -- "Total comprehensive income ... IX Profit (loss) for the
        # period / year attributable to" -- still attributes the rows below.
        if _DANGLING_ATTRIBUTION.search(printed):
            clause = _ATTRIBUTION_CLAUSE.search(printed)
            if clause:
                heading, heading_row, heading_colon = clause.group(0), row, True
        if match is not None and people_table and match.definition.value_type == ValueType.MONETARY:
            match = None
        if match is None:
            continue
        uom = _uom_text(table, row, uom_cols)
        label_basis = _one_basis(printed)
        # Unit resolution is strictly layered, most specific first:
        #   1. the row label and its own unit-of-measure cell
        #   2. the table's declared unit (already resolved from an explicit
        #      "(Rs in crore)" declaration by the table builder)
        # The surrounding caption prose is deliberately NOT consulted here.  A
        # window of narrative above a rupee table routinely mentions "533
        # million subscribers", and letting that through reported the whole
        # table in millions.
        row_context = f"{label} {uom}".strip()
        # A row that names its own unit -- in a unit-of-measure cell, or in the
        # label's own "(cents)" -- has overruled the table's declaration.
        row_declares_unit = bool(uom.strip()) or states_sub_unit(row_context)

        # Whether the row reports amounts: some figure in it is a plain
        # number, not a rate -- neither marked "%" itself nor standing in a
        # column headed as one.
        row_has_amounts = any(
            c.is_numeric and c.text.strip() and not states_rate(c.text)
            and c.col not in rate_columns
            for c in table.cells if c.row == row and c.col not in label_cols)

        # ---- period columns ------------------------------------------------
        row_facts: list[tuple[FinancialFact, TableColumn, NormalizedPeriod | None]] = []
        for col, period in period_cols + [(c, None) for c in plain_cols]:
            cell = _cell_at(table, row, col.index)
            if cell is None or not cell.text.strip():
                continue
            own_currency = column_currency.get(str(col.index))
            fact = _fact_from_cell(
                cell=cell, table=table, ctx=ctx, match=match, label=label,
                column=col, period=period, row_context=row_context,
                table_unit=table_unit,
                table_currency=(Currency(own_currency) if own_currency
                                else table_currency),
                currency_from_document=(False if own_currency
                                        else currency_from_document),
                row_declares_unit=row_declares_unit,
                row_has_amounts=row_has_amounts,
                no_period_basis=unattributed.get(col.index,
                                                 PeriodBasis.UNKNOWN))
            if fact is not None:
                if _off_the_label_line(anchors.get(row), cell, line_height):
                    fact.meta = {**(fact.meta or {}), "row_misaligned": True}
                # The row's own words outrank the table's: "Consolidated
                # Profit" on a standalone-to-consolidated bridge, "CRAR
                # (standalone)" on a consolidated slide (Phase 22).
                if label_basis:
                    _rebase(fact, label_basis, ctx, "the row's label")
                elif below_basis:
                    _rebase(fact, below_basis, ctx, "a line in the table introducing the rows below")
                facts.append(fact)
                row_facts.append((fact, col, period))

        _link_growth(row_facts, table, row, growth_cols, ctx)
        # A component printed under a total OF THE SAME NAME -- "(c) Other
        # income" under "Other income (a+b+c)". Recorded so that no identity
        # ever takes the part for the whole: where the total's own row was
        # unreadable, a bank's total income was tested against interest plus
        # one third of its other income, and "failed".
        if _ENUMERATED_CHILD.search(printed) and (parent or sum_parent):
            own = normalize_label(printed)
            if own and any(normalize_label(p).startswith(own)
                           for p in (parent, sum_parent) if p):
                for fact, _, _ in row_facts:
                    fact.meta = {**(fact.meta or {}), "component": True}

    # ---- the safety gate ---------------------------------------------------
    # Whether a missing period is a defect or a fact about the document depends
    # entirely on what kind of table this is. A segment table has no periods to
    # find; a statement of profit and loss has one for every figure. Nothing
    # downstream could tell those apart, so a figure from a dated statement that
    # lost its period reached users at HIGH confidence, unflagged.
    shape = classify_table(table)
    mandatory = period_is_mandatory(shape)
    report = validate_table(table, n_header=n_header)
    table.meta = dict(getattr(table, "meta", None) or {})
    table.meta["shape"] = str(shape)
    table.meta["validation"] = report.as_dict()
    for finding in report.findings:
        table.warnings.append(finding.message)

    # A reconstruction that contradicts itself cannot vouch for which column a
    # figure came from, and therefore cannot vouch for its period -- however
    # confidently the period parsed.
    column_established = None
    if mandatory and (not period_cols or report.period_is_unsafe):
        column_established = False

    _bank_meanings(facts)
    facts = _cash_flow_balances(facts, table)
    facts = _same_name_lines(facts, table)

    held = 0
    for fact in facts:
        fact.meta = dict(fact.meta or {})
        fact.meta["table_shape"] = str(shape)
        result = safety_gate.check(
            fact, period_mandatory=mandatory,
            column_established=column_established)
        if safety_gate.apply(fact, result,
                             review_threshold=settings.review_threshold):
            held += 1
    if held:
        log.info("extraction.safety_gate_held", document_id=ctx.document_id,
                 table_id=table.id, page_number=table.page_number,
                 shape=str(shape), held=held, of=len(facts))

    return facts


#: A figure with a thousands group -- "13,72,936", "4 579" -- inside a column
#: head is a data row the header reconstruction swallowed.
_GROUPED_FIGURE = re.compile(r"(?<![A-Za-z\d])\d{1,3}(?:[,\s]\d{2,3})+(?:\.\d+)?(?!\d)")


def _head(column: TableColumn) -> str:
    """A column's head up to the first figure of data swallowed into it.

    A release's balance table was reconstructed with its first data rows in
    the column heads -- "As on 30th June'26 13,72,936 5,21,698 ... 38%" --
    and the "%" of one of those rows made every crore figure in the column a
    percentage (Phase 22). What a head says about its column is what it says
    before any such figure."""
    head = column.header or ""
    m = _GROUPED_FIGURE.search(head)
    return head[:m.start()] if m else head


def _value_type_claims(*, expected: ValueType, cell: TableCell, label: str,
                       row_context: str, column: TableColumn,
                       ) -> list[Claim]:
    """What each level of the table says this figure is.

    Only positive statements are collected. "This cell has no percent sign" is
    not evidence that it is money -- most money has no symbol either -- so an
    absence contributes nothing and lets a weaker level speak.
    """
    claims = [Claim(expected, Level.TAXONOMY,
                    "what this metric name usually means")]
    if states_rate(_head(column)):
        claims.append(Claim(ValueType.PERCENTAGE, Level.TABLE,
                            "the column heading, which states a rate"))
    if states_rate(row_context) or states_rate(label):
        claims.append(Claim(ValueType.PERCENTAGE, Level.ROW,
                            "the row label, which states a rate"))
    if states_rate(cell.text):
        claims.append(Claim(ValueType.PERCENTAGE, Level.CELL,
                            "the figure itself, which carries a percent sign"))
    return claims


def _taxonomy_rate_is_contradicted(
        expected: ValueType, cell: TableCell, label: str, row_context: str,
        column: TableColumn, table_unit: MagnitudeUnit | None) -> bool:
    """Whether the source contradicts the taxonomy calling this metric a rate.

    The taxonomy says what a metric *name* usually means; the document says
    what this figure *is*.  Those are different kinds of claim, and where they
    disagree the document wins -- otherwise a statement that declares itself in
    millions and prints a "Gross margin" line of 43,718 reports it as "43,718
    percent", which no later stage can detect and no reader would expect.

    The name is overruled only when all three hold:

    * the source says "rate" nowhere -- not in the cell, the row label, the
      unit-of-measure cell, nor the column head.  An explicit ``(%)`` is the
      document speaking and always wins, which is what keeps "EBITDA Margin
      (%) 15.9" a percentage in a table declared in crore;
    * the table carries an explicit magnitude declaration, so there is real
      evidence to prefer rather than merely an absence of evidence;
    * the figure is too large to be a rate at all, so nothing that could
      legitimately be a percentage is ever reclassified.

    Any one of the three failing leaves the taxonomy's reading untouched.
    """
    if expected not in (ValueType.PERCENTAGE, ValueType.RATIO):
        return False                      # counts and per-share are not rates
    if table_unit is None:
        return False                      # no declaration to prefer
    if states_rate(cell.text) or states_rate(row_context) \
            or states_rate(label) or states_rate(_head(column)):
        return False                      # the document said so; it wins
    parsed = parse_number(cell.text)
    if parsed is None:
        return False
    return abs(parsed[0]) > RATE_CEILING


def _fact_from_cell(*, cell: TableCell, table: ExtractedTable,
                    ctx: TableFactContext, match: MetricMatch, label: str,
                    column: TableColumn, period: NormalizedPeriod | None,
                    row_context: str, table_unit: MagnitudeUnit | None,
                    table_currency: Currency | None,
                    currency_from_document: bool = False,
                    row_declares_unit: bool = False,
                    row_has_amounts: bool = False,
                    no_period_basis: PeriodBasis = PeriodBasis.UNKNOWN,
                    ) -> FinancialFact | None:
    # A printed dash is nil -- the statement's way of saying there is nothing
    # to report -- and not a figure of zero: a fact of 0.0 would be a number
    # the statement never printed (Phase 22; "a '-' extracted as 0").
    if _NIL_CELL.match(normalise_separators(cell.text or "")):
        return None
    # A cell whose text carries a word is text with a number in it, not a
    # figure: a stamp's "PUNE-35" was read as 35 (Phase 22).
    if any(w.lower() not in _CELL_WORDS_ALLOWED for w in _CELL_WORD.findall(cell.text or "")):
        return None
    # A cell holding two figures is a missed column boundary, not a value. The
    # parser would return the first and discard the second, so the number would
    # be right and its column wrong -- which is how a figure came to be dated
    # to the wrong quarter while reading as a clean, confident fact. Neither
    # figure can be placed, so neither becomes one.
    if holds_more_than_one_number(
            repair_by_decimals(cell.text, table_decimals_of(table)) or cell.text,
            grouping=table_grouping_of(table)):
        log.info("extraction.ambiguous_cell", document_id=ctx.document_id,
                 table_id=table.id, page_number=table.page_number,
                 row=cell.row, col=cell.col, text=cell.text[:40])
        warning = ("A cell in this table held more than one figure, so the "
                   "column boundary there is wrong. Those values were not "
                   "extracted rather than assigned to a guessed column.")
        if warning not in table.warnings:
            table.warnings.append(warning)
        return None

    # A rate printed in a row of amounts is not the row's value. A slide's
    # "Profit after tax | 6,081 | 4,765 | 28% | 19,332" carries a year-on-year
    # change in a column whose head was not read as one, and the 28% came out
    # as profit after tax. Where the row's other figures are amounts and the
    # taxonomy's measure is an amount, a figure the document marks as a rate
    # is a comparison of two of them: not extracted, rather than extracted
    # under the wrong name. (A row that is rates throughout -- "CASA 50.8%" --
    # is a different case, and is read as the percentage it says it is.)
    if (row_has_amounts
            and (states_rate(cell.text) or states_rate(_head(column)))
            and match.definition.value_type in (
                ValueType.MONETARY, ValueType.PER_SHARE, ValueType.COUNT)):
        warning = ("A figure marked as a rate stood in a row of amounts, in a "
                   "column not read as a comparison column. It was not "
                   "extracted as the row's value.")
        if warning not in table.warnings:
            table.warnings.append(warning)
        return None

    # A name that means an amount or its rate -- "Gross NPA" is printed both
    # in crore and as a percentage of advances -- is decided by the figure's
    # own marks: a "%" on the cell, in its label or over its column says rate;
    # none of them, in a table that declares a monetary scale, says amount. A
    # bank's typeset results filed its gross NPAs of 6,266.25 crore under the
    # NPA RATIO, and only its scanned twin, read separately, showed it.
    if match.definition.twin:
        rate_marked = (states_rate(cell.text) or states_rate(label)
                       or states_rate(_head(column)))
        declares_scale = bool(table_unit) or bool(
            (getattr(table, "meta", None) or {}).get("unit_declaration"))
        is_rate = match.definition.value_type in RATE_KINDS
        if (rate_marked and not is_rate) or (not rate_marked and is_rate
                                             and declares_scale):
            twin = get_metric(match.definition.twin)
            if twin is not None:
                match = MetricMatch(twin.key, twin, match.score,
                                    match.matched_alias, match.method)

    expected = match.definition.value_type
    rate_overridden = _taxonomy_rate_is_contradicted(
        expected, cell, label, row_context, column, table_unit)
    if rate_overridden:
        expected = ValueType.UNKNOWN

    # What the document says about this figure's kind, and who said it.
    #
    # The taxonomy's prior is the weakest voice here. A cell printed "50.8%"
    # under a metric the taxonomy calls monetary is a percentage -- the reader
    # can see it -- and the spatial path has always refused the alternative.
    # The table path used to accept it, and reported CASA deposits of 50.8 %
    # as an amount of money.
    verdict = resolve_value_type(_value_type_claims(
        expected=expected, cell=cell, label=label,
        row_context=row_context, column=column))
    if verdict.value_type in RATE_KINDS and expected not in RATE_KINDS:
        expected = verdict.value_type
    # A metric declared as a percentage or ratio must not inherit the table's
    # monetary unit -- that is how "EBITDA margin 15.9" becomes "15.9 crore".
    non_monetary = expected in (
        ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT)
    # Nor may a per-share figure, for the same reason in a different disguise:
    # a statement headed "Rs. crore" still prints EPS in rupees, and inheriting
    # the crore would multiply it by ten million. It keeps the currency, which
    # the heading does establish, and refuses the scale.
    takes_a_scale = not non_monetary and expected != ValueType.PER_SHARE
    # The table-level unit is a *fallback*: it does not apply to a
    # non-monetary metric, nor to a row that stated its own unit.
    unit_default = (None if (not takes_a_scale or row_declares_unit)
                    else table_unit)
    cur_default = None if (non_monetary or row_declares_unit) else table_currency

    # A thousands comma read as a decimal point, in a table that prints whole
    # numbers: "9,066" arrived as "9.066" and was reported as nine. Repaired
    # only for amounts -- a ratio, a rate or a per-share figure can genuinely
    # carry three decimals.
    cell_text = cell.text
    if expected not in (ValueType.RATIO, ValueType.PERCENTAGE, ValueType.PER_SHARE):
        repaired = repair_by_decimals(cell.text, table_decimals_of(table))
        if repaired is not None:
            cell_text = repaired
    num = normalize_number(cell_text, context=row_context,
                           default_currency=cur_default,
                           default_magnitude=unit_default,
                           expected_type=expected if expected != ValueType.UNKNOWN
                           else None,
                           default_currency_inferred=currency_from_document)
    if num is None:
        return None

    value_type = num.value_type
    if expected != ValueType.UNKNOWN and value_type == ValueType.UNKNOWN:
        value_type = expected
    if expected in (ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT):
        value_type = expected
    # The source overrules the parse as well as the taxonomy: a cell reading
    # "50.8%" is a percentage whatever else the row or the table declared.
    if verdict.level >= Level.CELL and verdict.value_type in RATE_KINDS:
        value_type = verdict.value_type

    # The column's own basis, where a group head above it declares one. On a
    # table that prints standalone and consolidated side by side under two
    # group heads, the section the table sits in cannot say which columns are
    # which; attributing all eight to the page's basis put a bank's
    # standalone quarter under its consolidated group, unflagged.
    entity_name, entity_id = ctx.entity, ctx.entity_id
    entity_type, parent_entity_id = ctx.entity_type, ctx.parent_entity_id
    column_basis = ((getattr(table, "meta", None) or {}).get("column_basis") or {}
                    ).get(str(column.index))
    if column_basis:
        declared = ctx.basis_entities.get(column_basis)
        if declared is not None:
            entity_name, entity_id, entity_type, parent_entity_id = declared
        else:
            entity_name = column_basis.capitalize()
            entity_id = None
            entity_type = (EntityType.STANDALONE if column_basis == "standalone"
                           else EntityType.CONSOLIDATED)

    # A column with no period whose head names something else -- a segment
    # ("Retail Banking"), a fair-value level, an exposure measure -- holds that
    # thing's figures, not the section's unit's. The fact has no place for the
    # qualifier, so the entity is left unstated and the head kept as evidence:
    # a UK bank's segment tables put 37 segment figures under the group.
    qualifier = None
    if (not column_basis and period is None
            and no_period_basis == PeriodBasis.NOT_APPLICABLE):
        qualifier = _column_qualifier(column.header)
        if qualifier:
            entity_name = entity_id = parent_entity_id = None
            entity_type = EntityType.UNKNOWN

    # A flow figure under a bare date: the head says when the period ended and
    # nothing about how long it ran. Read as "as at", a scanned results page's
    # first column -- "June 30, 2026" over "Unaudited", its "Quarter ended"
    # group head lost to the scan -- reported a quarter's sales as a balance,
    # unflagged. The end date is known and kept; the span is not, and says so.
    span_unstated = False
    if (period is not None and period.period_type == PeriodType.POINT_IN_TIME
            and match.definition.is_flow
            and not _STOCK_HEAD.search(f"{column.header or ''} {period.raw_text or ''}")):
        period = period.model_copy(update={
            "period_type": PeriodType.UNKNOWN, "start_date": None,
            "label": f"Period ending {period.end_date} (span not stated)"})
        span_unstated = True

    separator_repaired = cell_text != cell.text
    fact_id = stable_id("fact", ctx.document_id, table.id, cell.row, cell.col)
    prov = Provenance(
        document_id=ctx.document_id, page_number=table.page_number,
        region_id=table.region_id, table_id=table.id, cell_id=cell.id,
        bbox=cell.bbox, original_text=cell.text,
        ocr_element_ids=list(cell.element_ids),
        page_image_url=ctx.page_image_url,
        extraction_method="table_cell")

    sig = conf_mod.Signals(
        ocr=cell.confidence,
        layout=table.confidence,
        structure=min(1.0, table.confidence * (0.85 + 0.15 * column.numeric_ratio)),
        semantic=match.score,
        corroborated=False,
        notes=[f"metric matched by {match.method} ({match.score:.2f})"])
    breakdown = conf_mod.score(sig)

    base = num.base_value
    if base is None and value_type == ValueType.MONETARY:
        base = None  # unit unknown: do not fabricate a base value

    return FinancialFact(
        fact_id=fact_id, document_id=ctx.document_id, metric=label,
        canonical_metric=match.key, metric_display=match.definition.display,
        entity=entity_name, entity_id=entity_id,
        entity_type=entity_type, parent_entity_id=parent_entity_id,
        statement=match.definition.statement,
        category=match.definition.category,
        derivation=DerivationStatus.REPORTED,
        unit_inferred=(num.magnitude != MagnitudeUnit.NONE
                       and unit_default is not None
                       and num.magnitude == unit_default),
        currency_inferred=num.currency_inferred,
        kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
        value=num.value, raw_value=cell.text, value_type=value_type,
        currency=num.currency, unit=num.magnitude, base_value=base,
        period=period.label if period else None, period_normalized=period,
        confidence=breakdown.overall, confidence_band=breakdown.band,
        confidence_breakdown=breakdown,
        source_page=table.page_number, provenance=prov,
        original_text=cell.text, context_text=table.title or table.caption[:200],
        # The basis the column head states -- "Adjusted", "Reported",
        # "Restated" -- which makes this a different figure from the plain
        # measure in the column beside it (Phase 22).
        qualification=from_column_head(column.header),
        meta={"table_id": table.id, "row": cell.row, "col": cell.col,
              # Why this figure has no period, when it has none.  "The column
              # was a segment, not a date" and "the column looked like a date
              # and would not read" are different answers, and only the second
              # is a gap.
              **({} if period else {"period_absence": str(no_period_basis)}),
              "column_header": column.header,
              # How the printed figure was read -- grouping, decimal mark,
              # sign convention, any repair -- as evidence a reviewer can check.
              "number_format": describe_number(cell_text),
              **({"column_qualifier": qualifier} if qualifier else {}),
              **({"period_span_unstated": True} if span_unstated else {}),
              "metric_match_method": match.method,
              # recorded rather than done silently: a reviewer can see that the
              # metric's usual type was set aside for the table's declaration
              **({"taxonomy_rate_overridden": True} if rate_overridden else {})})


#: What a column head says about scale, currency, audit status, time or basis
#: -- none of it names a unit of the business.
_HEAD_NOISE = re.compile(
    r"[$€£₹¥%]|\bus\$|\b(?:s|\$)?mil+i?on[s]?\b|\bmn\b|\bbn\b|\bbillion\b"
    r"|\bcrores?\b|\blakhs?\b|\bthousands?\b|\b(?:inr|usd|eur|gbp|rs|cr)\b"
    r"|\b(?:un)?audited\b|\breviewed\b|\brestated\b|\brefer\b|\bnotes?\b"
    r"|\b(?:quarters?|ended|ending|years?|half|months?|period|ytd|as|at|on|of"
    r"|the|for|and|in|to|from|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*"
    r"|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\b"
    r"|\b(?:consolidated|standalone|group|company|bank)\b|\d", re.I)
#: Heads that qualify the measure but not whose figure it is: the group's
#: reported or adjusted profit is still the group's.
_MEASURE_WORDS = frozenset({
    "total", "reported", "adjusted", "underlying", "statutory", "ifrs", "gaap",
    "actual", "amount", "value", "particulars", "figures", "current",
    "previous", "corresponding", "comparative"})


def _column_qualifier(header: str | None) -> str | None:
    """The part of a period-less column head that names something the fact
    model cannot carry -- "Retail Banking", "Level 1", "Fair value" -- or None
    when the head says only scale, currency, time, basis or a measure word."""
    words = re.findall(r"[a-z][a-z&\-]{2,}", _HEAD_NOISE.sub(" ", (header or "").lower()))
    # a footnote marker run into the word: "Adjusted¹" read "Adjustedi"
    named = [w for w in words
             if w not in _MEASURE_WORDS and w[:-1] not in _MEASURE_WORDS]
    return " ".join(named) if named else None


#: Lines only a bank's (or a lender's) income statement prints.
_BANK_LINES = frozenset({"interest_earned", "interest_expended", "net_interest_income",
                         "provisions_and_contingencies", "pre_provision_operating_profit",
                         "non_interest_income", "net_total_income", "provisions_for_npa"})
#: "Operating Profit", with nothing that says which operating profit.
_BARE_OPERATING_PROFIT = re.compile(r"^operating profit(?: loss)?$")


def _bank_meanings(facts: list[FinancialFact]) -> None:
    """Names a bank's statement uses in its own sense.

    In a bank's results "Operating profit" is operating income less operating
    expenses -- profit BEFORE provisions and contingencies -- and a results
    deck prints it between "Operating Expenses" and "Total Provisions". Filed
    as EBIT, SBI's pre-provision profit was reported as a corporate operating
    profit. The table says which statement it is: two or more lines only a
    lender prints. Only the bare name is re-read; "Operating profit before
    provisions" already says what it is.
    """
    keys = {f.canonical_metric for f in facts}
    _net_income_meaning(facts, keys)
    if len(keys & _BANK_LINES) < 2:
        return
    ppop = get_metric("pre_provision_operating_profit")
    nti = get_metric("net_total_income")
    for f in facts:
        bare = normalize_label(f.metric or "")
        if f.canonical_metric == "ebit" and _BARE_OPERATING_PROFIT.match(bare):
            _rekey(f, ppop, "a lender's statement: operating profit before "
                            "provisions and contingencies")
        # A lender's "Operating Revenue" is net interest income plus other
        # income -- the income its operating expenses are set against -- not
        # a manufacturer's revenue from operations (Phase 22).
        elif (f.canonical_metric in ("revenue_from_operations", "revenue", "total_income")
                and _BARE_OPERATING_INCOME.match(bare)):
            _rekey(f, nti, "a lender's statement: operating revenue is net "
                           "interest income plus other income")


def _rekey(f: FinancialFact, definition, context: str) -> None:
    f.canonical_metric = definition.key
    f.metric_display = definition.display
    f.statement = definition.statement
    f.category = definition.category
    f.meta = {**(f.meta or {}), "metric_context": context}


#: A lender's operating revenue, with nothing that says which revenue.
_BARE_OPERATING_INCOME = re.compile(r"^(?:total )?operating (?:revenue|income)s?$")
#: Lines only a lender prints -- a bank's, or a finance company's.
_LENDER_LINES = _BANK_LINES | frozenset({
    "aum", "advances", "total_deposits", "impairment_on_financial_instruments",
    "gross_npa_ratio", "net_npa_ratio", "credit_cost"})
#: Every name a table can give its profit after tax.
_PROFIT_AFTER_TAX = frozenset({"pat", "pat_after_associates", "pat_attributable_to_owners"})


def _net_income_meaning(facts: list[FinancialFact], keys: set[str]) -> None:
    """"Net Income" where the same table prints its profit after tax as well.

    Then "net income" cannot be the profit: a finance company's release
    prints "Total Income", "Finance Cost", "Net Income", ... "Profit After
    Tax", and its net income is income less finance cost -- filed as net
    income in the sense of earnings, it was a second, larger profit. In a
    lender's table that is net total income; elsewhere what it is cannot be
    told, and the figure goes to review rather than to a guess.
    """
    if not (keys & _PROFIT_AFTER_TAX):
        return
    lender = bool(keys & _LENDER_LINES) and "finance_costs" in keys
    nti = get_metric("net_total_income")
    for f in facts:
        if f.canonical_metric != "net_income":
            continue
        if normalize_label(f.metric or "") != "net income":
            continue
        if lender:
            _rekey(f, nti, "a lender's table that also prints profit after tax: "
                           "net income is total income less finance cost")
        else:
            f.meta = {**(f.meta or {}), "metric_collision":
                      "the table prints profit after tax as well, so 'net income' "
                      "is not its profit, and what it is instead is not stated"}


#: What only a cash-flow statement prints.
_CASH_FLOW_LINES = frozenset({
    "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
    "cash_generated_from_operations", "net_change_in_cash", "cash_at_end",
    "cash_at_beginning", "operating_profit_before_wc_changes"})
#: A balance a cash-flow statement does print: the cash at either end.
_OPENING_OR_CLOSING = re.compile(r"\b(?:beginning|end|opening|closing)\b", re.I)


def _cash_flow_balances(facts: list[FinancialFact], table: ExtractedTable
                        ) -> list[FinancialFact]:
    """A balance-sheet name inside a cash-flow statement names a movement.

    "Bank balances other than cash and cash equivalents 2351.87" in the
    investing section is the period's movement in those balances, and
    "Trade receivables (1898.13)" under a working-capital heading the movement
    in receivables -- filed as the balances, they were a year's cash flows
    reported as the amounts on the balance sheet. The taxonomy has no key for
    a movement, so none is given. The statement is recognised by the lines
    only a cash-flow statement prints; the cash it opens and closes with is a
    balance it prints, and is kept.
    """
    keys = {f.canonical_metric for f in facts}
    if len(keys & _CASH_FLOW_LINES) < 2:
        return facts
    kept, dropped = [], 0
    for f in facts:
        definition = get_metric(f.canonical_metric)
        if (definition is not None and definition.statement == "balance_sheet"
                and not _OPENING_OR_CLOSING.search(f.metric or "")):
            dropped += 1
            continue
        kept.append(f)
    if dropped:
        note = ("Balance-sheet names inside this cash-flow statement are movements "
                "in those balances; they were not extracted as the balances.")
        if note not in table.warnings:
            table.warnings.append(note)
    return kept


#: How strongly each way of matching a label names its measure.
_MATCH_STRENGTH = {"exact": 3, "recognition-exact": 3, "prefix": 2,
                   "recognition-repair": 1, "fuzzy": 1}


def _strength(f: FinancialFact) -> int:
    meta = f.meta or {}
    # A label ending "... attributable to:" names a heading, whose figures on
    # the page are the rows beneath it; figures attached to it came from a row
    # the grouping merged into it.
    if _DANGLING_ATTRIBUTION.search(f.metric or ""):
        return 0
    return _MATCH_STRENGTH.get(meta.get("metric_match_method", ""), 1)


def _same_name_lines(facts: list[FinancialFact], table: ExtractedTable
                     ) -> list[FinancialFact]:
    """Two lines of one table read as the same measure, with different figures.

    A statement does not print one measure twice in a column with two values.
    When it seems to, one line was read as the other: L&T's "Construction
    materials consumed" matched the cost of materials by similarity beside the
    line that IS the cost of materials; a heading "Other comprehensive income
    ... attributable to:" merged with the figures of the profit subtotal above
    it beside the line that IS other comprehensive income. The line the
    taxonomy recognises most directly keeps the name; weaker readings of the
    same name are not extracted. Where no reading is stronger than another --
    two similarity matches -- neither can be told from the other, and both go
    to review. (Two exact readings are two genuine lines: current and non-
    current borrowings are both "Borrowings".)
    """
    groups: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        meta = f.meta or {}
        groups[(f.canonical_metric, meta.get("col"))].append(f)
    drop: set[str] = set()
    for (_key, _col), members in groups.items():
        if len({(m.meta or {}).get("row") for m in members}) < 2:
            continue
        if len({m.value for m in members}) < 2:
            continue
        best = max(_strength(m) for m in members)
        weaker = [m for m in members if _strength(m) < best]
        strongest = [m for m in members if _strength(m) == best]
        drop.update(m.fact_id for m in weaker)
        if best < 3 and len({(m.meta or {}).get("row") for m in strongest}) > 1:
            for m in strongest:
                m.meta = {**(m.meta or {}), "metric_collision":
                          "two lines of this table were read as this measure "
                          "with different figures, and neither reading is "
                          "stronger"}
    if drop:
        note = ("Two lines of this table were read as one measure; the weaker "
                "reading was not extracted.")
        if note not in table.warnings:
            table.warnings.append(note)
    return [f for f in facts if f.fact_id not in drop]


def _link_growth(row_facts, table: ExtractedTable, row: int,
                 growth_cols: list[TableColumn], ctx: TableFactContext) -> None:
    """Attach comparison values and growth to the facts of one row.

    Reported growth (a "% chg" column in the document) is recorded as reported
    and left untouched; where no growth column exists but two comparable periods
    are present, growth is computed here and marked as *not* reported so the two
    can never be confused.
    """
    if not row_facts:
        return
    dated = [(f, c, p) for f, c, p in row_facts if p is not None]
    reported: dict[str, tuple[float, TableColumn]] = {}

    for gc in growth_cols:
        cell = None
        for c in table.cells:
            if c.row == row and c.col == gc.index:
                cell = c
                break
        if cell is None or not cell.text.strip():
            continue
        num = normalize_number(cell.text, context="%")
        if num is None:
            continue
        val = num.value
        if _BPS_CELL_RE.search(cell.text):
            # a margin change quoted in basis points is not a percent change
            reported[f"bps:{gc.index}"] = (val, gc)
            continue
        reported[f"pct:{gc.index}"] = (val, gc)

    # the "current" period is the most recent dated column in the row
    if dated:
        dated.sort(key=lambda t: (t[2].fiscal_year or 0, t[2].quarter or 0),
                   reverse=True)
        current_fact, _, current_period = dated[0]
        prior_yoy = next((t for t in dated[1:]
                          if is_year_over_year(current_period, t[2])), None)
        prior_qoq = next((t for t in dated[1:]
                          if is_quarter_over_quarter(current_period, t[2])), None)
        prior = prior_yoy or prior_qoq
        gtype = GrowthType.YOY if prior_yoy else (
            GrowthType.QOQ if prior_qoq else None)

        if prior is not None and prior[0].value is not None:
            current_fact.comparison_period = prior[2].label
            current_fact.comparison_value = prior[0].value
            current_fact.growth_type = gtype

        pct_reported = next((v for k, (v, _c) in reported.items()
                             if k.startswith("pct")), None)
        bps_reported = next((v for k, (v, _c) in reported.items()
                             if k.startswith("bps")), None)

        is_rate = current_fact.value_type in (
            ValueType.PERCENTAGE, ValueType.RATIO, ValueType.BPS)

        # Always recompute the change from the value pair, whether or not the
        # document stated one.  Storing both is what lets validation compare
        # them, and what keeps "reported" and "calculated" distinguishable.
        if (prior is not None and prior[0].value not in (None, 0)
                and current_fact.value is not None):
            prev = prior[0].value
            current_fact.change_absolute = round(current_fact.value - prev, 6)
            if not is_rate:
                current_fact.growth_calculated = round(
                    (current_fact.value - prev) / abs(prev) * 100.0, 4)

        if pct_reported is not None and not is_rate:
            current_fact.growth = pct_reported
            current_fact.growth_reported = True
            if current_fact.growth_type is None:
                gc = next((c for k, (_v, c) in reported.items()
                           if k.startswith("pct")), None)
                current_fact.growth_type = (gc.growth_type if gc and gc.growth_type
                                            else GrowthType.UNKNOWN)
        elif not is_rate and current_fact.growth_calculated is not None:
            current_fact.growth = current_fact.growth_calculated
            current_fact.growth_reported = False

        # A margin or ratio does not have a meaningful *relative* growth: a move
        # from 18.0% to 15.9% is -210 bps, not "-11.7%".  Record the change in
        # percentage points / basis points instead, and leave `growth` unset
        # unless the document itself reported one.
        if is_rate and prior is not None and prior[0].value is not None \
                and current_fact.value is not None:
            delta = current_fact.value - prior[0].value
            current_fact.meta["change_pp"] = round(delta, 4)
            if current_fact.value_type == ValueType.PERCENTAGE:
                current_fact.change_bps = round(delta * 100.0, 1)
                current_fact.meta["change_bps"] = current_fact.change_bps
            current_fact.meta["change_basis"] = "calculated"

        if bps_reported is not None:
            current_fact.meta["reported_change_bps"] = bps_reported
            current_fact.meta["change_basis"] = "reported"
        elif is_rate and pct_reported is not None:
            # a "% chg" column on a rate row is a percentage-point move
            current_fact.meta["reported_change_pp"] = pct_reported
            current_fact.meta["change_basis"] = "reported"


# ---------------------------------------------------------------- de-dup pass
def deduplicate(facts: list[FinancialFact]) -> list[FinancialFact]:
    """Collapse identical facts and use repetition as corroborating evidence.

    The same figure often appears in a highlights table and again in the
    statements.  Agreement across independent locations is real evidence, so the
    surviving fact's confidence is raised and the duplicate pages are recorded.
    Disagreement is *not* silently resolved: both are kept and flagged.
    """
    buckets: dict[tuple, list[FinancialFact]] = {}
    for f in facts:
        key = (f.canonical_metric, f.entity or "", f.period or "",
               f.unit, f.currency, f.value_type)
        buckets.setdefault(key, []).append(f)

    out: list[FinancialFact] = []
    for key, group in buckets.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        values: dict[float, list[FinancialFact]] = {}
        for f in group:
            values.setdefault(round(f.value, 6) if f.value is not None else float("nan"),
                              []).append(f)
        if len(values) == 1:
            group.sort(key=lambda f: f.confidence, reverse=True)
            winner = group[0]
            agreements = len(group) - 1
            if winner.confidence_breakdown is not None:
                sig = conf_mod.Signals(
                    ocr=winner.confidence_breakdown.ocr,
                    layout=winner.confidence_breakdown.layout,
                    structure=winner.confidence_breakdown.structure,
                    semantic=winner.confidence_breakdown.semantic,
                    corroborated=bool(winner.confidence_breakdown.corroboration),
                    duplicate_agreements=agreements,
                    notes=list(winner.confidence_breakdown.notes))
                bd = conf_mod.score(sig)
                winner.confidence = bd.overall
                winner.confidence_band = bd.band
                winner.confidence_breakdown = bd
            winner.meta["duplicate_count"] = len(group)
            winner.meta["duplicate_pages"] = sorted({f.source_page for f in group})
            out.append(winner)
        else:
            for f in group:
                f.requires_review = True
                f.meta["conflicting_duplicates"] = [
                    {"value": g.value, "page": g.source_page,
                     "confidence": g.confidence}
                    for g in group if g.fact_id != f.fact_id]
                out.append(f)
    return out
