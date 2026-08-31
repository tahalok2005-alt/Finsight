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
from dataclasses import dataclass

from app.config.settings import settings
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import confidence as conf_mod
from app.extraction.taxonomy import MetricMatch, match_metric
from app.models.enums import (
    CellRole, Currency, FactKind, GrowthType, MagnitudeUnit, StatementNature,
    ValueType,
)
from app.models.schemas import (
    BBoxModel, ExtractedTable, FinancialFact, NormalizedPeriod, Provenance,
    TableCell, TableColumn,
)
from app.normalization.numbers import (
    MAGNITUDE_FACTORS, detect_value_type, normalize_number,
)
from app.normalization.periods import (
    FiscalCalendar, is_quarter_over_quarter, is_year_over_year, parse_period,
    period_distance,
)

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
    this purpose even though the serial column is numeric.
    """
    out: list[int] = []
    for col in table.columns:
        data = [c for c in table.cells if c.col == col.index and c.row >= n_header]
        if not data:
            continue
        numeric_ratio = sum(1 for c in data if c.is_numeric) / len(data)
        is_serial = (col.index == 0 and numeric_ratio > 0.7
                     and all((c.numeric_value or 0) < 200 for c in data if c.is_numeric))
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
    if col.normalized_period:
        return parse_period(col.header, calendar=calendar)
    return None


def _uom_columns(table: ExtractedTable, label_cols: list[int]) -> set[int]:
    return {c.index for c in table.columns
            if c.index in label_cols and _UOM_HEADER_RE.match(c.header or "")}


def _uom_text(table: ExtractedTable, row: int, uom_cols: set[int]) -> str:
    for col in sorted(uom_cols):
        c = _cell_at(table, row, col)
        if c and c.text.strip():
            return c.text
    return ""


def extract_from_table(table: ExtractedTable, ctx: TableFactContext,
                       ) -> list[FinancialFact]:
    """Turn one reconstructed table into facts."""
    if not table.cells or not table.columns:
        return []

    n_header = _header_row_count(table)
    label_cols = _label_columns(table, n_header)
    uom_cols = _uom_columns(table, label_cols)
    data_cols = [c for c in table.columns if c.index not in label_cols]
    if not data_cols:
        return []

    period_cols = [(c, _period_for(c, ctx.calendar)) for c in data_cols
                   if not c.is_growth_column]
    period_cols = [(c, p) for c, p in period_cols if p is not None]
    growth_cols = [c for c in data_cols if c.is_growth_column]
    # Columns with no parseable period still carry data; keep them so nothing is
    # dropped, but they will produce facts with period=None.
    plain_cols = [c for c in data_cols
                  if not c.is_growth_column
                  and c.index not in {pc.index for pc, _ in period_cols}]

    table_unit = table.unit_hint or ctx.default_unit
    table_currency = table.currency_hint or ctx.default_currency
    context_text = f"{table.title} {table.caption}".strip()

    facts: list[FinancialFact] = []
    data_rows = sorted({c.row for c in table.cells if c.row >= n_header})

    for row in data_rows:
        label, anchor = _row_label(table, row, label_cols, uom_cols)
        if not label:
            continue
        match = match_metric(label)
        if match is None:
            continue
        uom = _uom_text(table, row, uom_cols)
        # Unit resolution is strictly layered, most specific first:
        #   1. the row label and its own unit-of-measure cell
        #   2. the table's declared unit (already resolved from an explicit
        #      "(Rs in crore)" declaration by the table builder)
        # The surrounding caption prose is deliberately NOT consulted here.  A
        # window of narrative above a rupee table routinely mentions "533
        # million subscribers", and letting that through reported the whole
        # table in millions.
        row_context = f"{label} {uom}".strip()
        row_declares_unit = bool(uom.strip())

        # ---- period columns ------------------------------------------------
        row_facts: list[tuple[FinancialFact, TableColumn, NormalizedPeriod | None]] = []
        for col, period in period_cols + [(c, None) for c in plain_cols]:
            cell = _cell_at(table, row, col.index)
            if cell is None or not cell.text.strip():
                continue
            fact = _fact_from_cell(
                cell=cell, table=table, ctx=ctx, match=match, label=label,
                column=col, period=period, row_context=row_context,
                table_unit=table_unit, table_currency=table_currency,
                row_declares_unit=row_declares_unit)
            if fact is not None:
                facts.append(fact)
                row_facts.append((fact, col, period))

        _link_growth(row_facts, table, row, growth_cols, ctx)

    return facts


def _fact_from_cell(*, cell: TableCell, table: ExtractedTable,
                    ctx: TableFactContext, match: MetricMatch, label: str,
                    column: TableColumn, period: NormalizedPeriod | None,
                    row_context: str, table_unit: MagnitudeUnit | None,
                    table_currency: Currency | None,
                    row_declares_unit: bool = False) -> FinancialFact | None:
    expected = match.definition.value_type
    # A metric declared as a percentage or ratio must not inherit the table's
    # monetary unit -- that is how "EBITDA margin 15.9" becomes "15.9 crore".
    non_monetary = expected in (
        ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT)
    # The table-level unit is a *fallback*: it does not apply to a
    # non-monetary metric, nor to a row that stated its own unit.
    unit_default = None if (non_monetary or row_declares_unit) else table_unit
    cur_default = None if (non_monetary or row_declares_unit) else table_currency

    num = normalize_number(cell.text, context=row_context,
                           default_currency=cur_default,
                           default_magnitude=unit_default,
                           expected_type=expected if expected != ValueType.UNKNOWN
                           else None)
    if num is None:
        return None

    value_type = num.value_type
    if expected != ValueType.UNKNOWN and value_type == ValueType.UNKNOWN:
        value_type = expected
    if expected in (ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT):
        value_type = expected

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
        entity=ctx.entity, kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
        value=num.value, raw_value=cell.text, value_type=value_type,
        currency=num.currency, unit=num.magnitude, base_value=base,
        period=period.label if period else None, period_normalized=period,
        confidence=breakdown.overall, confidence_band=breakdown.band,
        confidence_breakdown=breakdown,
        source_page=table.page_number, provenance=prov,
        original_text=cell.text, context_text=table.title or table.caption[:200],
        meta={"table_id": table.id, "row": cell.row, "col": cell.col,
              "column_header": column.header,
              "metric_match_method": match.method})


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

        if pct_reported is not None and not is_rate:
            current_fact.growth = pct_reported
            current_fact.growth_reported = True
            if current_fact.growth_type is None:
                gc = next((c for k, (_v, c) in reported.items()
                           if k.startswith("pct")), None)
                current_fact.growth_type = (gc.growth_type if gc and gc.growth_type
                                            else GrowthType.UNKNOWN)
        elif (not is_rate and prior is not None and prior[0].value not in (None, 0)
              and current_fact.value is not None):
            prev = prior[0].value
            current_fact.growth = round(
                (current_fact.value - prev) / abs(prev) * 100.0, 4)
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
                current_fact.meta["change_bps"] = round(delta * 100.0, 1)
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
