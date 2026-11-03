"""Turning KPI cards into canonical financial facts.

Everything a fact needs -- the metric, the number, the unit, the period, the
entity, the confidence, the provenance -- is decided here by the same code the
table extractor uses. A card is a different *shape* of evidence, not a
different kind of financial figure, and it must not acquire its own parallel
notion of what a metric or a period is.

The one thing genuinely new is how a period reaches a card, and it is the part
this module is most careful about. See ``_period_for_card``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import confidence as conf_mod
from app.extraction.taxonomy import match_metric
from app.models.enums import (
    Currency, DerivationStatus, EntityType, FactKind, MagnitudeUnit,
    PeriodBasis, StatementNature, ValueType,
)
from app.models.schemas import (
    BBoxModel, FinancialFact, NormalizedPeriod, Provenance,
)
from app.normalization.numbers import detect_magnitude, normalize_number
from app.normalization.periods import is_date_like
from app.normalization.periods import FiscalCalendar, parse_period
from app.spatial.cards import CardCandidate, GridFinding
from app.spatial.rows import RowCandidate, RowFinding
from app.spatial.transposed import TransposedCandidate, TransposedFinding

log = get_logger("spatial.extract")

#: The strategy recorded on every fact a tile produced.
STRATEGY = "spatial_kpi"
#: ... and on every fact read from a horizontal metric row.
ROW_STRATEGY = "spatial_row"
#: ... and on every fact read from a drawn grid of figures.
GRID_STRATEGY = "spatial_grid"

#: "2.5x", "2.5X" -- a multiple, which presentations use for growth since some
#: baseline. It is a number beside a metric name without being that metric's
#: value, and a tile has no column head to say so.
_MULTIPLE_NOTATION = re.compile(r"\d\s*[xX]\s*$")


@dataclass(slots=True)
class CardContext:
    """What a page can tell a card that the card does not say itself."""
    document_id: str
    calendar: FiscalCalendar
    entity: str | None = None
    entity_id: str | None = None
    entity_type: EntityType = EntityType.UNKNOWN
    parent_entity_id: str | None = None
    #: Currency and magnitude the document declared, used only as a fallback
    #: and only when the card's own text carries neither.
    default_currency: Currency | None = None
    default_unit: MagnitudeUnit | None = None
    #: A period the page states once for everything on it -- a banner reading
    #: "Q1 FY27 performance". Inherited, never stated.
    page_period: NormalizedPeriod | None = None
    page_period_source: str = ""
    page_image_url: str | None = None


@dataclass(slots=True)
class CardExtraction:
    """Accepted facts and the reasons the rest were not."""
    facts: list[FinancialFact]
    rejected: dict[str, int]


def _period_for_card(card: CardCandidate, ctx: CardContext,
                     ) -> tuple[NormalizedPeriod | None, str]:
    """The period a card's figure belongs to, and where it came from.

    Two sources, in strict order of evidence:

    1. **Inside the tile.** "(Q1-FY27)" printed within the card's own bounding
       region belongs to that card's figure and nothing else. This is the only
       route to a period the document can be said to have *stated* for this
       value.
    2. **The page said it once.** A banner reading "Q1 FY27 performance" above
       a row of tiles is real evidence, but it is evidence about the page, not
       about this tile. It is taken, and marked ``inherited`` with the text it
       came from, so a reader can see the difference.

    Proximity is never enough on its own. A period sitting near a tile but
    outside it -- the neighbouring card's label, a stray axis tick -- is not
    used at all. That is the rule the misaligned-header defect taught: a date
    that arrives confidently and wrongly is worse than no date.
    """
    if card.period is not None:
        resolved = parse_period(card.period.text, calendar=ctx.calendar)
        if resolved is not None:
            return resolved, "inside the tile"
    if ctx.page_period is not None:
        inherited = ctx.page_period.model_copy(deep=True)
        inherited.basis = PeriodBasis.INHERITED
        return inherited, ctx.page_period_source
    return None, ""


@dataclass(slots=True)
class Reading:
    """A label and figure the acceptance rules let through."""
    match: object
    value: float
    base_value: float | None
    value_type: ValueType
    currency: Currency
    magnitude: MagnitudeUnit
    currency_inferred: bool


def read_label_and_value(label_text: str, value_text: str, ctx: CardContext,
                         ) -> Reading | str:
    """Decide whether a label and the figure beside it are a financial fact.

    Returns the reading, or the name of the rule that refused it. Shared
    verbatim between the two spatial readers: a figure arranged vertically and
    a figure arranged horizontally are the same kind of evidence about the same
    kind of quantity, and letting them acquire separate ideas of what a metric,
    a scale or a percentage is would be how the two start disagreeing.
    """
    match = match_metric(label_text)
    if match is None:
        # The taxonomy does not recognise this label. Inventing a metric from
        # an unrecognised label is how a slide's decoration becomes a
        # financial figure.
        return "label_not_a_known_metric"

    if is_date_like(value_text):
        # "30 Jun 26" under a tile's label is the date the tile is stated at,
        # not an amount of 30 (Phase 22).
        return "value_is_a_date"

    if _MULTIPLE_NOTATION.search(value_text):
        # "2.5X" beside "Total Deposits" says deposits grew two and a half
        # times, not that they are 2.5 of anything. There is no column head
        # here to correct that reading later, so it is refused now.
        return "value_is_a_multiple_not_an_amount"

    expected = match.definition.value_type
    non_monetary = expected in (ValueType.PERCENTAGE, ValueType.RATIO,
                                ValueType.COUNT)
    # A per-share figure is printed in the currency's base unit, always. A deck
    # reporting in millions still prints book value as "Rs. 56.47" and earnings
    # as "US$ 0.05", and letting the document's scale reach them turns 56 rupees
    # into 56 million rupees. It keeps the currency and refuses the magnitude.
    takes_a_scale = not non_monetary and expected != ValueType.PER_SHARE
    # A label that declares its own scale -- "Net Worth (US$ Mn.)" -- has
    # overruled the document's default, and the currency that belongs with that
    # scale is whatever the same parenthetical says. Where OCR has mangled the
    # symbol ("Uss Mn."), the currency is genuinely unreadable, and falling
    # back to the document's would report a dollar figure in rupees. Unknown is
    # the truthful answer.
    label_declares_scale = detect_magnitude(label_text) is not None
    num = normalize_number(
        value_text, context=label_text,
        default_currency=(None if non_monetary or label_declares_scale
                          else ctx.default_currency),
        default_magnitude=(None if not takes_a_scale or label_declares_scale
                           else ctx.default_unit),
        expected_type=expected if expected != ValueType.UNKNOWN else None,
        default_currency_inferred=True)
    if num is None or num.value is None:
        return "value_not_parseable"
    if (not non_monetary
            and num.magnitude == MagnitudeUnit.NONE
            and num.currency == Currency.UNKNOWN):
        # A monetary metric whose figure states neither a scale nor a currency.
        # In a table the column head or the caption would supply them; in a
        # layout nothing does, so the number's magnitude is genuinely unknown
        # and reporting it would invent one.
        return "no_scale_or_currency_on_a_monetary_figure"
    if not non_monetary and num.value_type == ValueType.PERCENTAGE:
        # A share-of-mix percentage sitting beside "Retail Deposits" in a chart
        # legend is not the deposits figure. In a table the column head would
        # settle which of the two a cell is; a layout has no such arbiter, so a
        # percentage against a monetary metric is refused rather than recorded
        # as an amount.
        return "percentage_value_for_a_monetary_metric"

    value_type = num.value_type
    if expected != ValueType.UNKNOWN and value_type == ValueType.UNKNOWN:
        value_type = expected
    if non_monetary:
        value_type = expected
    return Reading(match=match, value=num.value, base_value=num.base_value,
                   value_type=value_type, currency=num.currency,
                   magnitude=num.magnitude,
                   currency_inferred=num.currency_inferred)


def extract_from_cards(finding: GridFinding, ctx: CardContext,
                       ) -> CardExtraction:
    """Read every accepted card on a page into canonical facts."""
    facts: list[FinancialFact] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for card in finding.cards:
        reading = read_label_and_value(card.label_text, card.value_text, ctx)
        if isinstance(reading, str):
            reject(reading)
            continue
        match = reading.match
        num = reading
        value_type = reading.value_type

        period, period_source = _period_for_card(card, ctx)

        fact_id = stable_id("fact", ctx.document_id, "kpi",
                            card.page_number, card.label.id, card.value.id)
        prov = Provenance(
            document_id=ctx.document_id, page_number=card.page_number,
            bbox=BBoxModel(**card.bbox.as_dict()),
            original_text=card.value_text,
            ocr_element_ids=card.element_ids(),
            page_image_url=ctx.page_image_url,
            extraction_method=STRATEGY)

        # The same confidence scale as every other strategy. Structure is the
        # component that differs: for a card it is how strong the layout
        # evidence was, not how sound a reconstructed grid was.
        cards_on_page = int(card.evidence.get("cards_on_page", 0) or 0)
        layout_strength = min(1.0, 0.55 + 0.08 * cards_on_page)
        sig = conf_mod.Signals(
            ocr=min(card.label.confidence, card.value.confidence),
            layout=layout_strength,
            structure=layout_strength,
            semantic=match.score,
            corroborated=False,
            notes=[f"metric matched by {match.method} ({match.score:.2f})",
                   f"read from a KPI tile in a layout of {cards_on_page} "
                   f"across {card.evidence.get('columns_on_page', 0)} columns",
                   (f"period taken from {period_source}" if period
                    else "no period evidence inside the tile")])
        breakdown = conf_mod.score(sig)

        meta: dict = {
            "extraction_strategy": STRATEGY,
            "spatial_evidence": dict(card.evidence),
            "label_text": card.label_text,
        }
        if card.change is not None:
            # Kept as text: a movement note is context, and turning
            # "(20% YoY | 5% QoQ)" into a growth number would be asserting
            # which of the two this figure's growth is.
            meta["change_note"] = card.change.text.strip()
        if period is None:
            meta["period_absence"] = str(PeriodBasis.UNKNOWN)
        elif period.basis == PeriodBasis.INHERITED:
            meta["period_inherited_from"] = period_source

        facts.append(FinancialFact(
            fact_id=fact_id, document_id=ctx.document_id,
            metric=card.label_text,
            canonical_metric=match.key,
            metric_display=match.definition.display,
            entity=ctx.entity, entity_id=ctx.entity_id,
            entity_type=ctx.entity_type, parent_entity_id=ctx.parent_entity_id,
            statement=match.definition.statement,
            category=match.definition.category,
            derivation=DerivationStatus.REPORTED,
            kind=FactKind.KPI, nature=StatementNature.ACTUAL,
            value=num.value, raw_value=card.value_text, value_type=value_type,
            currency=num.currency, unit=num.magnitude,
            base_value=num.base_value,
            unit_inferred=(num.magnitude != MagnitudeUnit.NONE
                           and ctx.default_unit is not None
                           and num.magnitude == ctx.default_unit),
            currency_inferred=num.currency_inferred,
            period=(period.label if period else None),
            period_normalized=period,
            confidence=breakdown.overall, confidence_band=breakdown.band,
            confidence_breakdown=breakdown,
            source_page=card.page_number, provenance=prov,
            original_text=card.value_text,
            context_text=card.label_text,
            meta=meta))

    return CardExtraction(facts=facts, rejected=rejected)


# ---------------------------------------------------- horizontal metric rows
#: The evidence behind a row is weaker than the evidence behind a tile, and the
#: confidence has to say so. A tile is enclosed by whitespace on four sides; a
#: row is separated along one axis only, which is also the shape of a table
#: row, a chart legend and a credit rating grid.
_ROW_LAYOUT_CEILING = 0.72
_ROW_LAYOUT_BASE = 0.42
_ROW_LAYOUT_PER_ROW = 0.06


def _period_for_row(row: RowCandidate, block_period: NormalizedPeriod | None,
                    block_period_source: str,  ctx: CardContext,
                    ) -> tuple[NormalizedPeriod | None, str]:
    """The period a row's figure belongs to, and where it came from.

    Two sources, in strict order of evidence, and nothing else:

    1. **In the row's own text.** "Basic EPS (Annualized Q1 FY27)" states the
       period as part of naming the measure, and so does a continuation line
       directly beneath the label. This is the only route to ``stated``.
    2. **A heading that spatially contains the block.** "Shareholding
       (June 30, 2026)" set above a panel governs what is set out in it --
       provided it is actually above *that* panel and spans it. Inherited, with
       the heading recorded.

    There is deliberately no page-level fallback. A tile grid can take one,
    because a grid occupies its page and a heading over the page is a heading
    over the grid. A block occupies a corner, and the page may hold three more;
    the "Jun-26" stamp in the top corner of a slide governs neither the block
    below it nor anything else, and taking it gave a share count a date that
    came from a page decoration.

    Nearness is never a source. A period beside a block, or above a different
    block at the same height, is not evidence about this one -- that is the
    rule a footnote's "H1 FY19" broke when it reached a bar from another year.
    """
    stated = parse_period(row.label_text, calendar=ctx.calendar)
    if stated is not None:
        return stated, "the row's own label"
    if block_period is not None:
        inherited = block_period.model_copy(deep=True)
        inherited.basis = PeriodBasis.INHERITED
        return inherited, block_period_source
    return None, ""


def heading_over_block(block, headings: list, *, line_height: float,
                       ) -> object | None:
    """The heading that governs this block, if any heading does.

    Containment, not proximity. The heading must span the block horizontally
    and sit directly above it with nothing in between, which is what a designer
    does when a date heads a panel. A heading beside the block -- over the pie
    chart in the other half of the page -- governs the pie chart.
    """
    box = block.bbox
    if box is None:
        return None
    best = None
    best_gap = None
    for heading in headings:
        h = BBox.from_dict(heading.bbox.model_dump())
        if h.y1 > box.y:
            continue                       # not above the block
        overlap = min(h.x1, box.x1) - max(h.x, box.x)
        if overlap <= 0 or overlap < box.width * 0.6:
            continue                       # does not span it
        gap = box.y - h.y1
        if gap > line_height * 8:
            continue                       # too far above to be its heading
        if best_gap is None or gap < best_gap:
            best, best_gap = heading, gap
    return best


def extract_from_rows(finding: RowFinding, ctx: CardContext,
                      *, line_height: float = 40.0) -> CardExtraction:
    """Read every accepted horizontal row on a page into canonical facts."""
    facts: list[FinancialFact] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for block in finding.blocks:
        heading = heading_over_block(block, finding.headings,
                                     line_height=line_height)
        block_period = None
        block_period_source = ""
        if heading is not None:
            block_period = parse_period(heading.text, calendar=ctx.calendar)
            if block_period is not None:
                block_period_source = (
                    f"the heading {heading.text.strip()!r} set above this "
                    f"block")

        for row in block.rows:
            reading = read_label_and_value(row.label_text, row.value_text, ctx)
            if isinstance(reading, str):
                reject(reading)
                continue
            match = reading.match

            period, period_source = _period_for_row(
                row, block_period, block_period_source, ctx)

            rows_in_block = int(row.evidence.get("rows_in_block", 0) or 0)
            layout_strength = min(
                _ROW_LAYOUT_CEILING,
                _ROW_LAYOUT_BASE + _ROW_LAYOUT_PER_ROW * rows_in_block)
            sig = conf_mod.Signals(
                ocr=min(row.label.confidence, row.value.confidence),
                layout=layout_strength,
                structure=layout_strength,
                semantic=match.score,
                corroborated=False,
                notes=[f"metric matched by {match.method} ({match.score:.2f})",
                       f"read from a horizontal metric row in a block of "
                       f"{rows_in_block} sharing both edges",
                       (f"period taken from {period_source}" if period
                        else "no period evidence in the row or its heading")])
            breakdown = conf_mod.score(sig)

            prov = Provenance(
                document_id=ctx.document_id, page_number=row.page_number,
                bbox=BBoxModel(**row.bbox.as_dict()),
                original_text=row.value_text,
                ocr_element_ids=row.element_ids(),
                page_image_url=ctx.page_image_url,
                extraction_method=ROW_STRATEGY)

            meta: dict = {
                "extraction_strategy": ROW_STRATEGY,
                "spatial_evidence": dict(row.evidence),
                "label_text": row.label_text,
            }
            if period is None:
                meta["period_absence"] = str(PeriodBasis.UNKNOWN)
            elif period.basis == PeriodBasis.INHERITED:
                meta["period_inherited_from"] = period_source

            facts.append(FinancialFact(
                fact_id=stable_id("fact", ctx.document_id, "row",
                                  row.page_number, row.label.id, row.value.id),
                document_id=ctx.document_id,
                metric=row.label_text,
                canonical_metric=match.key,
                metric_display=match.definition.display,
                entity=ctx.entity, entity_id=ctx.entity_id,
                entity_type=ctx.entity_type,
                parent_entity_id=ctx.parent_entity_id,
                statement=match.definition.statement,
                category=match.definition.category,
                derivation=DerivationStatus.REPORTED,
                kind=FactKind.KPI, nature=StatementNature.ACTUAL,
                value=reading.value, raw_value=row.value_text,
                value_type=reading.value_type,
                currency=reading.currency, unit=reading.magnitude,
                base_value=reading.base_value,
                unit_inferred=(reading.magnitude != MagnitudeUnit.NONE
                               and ctx.default_unit is not None
                               and reading.magnitude == ctx.default_unit),
                currency_inferred=reading.currency_inferred,
                period=(period.label if period else None),
                period_normalized=period,
                confidence=breakdown.overall, confidence_band=breakdown.band,
                confidence_breakdown=breakdown,
                source_page=row.page_number, provenance=prov,
                original_text=row.value_text,
                context_text=row.label_text,
                meta=meta))

    return CardExtraction(facts=facts, rejected=rejected)


# --------------------------------------------------------- drawn grids
#: A grid gives two independent axes -- a row label and a column head -- which
#: is the same kind of evidence a reconstructed table cell has. It is scored
#: below one, because the grid was inferred from positions rather than read off
#: ruling lines, and above a horizontal row, which has one axis only.
_GRID_LAYOUT_CEILING = 0.82
_GRID_LAYOUT_BASE = 0.55
_GRID_LAYOUT_PER_COLUMN = 0.05


def extract_from_grid(finding: TransposedFinding, ctx: CardContext,
                      ) -> CardExtraction:
    """Read every accepted cell of a drawn grid into canonical facts."""
    facts: list[FinancialFact] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for cand in finding.candidates:
        reading = read_label_and_value(cand.label_text, cand.value_text, ctx)
        if isinstance(reading, str):
            reject(reading)
            continue
        match = reading.match

        # The period comes from the column head, and only where that head was
        # found to line up with this column. A misaligned head is refused
        # upstream and arrives as None; the figure then keeps no period rather
        # than borrowing its neighbour's.
        period = None
        period_source = ""
        if cand.period is not None:
            period = parse_period(cand.period_text, calendar=ctx.calendar)
            if period is not None:
                period_source = (f"the column head {cand.period_text!r} "
                                 f"above this figure")

        columns = int(cand.evidence.get("grid_columns", 0) or 0)
        layout_strength = min(
            _GRID_LAYOUT_CEILING,
            _GRID_LAYOUT_BASE + _GRID_LAYOUT_PER_COLUMN * columns)
        sig = conf_mod.Signals(
            ocr=min(cand.label.confidence, cand.value.confidence),
            layout=layout_strength, structure=layout_strength,
            semantic=match.score, corroborated=False,
            notes=[f"metric matched by {match.method} ({match.score:.2f})",
                   f"read from a "
                   f"{cand.evidence.get('grid_rows', 0)}x{columns} grid of "
                   f"figures, "
                   f"{float(cand.evidence.get('grid_filled', 0)):.0%} filled",
                   (f"period taken from {period_source}" if period
                    else "the column head did not line up with this column, "
                         "so no period was taken")])
        breakdown = conf_mod.score(sig)

        prov = Provenance(
            document_id=ctx.document_id, page_number=cand.page_number,
            bbox=BBoxModel(**cand.bbox.as_dict()),
            original_text=cand.value_text,
            ocr_element_ids=cand.element_ids(),
            page_image_url=ctx.page_image_url,
            extraction_method=GRID_STRATEGY)

        meta: dict = {
            "extraction_strategy": GRID_STRATEGY,
            "spatial_evidence": dict(cand.evidence),
            "label_text": cand.label_text,
        }
        if period is None:
            meta["period_absence"] = str(PeriodBasis.UNKNOWN)

        facts.append(FinancialFact(
            fact_id=stable_id("fact", ctx.document_id, "grid",
                              cand.page_number, cand.label.id, cand.value.id),
            document_id=ctx.document_id,
            metric=cand.label_text, canonical_metric=match.key,
            metric_display=match.definition.display,
            entity=ctx.entity, entity_id=ctx.entity_id,
            entity_type=ctx.entity_type,
            parent_entity_id=ctx.parent_entity_id,
            statement=match.definition.statement,
            category=match.definition.category,
            derivation=DerivationStatus.REPORTED,
            kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
            value=reading.value, raw_value=cand.value_text,
            value_type=reading.value_type,
            currency=reading.currency, unit=reading.magnitude,
            base_value=reading.base_value,
            unit_inferred=(reading.magnitude != MagnitudeUnit.NONE
                           and ctx.default_unit is not None
                           and reading.magnitude == ctx.default_unit),
            currency_inferred=reading.currency_inferred,
            period=(period.label if period else None),
            period_normalized=period,
            confidence=breakdown.overall, confidence_band=breakdown.band,
            confidence_breakdown=breakdown,
            source_page=cand.page_number, provenance=prov,
            original_text=cand.value_text, context_text=cand.label_text,
            meta=meta))

    return CardExtraction(facts=facts, rejected=rejected)
