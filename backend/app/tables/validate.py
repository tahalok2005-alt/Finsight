"""Checking a reconstructed table against itself, before it becomes facts.

Every check here compares the table with something it already contains, so a
failure means the reconstruction is internally inconsistent -- not that the
document is unusual. That distinction is what makes the findings worth acting
on: a table can be strange and still be read correctly, but it cannot be read
correctly and contradict itself.

Nothing here alters values or drops cells. Findings are recorded on the table
and reach a reader through the facts' review state, because a reconstruction we
cannot vouch for must not produce figures that look settled.

What is deliberately *not* checked: whether totals add up. Component rows in a
real statement routinely exclude items the total includes -- Groww's "Total
expenses" covers five named lines and the statement's own sub-headings sit
between them -- so an arithmetic check against the visible rows fires on
perfectly good tables. Cross-footing belongs to the validation engine, which
has the metric taxonomy to know which rows are components of which total.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.core.logging import get_logger

log = get_logger("tables.validate")

#: A column holding fewer than this share of the rows other data columns hold
#: is suspicious: either it is not really a column or the rows are misaligned.
_SPARSE_COLUMN_RATIO = 0.25
#: How much of a value cell must sit inside its column's x range.
_MIN_CELL_CONTAINMENT = 0.5


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    message: str
    #: True when the finding means figures may carry the wrong period.
    affects_period: bool = False

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "affects_period": self.affects_period}


@dataclass
class TableReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def period_is_unsafe(self) -> bool:
        """Whether any finding puts column-to-period mapping in doubt."""
        return any(f.affects_period for f in self.findings)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "period_is_unsafe": self.period_is_unsafe,
                "findings": [f.as_dict() for f in self.findings]}


def _data_columns(table):
    return [c for c in table.columns
            if c.index != 0 and not c.is_growth_column]


def validate_table(table, *, n_header: int) -> TableReport:
    """Check one reconstructed table for internal inconsistency."""
    report = TableReport()
    data_cols = _data_columns(table)
    if not data_cols:
        return report

    body = [c for c in table.cells if c.row >= n_header]
    if not body:
        return report

    # ---- 1. do the value cells sit inside the columns they were filed under?
    bounds = {c.index: (c.x_start, c.x_end) for c in table.columns}
    stray = 0
    for cell in body:
        span = bounds.get(cell.col)
        if span is None or not cell.text.strip():
            continue
        x0, x1 = span
        box = cell.bbox
        overlap = min(box.x + box.width, x1) - max(box.x, x0)
        if overlap < _MIN_CELL_CONTAINMENT * max(1.0, box.width):
            stray += 1
    if stray:
        share = stray / max(1, len([c for c in body if c.text.strip()]))
        if share > 0.1:
            report.findings.append(Finding(
                "cells_outside_their_columns",
                f"{stray} of the table's filled cells sit mostly outside the "
                f"column they were assigned to, so the column boundaries and "
                f"the figures disagree.",
                affects_period=True))

    # ---- 2. is one dated column almost empty while its neighbours are full?
    filled = Counter(c.col for c in body if c.text.strip())
    counts = [filled.get(c.index, 0) for c in data_cols]
    if len(counts) >= 2 and max(counts) > 0:
        busiest = max(counts)
        sparse = [c.index for c in data_cols
                  if filled.get(c.index, 0) < _SPARSE_COLUMN_RATIO * busiest
                  and c.period_resolved is not None]
        if sparse:
            report.findings.append(Finding(
                "dated_column_is_nearly_empty",
                f"Column(s) {sparse} carry a reporting period but almost no "
                f"figures, which usually means the headings and the data are "
                f"offset from one another.",
                affects_period=True))

    # ---- 3. two columns resolving to the identical period ------------------
    # Legitimate when the spans differ -- a quarter and a year can both end on
    # 31 March -- so the label, which carries the span, is what must be unique.
    labels = [c.period_resolved.label for c in data_cols
              if c.period_resolved is not None]
    duplicated = [label for label, n in Counter(labels).items() if n > 1]
    if duplicated:
        report.findings.append(Finding(
            "repeated_period",
            f"More than one column resolved to {duplicated}. Two columns of "
            f"one statement do not normally report the same period, so at "
            f"least one heading was read wrongly.",
            affects_period=True))

    # ---- 4. a table whose columns are dated but whose rows are not aligned --
    dated = [c for c in data_cols if c.period_resolved is not None]
    if dated and len(dated) < len(data_cols):
        undated = [c.index for c in data_cols if c.period_resolved is None
                   and filled.get(c.index, 0) > _SPARSE_COLUMN_RATIO * max(counts)]
        if undated:
            report.findings.append(Finding(
                "column_without_a_period",
                f"Column(s) {undated} hold figures but no period could be read "
                f"from their heading, while their neighbours are dated.",
                affects_period=False))

    if report.findings:
        log.info("tables.validation", table_id=table.id,
                 page_number=table.page_number,
                 codes=[f.code for f in report.findings])
    return report
