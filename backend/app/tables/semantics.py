"""Semantic interpretation of a reconstructed table.

Structural reconstruction (``tables/structure.py``) answers *where* the rows and
columns are.  This module answers what they **mean**:

* what kind of table is this -- results, balance sheet, cash flow, operational?
* which reporting unit does it describe?
* which column is the current period, which is the prior-year comparison, which
  is the sequential quarter, and which is a stated change?
* what unit and currency govern its figures?
* which rows correspond to metrics the taxonomy knows?

The comparison relationships are derived from the **resolved periods**, not from
column order.  A table may print the prior year first, or omit the sequential
quarter entirely; reading position would get those wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction.taxonomy import match_metric
from app.models.enums import (
    ColumnRole, Currency, GrowthType, MagnitudeUnit, PeriodType, StatementType,
    TableType,
)
from app.models.schemas import (
    ColumnSemantics, ExtractedTable, TableSemantics,
)
from app.normalization.periods import FiscalCalendar, parse_period

log = get_logger("tables.semantics")

# Vocabulary that identifies a statement, matched against the table's title,
# caption and row labels.  Generic accounting language only -- nothing here is
# specific to any filer.
_BALANCE_SHEET_TERMS = re.compile(
    r"\bbalance sheet\b|\bstatement of assets and liabilities\b|"
    r"\btotal assets\b|\btotal equity\b|\bshareholders.? funds\b|"
    r"\bnon-?current (assets|liabilities)\b", re.I)
_CASH_FLOW_TERMS = re.compile(
    r"\bcash flow\b|\bcash flows?\b|\boperating activities\b|"
    r"\binvesting activities\b|\bfinancing activities\b", re.I)
_INCOME_TERMS = re.compile(
    r"\bprofit and loss\b|\bstatement of profit\b|\bincome statement\b|"
    r"\brevenue from operations\b|\bprofit before tax\b|\btotal income\b|"
    r"\bearnings per\b", re.I)
_EQUITY_TERMS = re.compile(r"\bchanges in equity\b|\bstatement of equity\b", re.I)
_SEGMENT_TERMS = re.compile(
    r"\bsegment (revenue|results?|information|wise)\b|\bbusiness segment\b", re.I)
_RATIO_TERMS = re.compile(r"\bratios?\b|\bformulae?\b|\bcoverage ratio\b", re.I)
_OPERATIONAL_TERMS = re.compile(
    r"\boperational (update|metrics|performance)\b|\bkey (operating|operational)\b|"
    r"\buom\b|\bunit of measure\b", re.I)
_PRICE_TERMS = re.compile(
    r"\bcracks?\b|\bprice(s|ing)?\b|\bdelta\b|\brealisations?\b|\brealizations?\b|"
    r"\b\$/(bbl|mt|mmbtu)\b", re.I)

_CHANGE_HEADER = re.compile(
    r"%\s*(chg|change|growth)|y-?o-?y|q-?o-?q|yoy|qoq|\bgrowth\b|\bchange\b|"
    r"\bvar\b|\bbps\b", re.I)
_SHARE_HEADER = re.compile(r"%\s*(of|share|mix|contribution)|\bshare\b", re.I)
_UOM_HEADER = re.compile(r"^\s*(uom|unit|units|u\.o\.m\.?)\s*$", re.I)
_SERIAL_HEADER = re.compile(r"^\s*(sr\.?|s\.?\s*no\.?|no\.?|#|sl\.?)[\s.]*$", re.I)
_NOTE_HEADER = re.compile(r"^\s*(notes?|remarks?|comments?)\s*$", re.I)


def classify_table(table: ExtractedTable, row_labels: list[str]) -> tuple[TableType,
                                                                         StatementType]:
    """Decide what kind of table this is from its own text.

    Row labels carry more signal than the title, because a caption may be a
    section banner while the rows name the actual line items.
    """
    corpus = " ".join([table.title or "", table.caption or "", *row_labels])

    if _BALANCE_SHEET_TERMS.search(corpus):
        return TableType.BALANCE_SHEET, StatementType.BALANCE_SHEET
    if _CASH_FLOW_TERMS.search(corpus):
        return TableType.CASH_FLOW, StatementType.CASH_FLOW
    if _EQUITY_TERMS.search(corpus):
        return TableType.OTHER, StatementType.EQUITY
    if _SEGMENT_TERMS.search(corpus):
        return TableType.SEGMENT_RESULTS, StatementType.SEGMENT
    if _RATIO_TERMS.search(corpus) and not _INCOME_TERMS.search(corpus):
        return TableType.RATIO_TABLE, StatementType.RATIO
    if _PRICE_TERMS.search(corpus):
        return TableType.PRICE_TABLE, StatementType.UNKNOWN
    if _OPERATIONAL_TERMS.search(corpus):
        return TableType.OPERATIONAL_METRICS, StatementType.OPERATIONAL
    if _INCOME_TERMS.search(corpus):
        return TableType.INCOME_STATEMENT, StatementType.INCOME_STATEMENT

    # No statement vocabulary: fall back on what the rows actually are.
    statements = [m.definition.statement for m in
                  (match_metric(l) for l in row_labels) if m is not None]
    if statements:
        dominant = max(set(statements), key=statements.count)
        if statements.count(dominant) >= max(2, len(statements) // 3):
            mapping = {
                StatementType.BALANCE_SHEET: TableType.BALANCE_SHEET,
                StatementType.CASH_FLOW: TableType.CASH_FLOW,
                StatementType.OPERATIONAL: TableType.OPERATIONAL_METRICS,
                StatementType.RATIO: TableType.RATIO_TABLE,
                StatementType.SEGMENT: TableType.SEGMENT_RESULTS,
            }
            return mapping.get(dominant, TableType.FINANCIAL_RESULTS), dominant

    if any(c.normalized_period for c in table.columns):
        return TableType.FINANCIAL_RESULTS, StatementType.INCOME_STATEMENT
    return TableType.OTHER, StatementType.UNKNOWN


def _column_role(column, label_indexes: set[int]) -> ColumnRole:
    header = column.header or ""
    if _SERIAL_HEADER.match(header):
        return ColumnRole.SERIAL
    if _UOM_HEADER.match(header):
        return ColumnRole.UNIT_OF_MEASURE
    if _NOTE_HEADER.match(header):
        return ColumnRole.NOTE
    if column.is_growth_column or _CHANGE_HEADER.search(header):
        return ColumnRole.CHANGE
    if _SHARE_HEADER.search(header):
        return ColumnRole.SHARE
    if column.normalized_period:
        return ColumnRole.PERIOD_VALUE
    if column.index in label_indexes:
        return ColumnRole.LABEL
    if column.numeric_ratio >= 0.5:
        return ColumnRole.PERIOD_VALUE
    return ColumnRole.LABEL if column.index <= 1 else ColumnRole.UNKNOWN


def _period_sort_key(sem: ColumnSemantics) -> tuple:
    return (sem.fiscal_year or 0, sem.quarter or 0)


def interpret(table: ExtractedTable, *, calendar: FiscalCalendar,
              entity_id: str | None = None, entity_name: str | None = None,
              label_columns: set[int] | None = None) -> TableSemantics:
    """Build the semantic layer for one reconstructed table."""
    label_columns = label_columns or set()

    header_rows = {c.row for c in table.cells
                   if str(c.role) in ("column_header", "corner")}
    n_header = (max(header_rows) + 1) if header_rows else 0
    row_labels: list[str] = []
    row_metrics: dict[str, str] = {}
    for cell in table.cells:
        if cell.row < n_header or cell.col not in (label_columns or {0}):
            continue
        text = cell.text.strip()
        if not text or cell.is_numeric:
            continue
        row_labels.append(text)
        match = match_metric(text)
        if match is not None:
            row_metrics[str(cell.row)] = match.key

    table_type, statement = classify_table(table, row_labels)

    columns: list[ColumnSemantics] = []
    for column in table.columns:
        role = _column_role(column, label_columns)
        period = (parse_period(column.header, calendar=calendar)
                  if column.normalized_period else None)
        columns.append(ColumnSemantics(
            index=column.index,
            header=column.header,
            role=role,
            period=column.normalized_period,
            period_type=(PeriodType(period.period_type) if period else None),
            fiscal_year=(period.fiscal_year if period else None),
            quarter=(period.quarter if period else None),
            growth_type=(GrowthType(column.growth_type)
                         if column.growth_type else None),
            confidence=round(0.5 + 0.4 * column.numeric_ratio, 3),
        ))

    # ---- comparison relationships, derived from periods not from position ---
    dated = [c for c in columns
             if c.role == ColumnRole.PERIOD_VALUE and c.fiscal_year]
    dated.sort(key=_period_sort_key, reverse=True)

    current = dated[0] if dated else None
    comparison: ColumnSemantics | None = None
    sequential: ColumnSemantics | None = None
    if current is not None:
        current.is_current = True
        cur_key = (current.fiscal_year or 0) * 4 + (current.quarter or 0)
        for other in dated[1:]:
            other_key = (other.fiscal_year or 0) * 4 + (other.quarter or 0)
            gap = cur_key - other_key
            if current.quarter and other.quarter:
                if gap == 4 and comparison is None:
                    comparison = other
                elif gap == 1 and sequential is None:
                    sequential = other
            elif not current.quarter and not other.quarter and gap == 4:
                comparison = comparison or other

    # attach the change column to what it is a change *of*
    for column in columns:
        if column.role != ColumnRole.CHANGE:
            continue
        if column.growth_type == GrowthType.YOY and comparison is not None:
            column.comparison_of = comparison.index
        elif column.growth_type == GrowthType.QOQ and sequential is not None:
            column.comparison_of = sequential.index
        elif comparison is not None:
            column.comparison_of = comparison.index

    notes: list[str] = []
    if current is None:
        notes.append("No column resolved to a reporting period; values from this "
                     "table carry no period attribution.")
    if comparison is None and current is not None:
        notes.append("No prior-year column found, so year-on-year change cannot "
                     "be computed from this table alone.")

    periods = [c.period for c in dated if c.period]
    confidence = round(min(0.97, 0.4 + 0.25 * bool(periods)
                           + 0.2 * bool(row_metrics)
                           + 0.15 * (table.confidence or 0)), 3)

    return TableSemantics(
        table_id=table.id,
        document_id=table.document_id,
        page_number=table.page_number,
        table_type=table_type,
        statement=statement,
        title=table.title or "",
        entity_id=entity_id,
        entity_name=entity_name,
        periods=periods,
        current_period=current.period if current else None,
        comparison_period=comparison.period if comparison else None,
        sequential_period=sequential.period if sequential else None,
        unit=MagnitudeUnit(table.unit_hint) if table.unit_hint else None,
        currency=Currency(table.currency_hint) if table.currency_hint else None,
        columns=columns,
        row_metrics=row_metrics,
        confidence=confidence,
        notes=notes,
    )


def semantics_id(document_id: str, table_id: str) -> str:
    return stable_id("tsem", document_id, table_id)
