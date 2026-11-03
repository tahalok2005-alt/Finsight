"""The last check before a financial fact is allowed to look authoritative.

Everything upstream tries to read a figure correctly. This asks a different
question: *given what we ended up with, is this fact fit to be shown as a
finished answer?* A number can be read perfectly and still be unfit -- a
correct value under the wrong period is not a smaller error than a misread
digit, it is a larger one, because it looks right.

The gate does not delete facts and does not alter values. It decides two
things: whether the fact may be presented as confident, and what a reader is
told is missing. A fact that fails is kept, marked REVIEW_REQUIRED, held below
the review threshold, and given a reason in plain words.

The rule that motivated it: a figure from a periodized statement with no period
was reaching users at HIGH confidence with no flag, because nothing in the
confidence model required a period and nothing else asked. Across the corpus
that was 260 facts -- one in five.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.models.enums import (
    Currency, MagnitudeUnit, PeriodBasis, ReviewReason, ValueType,
)

log = get_logger("extraction.safety_gate")

#: Value types that do not carry a currency, so a missing one is not a gap.
_CURRENCY_FREE = frozenset({
    ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT, ValueType.BPS,
})
#: Value types that do not carry a magnitude.
_SCALE_FREE = frozenset({
    ValueType.PERCENTAGE, ValueType.RATIO, ValueType.COUNT, ValueType.BPS,
    ValueType.PER_SHARE,
})


@dataclass(frozen=True, slots=True)
class GateFinding:
    """One thing missing from a fact, and how it should read to a person."""
    field: str
    reason: ReviewReason
    message: str


@dataclass
class GateResult:
    findings: list[GateFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def primary(self) -> GateFinding | None:
        return self.findings[0] if self.findings else None

    def as_dict(self) -> dict:
        return {"passed": self.passed,
                "findings": [{"field": f.field, "reason": str(f.reason),
                              "message": f.message} for f in self.findings]}


def _period_basis(fact) -> PeriodBasis | None:
    """How this fact's period was arrived at, or None if it has no period.

    The basis lives on the resolved period rather than on the fact, so reading
    it directly off the fact silently yields None for everything -- which would
    make the check below pass for every fact ever written.
    """
    period = getattr(fact, "period_normalized", None)
    return getattr(period, "basis", None) if period is not None else None


def check(fact, *, period_mandatory: bool,
          column_established: bool | None = None) -> GateResult:
    """Assess one fact. ``period_mandatory`` comes from the table's shape.

    Ordering matters: the first finding becomes the message a reader sees, so
    the most consequential gap is checked first. A figure with no period is
    worse than one with no entity, because a reader can often supply the entity
    from the page and can never supply the period.
    """
    out = GateResult()

    # ---- period ------------------------------------------------------------
    if period_mandatory and not fact.period:
        out.findings.append(GateFinding(
            "period", ReviewReason.AMBIGUOUS_PERIOD,
            "This figure comes from a statement whose columns are reporting "
            "periods, but its own period could not be established. The value "
            "is shown without one rather than guessed."))
    elif period_mandatory and _period_basis(fact) == PeriodBasis.NOT_APPLICABLE:
        # The two halves of the record disagree: the fact claims a period while
        # its column claims to have none. Whichever is wrong, it is not settled.
        out.findings.append(GateFinding(
            "period", ReviewReason.AMBIGUOUS_PERIOD,
            "This figure's column was recorded as having no reporting period, "
            "but the statement it belongs to is organised by period."))

    # ---- the column it came from -------------------------------------------
    if column_established is False:
        out.findings.append(GateFinding(
            "column", ReviewReason.AMBIGUOUS_PERIOD,
            "The table's columns could not be matched to their headings, so "
            "this figure's column -- and therefore its period -- is not "
            "established."))

    # ---- value type, unit, currency ----------------------------------------
    vt = getattr(fact, "value_type", None)
    if vt in (None, ValueType.UNKNOWN):
        out.findings.append(GateFinding(
            "value_type", ReviewReason.UNIT_UNDETERMINED,
            "What kind of quantity this is -- money, a percentage, a count -- "
            "could not be established from the source."))
    else:
        # MagnitudeUnit.NONE is the "no scale stated" value; there is no
        # UNKNOWN member. For a monetary figure that is a gap, because every
        # monetary figure in a filing is printed at some scale.
        if vt not in _SCALE_FREE and getattr(fact, "unit", None) in (
                None, MagnitudeUnit.NONE):
            out.findings.append(GateFinding(
                "unit", ReviewReason.UNIT_UNDETERMINED,
                "The scale of this figure -- crore, million, thousand -- is "
                "not stated anywhere the reader could see."))
        if vt not in _CURRENCY_FREE and getattr(fact, "currency", None) in (
                None, Currency.UNKNOWN):
            out.findings.append(GateFinding(
                "currency", ReviewReason.UNIT_UNDETERMINED,
                "The currency of this figure could not be read from the "
                "document."))

    # ---- provenance --------------------------------------------------------
    prov = getattr(fact, "provenance", None)
    if prov is None or not getattr(prov, "page_number", None):
        out.findings.append(GateFinding(
            "provenance", ReviewReason.LOW_CONFIDENCE,
            "This figure has no recorded place in the document, so it cannot "
            "be checked against the page."))
    elif getattr(prov, "bbox", None) is None:
        out.findings.append(GateFinding(
            "provenance", ReviewReason.LOW_CONFIDENCE,
            "This figure's exact position on the page was not recorded."))

    return out


def apply(fact, result: GateResult, *, review_threshold: float) -> bool:
    """Record the gate's verdict on the fact. Returns True if it was held back.

    Confidence is *capped*, never rewritten: whatever the scorer concluded from
    the evidence stands, but a fact that failed the gate may not present itself
    as settled. The cap sits just below the review threshold so the fact reads
    as needing attention rather than as nearly-confident.
    """
    fact.meta = dict(getattr(fact, "meta", None) or {})
    fact.meta["safety_gate"] = result.as_dict()
    if result.passed:
        return False

    fact.requires_review = True
    primary = result.primary
    if primary is not None:
        fact.review_reason = primary.reason
        fact.meta["safety_gate_message"] = primary.message
    ceiling = max(0.0, review_threshold - 0.01)
    if fact.confidence > ceiling:
        fact.meta["confidence_before_gate"] = round(fact.confidence, 4)
        fact.confidence = round(ceiling, 4)
    return True
