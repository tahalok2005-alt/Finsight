"""What kind of table this is, and therefore what it owes the reader.

The reason this matters is narrow and important: **whether a missing period is
a defect or a fact about the document.**

A segment table whose columns are "Retail", "Wholesale", "Treasury" has no
periods to find, and flagging its figures for review would bury a reader in
warnings about a table that is behaving perfectly. A statement of profit and
loss whose columns are dates has a period for every figure, and a figure that
arrives without one is a defect however good the number looks.

The same value of `period = None` therefore means opposite things in the two
tables, and nothing downstream could tell them apart. This module supplies the
distinction from the table's own structure.

The classification is evidence-driven and refuses when the evidence is thin:
`UNCLASSIFIED` is a real answer and is treated as "period expected but not
mandatory", which is the conservative reading.
"""
from __future__ import annotations

import re
from enum import StrEnum

from app.core.logging import get_logger

log = get_logger("tables.shape")


class TableShape(StrEnum):
    """What the columns of this table are."""

    #: Columns are instants: "As at 31 March 2026". Balance sheets.
    POINT_IN_TIME = "point_in_time"
    #: Columns are spans: "Quarter ended 30 June 2026". P&L, cash flow.
    PERIOD_FLOW = "period_flow"
    #: Columns are dated but the span is not stated: "Jun-26", "Q1 FY27".
    #: KPI and operating tables. Still periodized.
    PERIODIZED_KPI = "periodized_kpi"
    #: Columns are not periods at all: segments, geographies, currencies.
    NON_PERIODIC = "non_periodic"
    #: Not enough evidence to say.
    UNCLASSIFIED = "unclassified"

    @property
    def is_periodized(self) -> bool:
        """Whether a figure from this table is expected to carry a period."""
        return self in (self.POINT_IN_TIME, self.PERIOD_FLOW,
                        self.PERIODIZED_KPI)


#: Titles and heads that name a statement whose columns are instants.
_POINT_IN_TIME = re.compile(
    r"balance\s*sheet|statement\s+of\s+(?:financial\s+position|assets\s+and\s+"
    r"liabilities)|as\s*[-\s]*at\b|as\s+of\b", re.I)
#: Titles and heads that name a statement whose columns are spans.
_PERIOD_FLOW = re.compile(
    r"profit\s+and\s+loss|income\s+statement|statement\s+of\s+(?:profit|income|"
    r"operations|comprehensive\s+income|cash\s*flows?)|cash\s*flow|"
    r"(?:quarter|year|period|month)s?\s*ended|ended\b", re.I)
#: Column heads that are plainly not periods.
_NON_PERIODIC_HEAD = re.compile(
    r"^\s*(?:segment|division|business|geograph|region|countr|currenc|"
    r"particulars?|description|total|share|%|change)\b", re.I)


def classify_table(table) -> TableShape:
    """Decide what a reconstructed table's columns are.

    Column evidence outranks the title, because the title describes the section
    and the columns describe the figures. A "Statement of Profit and Loss" page
    can carry a small balance-sheet extract at the bottom, and it is the column
    heads that say so.
    """
    data_cols = [c for c in table.columns
                 if c.index != 0 and not c.is_growth_column]
    if not data_cols:
        return TableShape.UNCLASSIFIED

    resolved = [c for c in data_cols if c.period_resolved is not None]
    heads = " | ".join((c.header or "") for c in data_cols)
    title_text = f"{table.title or ''} {table.caption or ''}"

    # ---- strongest evidence: the resolved periods themselves ---------------
    if resolved:
        kinds = {str(getattr(c.period_resolved, "period_type", "") or "")
                 for c in resolved}
        instants = {"point_in_time", "instant", "as_at"}
        if kinds and kinds <= instants:
            return TableShape.POINT_IN_TIME
        spans = kinds - instants
        if spans:
            # Mixed is a period-flow statement carrying a couple of
            # balance-sheet lines, which Groww's does -- paid-up capital and
            # other equity sit under the year column of a P&L. The spans win.
            return TableShape.PERIOD_FLOW

    # ---- next: what the heads say ------------------------------------------
    if _POINT_IN_TIME.search(heads) and not _PERIOD_FLOW.search(heads):
        return TableShape.POINT_IN_TIME
    if _PERIOD_FLOW.search(heads):
        return TableShape.PERIOD_FLOW

    # ---- a head that is plainly a category, not a date ----------------------
    named = [c for c in data_cols if (c.header or "").strip()]
    if named and all(_NON_PERIODIC_HEAD.match(c.header or "") for c in named):
        return TableShape.NON_PERIODIC

    # ---- last: the title, which describes the section ----------------------
    if _POINT_IN_TIME.search(title_text) and not _PERIOD_FLOW.search(title_text):
        return TableShape.POINT_IN_TIME
    if _PERIOD_FLOW.search(title_text):
        return TableShape.PERIOD_FLOW

    # Dated heads with no stated span: a KPI table ("Jun-26 | Mar-26").
    if resolved:
        return TableShape.PERIODIZED_KPI

    return TableShape.UNCLASSIFIED


def period_is_mandatory(shape: TableShape) -> bool:
    """Whether a figure from a table of this shape must carry a period.

    `UNCLASSIFIED` deliberately answers False. Demanding a period from a table
    we could not classify would flag every unusual layout in the corpus, and a
    warning that fires everywhere is read nowhere.
    """
    return shape.is_periodized
