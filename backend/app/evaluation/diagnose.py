"""Why a golden fact was not extracted.

Coverage is only actionable if each miss can be attributed to the stage that
lost it. "79 facts missing" is a number; "43 lost at the taxonomy, 12 at the row
label, 4 at column geometry" is a plan.

The classification walks the same path the extractor walks, in order, and stops
at the first stage that cannot account for the fact. That ordering matters: a
row whose label the taxonomy does not know will also have no period, and
reporting it as a period failure would send the next phase to the wrong module.

Nothing here changes behaviour. It reads the artefacts a run already produced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger
from app.extraction.taxonomy import match_metric
from app.normalization.numbers import looks_numeric, parse_number

log = get_logger("evaluation.diagnose")


class MissCause(StrEnum):
    """Root-cause buckets, lettered as the brief lists them."""

    TAXONOMY = "A. taxonomy/metric vocabulary"
    ROW_RECOGNITION = "B. table row recognition"
    LABEL_PARSING = "C. label parsing"
    NUMBER_PARSING = "D. number parsing"
    COLUMN_GEOMETRY = "E. column geometry"
    PERIOD_RESOLUTION = "F. period resolution"
    ENTITY_BASIS = "G. entity/basis"
    UNIT_CURRENCY = "H. unit/currency"
    FRAGMENTATION = "I. table fragmentation"
    STRATEGY_NOT_INVOKED = "J. extraction strategy not invoked"
    SAFETY_GATE = "K. confidence/safety gate rejected it"
    RECONCILIATION = "L. reconciliation removed it"
    DUPLICATE = "M. duplicate/corroboration handling"
    UNREADABLE = "N. genuinely unreadable/ambiguous source"


@dataclass
class Miss:
    golden: str
    row: str
    column: str
    value: float
    cause: MissCause
    detail: str

    def as_dict(self) -> dict:
        return {"golden": self.golden, "row": self.row, "column": self.column,
                "value": self.value, "cause": str(self.cause),
                "detail": self.detail}


@dataclass
class Diagnosis:
    misses: list[Miss] = field(default_factory=list)

    def by_cause(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for miss in self.misses:
            out[str(miss.cause)] = out.get(str(miss.cause), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def as_dict(self) -> dict:
        return {"total": len(self.misses), "by_cause": self.by_cause(),
                "misses": [m.as_dict() for m in self.misses]}


def _norm(text: str) -> str:
    keep = [c for c in (text or "").lower() if c.isalnum() or c.isspace()]
    return " ".join("".join(keep).split())


def _cells_of(tables, page: int):
    for table in tables:
        if table.page_number != page:
            continue
        for cell in table.cells:
            yield table, cell


def _row_cells(table, row: int):
    return [c for c in table.cells if c.row == row]


def diagnose_miss(golden_row: str, column_id: str, want_value: float,
                  *, golden_table, tables, facts, page: int) -> Miss:
    """Attribute one missing golden fact to the stage that lost it."""
    described = f"{golden_row} [{column_id}] = {want_value}"

    def miss(cause: MissCause, detail: str) -> Miss:
        return Miss(described, golden_row, column_id, want_value, cause, detail)

    page_tables = [t for t in tables if t.page_number == page]
    if not page_tables:
        return miss(MissCause.FRAGMENTATION,
                    f"no table was reconstructed on page {page}")

    # ---- 1. is the value present anywhere as a cell? -----------------------
    wanted = _norm(golden_row)
    value_cells = []
    for table, cell in _cells_of(tables, page):
        if cell.numeric_value is None:
            continue
        scale = max(abs(cell.numeric_value), abs(want_value), 1.0)
        if abs(cell.numeric_value - want_value) <= 0.005 * scale:
            value_cells.append((table, cell))

    if not value_cells:
        # Is the digit string on the page at all, even unparsed?
        raw_hits = [
            (t, c) for t, c in _cells_of(tables, page)
            if c.text and looks_numeric(c.text)
            and _norm(c.text).replace(" ", "").strip("()")
            .replace(",", "").replace(".", "")
            == f"{want_value:.2f}".replace(".", "").lstrip("0")]
        if raw_hits:
            table, cell = raw_hits[0]
            parsed = parse_number(cell.text)
            return miss(MissCause.NUMBER_PARSING,
                        f"cell {cell.text!r} parsed to "
                        f"{parsed[0] if parsed else None}")
        return miss(MissCause.ROW_RECOGNITION,
                    "the value does not appear as a numeric cell in any "
                    "reconstructed table on this page")

    # ---- 2. the value is in a cell. is its row label recognised? -----------
    table, cell = value_cells[0]
    label_parts = [c.text for c in _row_cells(table, cell.row)
                   if c.col < cell.col and c.text.strip()
                   and not c.is_numeric]
    label = " ".join(label_parts).strip()
    if not label:
        return miss(MissCause.LABEL_PARSING,
                    f"row {cell.row} of the table holds the value but no "
                    f"label was assembled for it")

    match = match_metric(label)
    if match is None:
        printed = match_metric(golden_row)
        if printed is not None:
            return miss(MissCause.LABEL_PARSING,
                        f"label read as {label!r}; the printed label "
                        f"{golden_row!r} does match "
                        f"({printed.key}) -- recognition damaged it")
        return miss(MissCause.TAXONOMY,
                    f"no taxonomy entry for {label!r}")

    # ---- 3. the metric is known. did a fact reach the output? --------------
    produced = [f for f in facts
                if f.provenance and f.provenance.page_number == page
                and f.value is not None
                and abs(f.value - want_value)
                <= 0.005 * max(abs(want_value), 1.0)]
    if not produced:
        return miss(MissCause.STRATEGY_NOT_INVOKED,
                    f"row label {label!r} matched {match.key} and the cell "
                    f"holds the value, but no fact was produced")

    # ---- 4. a fact exists. why did it not match the golden fact? -----------
    fact = produced[0]
    if _norm(fact.metric or "") != wanted and not (
            wanted.startswith(_norm(fact.metric or ""))
            or _norm(fact.metric or "").startswith(wanted[:18])):
        return miss(MissCause.LABEL_PARSING,
                    f"fact carries metric {fact.metric!r}, golden expects "
                    f"{golden_row!r}")

    column = golden_table.columns.get(column_id)
    end = getattr(fact.period_normalized, "end_date", None)
    if column is not None and end != column.period_end:
        return miss(MissCause.PERIOD_RESOLUTION,
                    f"fact resolved to {end or 'no period'}, golden column "
                    f"{column_id} is {column.period_end}")

    return miss(MissCause.DUPLICATE,
                "a matching fact exists but was paired with another golden "
                "row by the matcher")


def diagnose(golden_table, tables, facts, misses: list[str]) -> Diagnosis:
    """Classify every miss reported for one golden table."""
    out = Diagnosis()
    by_key = {}
    for want in golden_table.facts:
        by_key[want.describe] = want
    for described in misses:
        want = by_key.get(described)
        if want is None:
            continue
        out.misses.append(diagnose_miss(
            want.row, want.column.id, want.value,
            golden_table=golden_table, tables=tables, facts=facts,
            page=golden_table.page))
    log.info("evaluation.diagnose", doc_key=golden_table.doc_key,
             page=golden_table.page, misses=len(out.misses),
             by_cause=out.by_cause())
    return out
