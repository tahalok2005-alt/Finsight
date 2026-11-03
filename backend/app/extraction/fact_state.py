"""What a fact's overall state is, from evidence kept apart rather than blended.

A single confidence number cannot answer the question a reader has. "0.92"
blends how clearly the digits were printed with whether anyone established what
period they belong to, and those fail independently: a figure can be read
perfectly from a clean page and still be worthless because its column was never
matched to a heading. Averaged together, excellent recognition hides poor
attribution -- which is precisely the failure this project keeps finding.

So the dimensions are scored separately and the *weakest* one decides. That is
not a formula, it is a rule about financial data: a figure is only as good as
the least-established thing about it.

Nothing here recomputes confidence. It reads what earlier stages established
and states the consequence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.models.enums import (
    Currency, MagnitudeUnit, ValidationStatus, ValueType,
)


class Level(StrEnum):
    """How well one dimension of a fact is established."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}[self.value]


class FactState(StrEnum):
    """The standard states a fact can be in, as the brief defines them."""
    VALIDATED = "VALIDATED"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    MEDIUM_CONFIDENCE = "MEDIUM_CONFIDENCE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    UNKNOWN = "UNKNOWN"


#: Value types that carry no currency, so a missing one is not a gap.
_CURRENCY_FREE = frozenset({
    ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT, ValueType.BPS,
})
#: Value types that carry no magnitude.
_SCALE_FREE = frozenset({
    ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT, ValueType.BPS,
    ValueType.PER_SHARE,
})


@dataclass
class Evidence:
    """One reading per dimension, plus the reason for the weakest of them.

    Phase 22 split the old "unit" into scale and currency, added the period's
    duration and the structure the figure was read from, and reads value from
    the digits' own recognition rather than from the blended score -- a single
    number cannot say which of these is the one holding a fact back.
    """
    metric: Level = Level.UNKNOWN
    value: Level = Level.UNKNOWN
    period: Level = Level.UNKNOWN
    duration: Level = Level.UNKNOWN
    entity: Level = Level.UNKNOWN
    unit: Level = Level.UNKNOWN
    currency: Level = Level.UNKNOWN
    structure: Level = Level.UNKNOWN
    accounting: Level = Level.UNKNOWN
    corroboration: Level = Level.UNKNOWN
    #: Dimensions that are not applicable to this fact, and so do not weigh.
    not_applicable: set[str] = field(default_factory=set)
    limiting: str = ""

    SCORED = ("metric", "value", "period", "duration", "entity", "unit", "currency",
              "structure")

    def scored(self) -> dict[str, Level]:
        return {name: getattr(self, name) for name in self.SCORED
                if name not in self.not_applicable}

    def as_dict(self) -> dict:
        out = {name: str(level) for name, level in self.scored().items()}
        out["accounting"] = str(self.accounting)
        out["corroboration"] = str(self.corroboration)
        if self.not_applicable:
            out["not_applicable"] = sorted(self.not_applicable)
        if self.limiting:
            out["limiting"] = self.limiting
        return out


def _band(confidence: float) -> Level:
    if confidence >= 0.85:
        return Level.HIGH
    if confidence >= 0.65:
        return Level.MEDIUM
    if confidence > 0.0:
        return Level.LOW
    return Level.UNKNOWN


#: How a label reached its metric, by strength: the taxonomy declaring the
#: label (or its OCR-damaged spelling) word for word, or a judgement of
#: similarity.
_EXACT_METHODS = frozenset({"exact", "prefix", "squashed", "recognition-exact", "context"})


def assess(fact, *, period_mandatory: bool) -> Evidence:
    """Read each dimension of one fact from what the pipeline established."""
    ev = Evidence()
    meta = getattr(fact, "meta", None) or {}

    # ---- metric -----------------------------------------------------------
    if getattr(fact, "canonical_metric", None):
        method = meta.get("metric_match_method", "")
        ev.metric = Level.HIGH if method in _EXACT_METHODS else Level.MEDIUM
    elif fact.metric:
        ev.metric = Level.LOW

    # ---- value ------------------------------------------------------------
    # How well the DIGITS were read: the recogniser's confidence in the cell,
    # not the blended score (which already carries period and metric). A
    # second, independent read that disagrees makes the value doubtful
    # whatever the first read's confidence; one that agrees corroborates it.
    if fact.value is None:
        ev.value = Level.UNKNOWN
    else:
        bd = getattr(fact, "confidence_breakdown", None)
        ocr = getattr(bd, "ocr", None) if bd is not None else None
        ev.value = _band(ocr if ocr is not None else fact.confidence)
        second = meta.get("second_read") or {}
        if second.get("agrees") is False:
            ev.value = Level.LOW
        elif second.get("agrees") is True and ev.value is Level.MEDIUM:
            ev.value = Level.HIGH

    # ---- period and its duration -----------------------------------------
    period = getattr(fact, "period_normalized", None)
    if fact.period and period is not None:
        basis = str(getattr(period, "basis", "") or "")
        # "stated" and "inherited" are both column-level evidence and both are
        # strong. Inherited does not mean guessed: it means the period reached
        # the cell along the structural path the table model defines -- a date
        # in the column head under a span word in the group head, which is how
        # a well-formed statement expresses a period.
        #
        # "derived" is genuinely weaker: the period was reconstructed rather
        # than read, from a span phrase plus a date found elsewhere.
        if basis in ("stated", "inherited"):
            ev.period = Level.HIGH
        elif basis == "derived":
            ev.period = Level.MEDIUM
        else:
            ev.period = Level.LOW
        # The span is a separate question from the end date: the quarter to
        # 31 March and the year to 31 March share an end.
        kind = str(getattr(period, "period_type", "") or "")
        ev.duration = Level.UNKNOWN if kind in ("", "unknown") else ev.period
    elif not period_mandatory:
        # A segment column genuinely has no period. Scoring it UNKNOWN would
        # hold every segment table below the bar for a gap that is not one.
        ev.not_applicable.update({"period", "duration"})
    else:
        ev.period = Level.UNKNOWN
        ev.duration = Level.UNKNOWN

    # ---- entity -----------------------------------------------------------
    if fact.entity:
        ev.entity = Level.HIGH if getattr(fact, "entity_id", None) else Level.MEDIUM
    else:
        ev.entity = Level.UNKNOWN

    # ---- scale and currency ------------------------------------------------
    vt = getattr(fact, "value_type", None)
    if vt in (None, ValueType.UNKNOWN):
        ev.unit = Level.UNKNOWN
        ev.currency = Level.UNKNOWN
    else:
        # `unit_inferred` marks a scale that came from the table's own
        # declaration rather than from the cell text. That is how every
        # financial statement declares its scale -- "(All amounts are in INR
        # Crores)" is a statement by the document, not an inference -- so it
        # is not weaker evidence.
        if vt in _SCALE_FREE:
            ev.not_applicable.add("unit")
        else:
            ev.unit = (Level.HIGH if fact.unit not in (None, MagnitudeUnit.NONE)
                       else Level.UNKNOWN)
        # `currency_inferred` is different: the currency was read from
        # magnitude words ("crore") rather than a symbol or code, which is a
        # real inference and is scored as one.
        if vt in _CURRENCY_FREE:
            ev.not_applicable.add("currency")
        elif fact.currency in (None, Currency.UNKNOWN):
            ev.currency = Level.UNKNOWN
        else:
            ev.currency = (Level.MEDIUM if getattr(fact, "currency_inferred", False)
                           else Level.HIGH)

    # ---- structure: where the figure was read from --------------------------
    prov = getattr(fact, "provenance", None)
    gate = meta.get("safety_gate") or {}
    if prov is None:
        ev.structure = Level.UNKNOWN
    elif gate and gate.get("passed") is False:
        # The table's reconstruction could not vouch for the cell's column.
        ev.structure = Level.LOW
    elif (str(getattr(fact, "kind", "table_cell")) == "table_cell"
            and meta.get("row") is not None and meta.get("col") is not None):
        ev.structure = Level.MEDIUM if meta.get("column_qualifier") else Level.HIGH
    else:
        # A tile, a row of a slide, a sentence: located, but with no grid.
        ev.structure = Level.MEDIUM

    # ---- accounting and corroboration: evidence, never requirements --------
    status = str(getattr(fact, "validation_status", "") or "")
    if status == str(ValidationStatus.PASSED):
        ev.accounting = Level.HIGH
    elif status == str(ValidationStatus.FAILED):
        ev.accounting = Level.LOW
    elif status == str(ValidationStatus.WARNING):
        ev.accounting = Level.MEDIUM
    else:
        ev.accounting = Level.UNKNOWN

    # Independent strategies that read the same figure. Reconciliation records
    # them in the meta (including the fact's own strategy); an attribute of
    # that name never existed on a fact, so until Phase 22 this was always
    # UNKNOWN.
    recorded = meta.get("corroborating_sources")
    if recorded:
        corroborated = max(0, len(recorded) - 1)
    else:
        corroborated = len(getattr(fact, "corroborating_sources", None) or [])
    if corroborated >= 2:
        ev.corroboration = Level.HIGH
    elif corroborated == 1:
        ev.corroboration = Level.MEDIUM
    else:
        ev.corroboration = Level.UNKNOWN

    # The limiting dimension is only meaningful when something is actually
    # limiting. With every dimension at HIGH, min() still returns one of them,
    # and the interface then reads "limited by metric" against a fact nothing
    # is holding back.
    scored = ev.scored()
    if scored:
        weakest_name = min(scored, key=lambda name: scored[name].rank)
        if scored[weakest_name] is not Level.HIGH:
            ev.limiting = weakest_name
    return ev


def state_of(fact, evidence: Evidence) -> FactState:
    """The fact's overall state: the weakest dimension decides.

    A fact flagged for review is REQUIRES_REVIEW whatever else is known: the
    flag names something a person must settle, and showing it as merely
    incomplete would hide that. Accounting agreement can promote a sound
    fact to VALIDATED, but it can never rescue one whose period or entity was
    never established -- an arithmetic check confirms a figure; it does not
    say which quarter the figure belongs to. VALIDATED needs every applicable
    dimension HIGH, a passing check, and no contradicting source.
    """
    scored = evidence.scored()
    if not scored:
        return FactState.UNKNOWN
    weakest = min(level.rank for level in scored.values())

    if getattr(fact, "requires_review", False) or weakest == Level.LOW.rank:
        return FactState.REQUIRES_REVIEW
    if weakest == Level.UNKNOWN.rank:
        return FactState.UNKNOWN
    if weakest == Level.MEDIUM.rank:
        return FactState.MEDIUM_CONFIDENCE
    if evidence.accounting is Level.LOW:
        # Everything about the fact is well established and the arithmetic
        # still disagrees. That is worth a person's attention precisely
        # because nothing else looks wrong.
        return FactState.REQUIRES_REVIEW
    contradicted = (str(getattr(fact, "duplicate_resolution", "") or "")
                    == "genuine_conflict")
    if evidence.accounting is Level.HIGH and not contradicted:
        return FactState.VALIDATED
    return FactState.HIGH_CONFIDENCE


def describe(fact, *, period_mandatory: bool) -> tuple[FactState, Evidence]:
    evidence = assess(fact, period_mandatory=period_mandatory)
    return state_of(fact, evidence), evidence
