"""Confidence scoring.

A fact's confidence is a weighted blend of independent evidence channels, not a
single number pulled from the OCR engine.  Each component answers a different
question:

* ``ocr``           - how well were the characters read?
* ``layout``        - how sure are we the region is what we think it is?
* ``structure``     - for a table cell: how sound is the reconstructed grid?
* ``semantic``      - how confidently did the label map onto a canonical metric?
* ``consistency``   - did arithmetic validation agree with the document?
* ``corroboration`` - did an independent channel (text layer, a duplicate
                      statement elsewhere in the document) read the same value?

Bands are deliberately coarse.  Reporting "0.8734" would imply a precision this
evidence does not support; HIGH/MEDIUM/LOW/REQUIRES_REVIEW is what an analyst
can act on.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import settings
from app.models.enums import (
    ConfidenceBand, DuplicateResolution, FactKind, MagnitudeUnit, PeriodBasis,
    ReviewReason, ValidationStatus, ValueType,
)
from app.models.schemas import ConfidenceBreakdown

# Weights sum to 1.0.  OCR and semantic matching dominate because a misread
# digit or a mis-mapped label is unrecoverable, whereas a slightly wrong region
# type usually still yields the right number.
WEIGHTS = {
    "ocr": 0.30,
    "layout": 0.12,
    "structure": 0.18,
    "semantic": 0.25,
    "consistency": 0.10,
    "corroboration": 0.05,
}


@dataclass(slots=True)
class Signals:
    ocr: float = 0.0
    layout: float = 0.0
    structure: float = 0.0
    semantic: float = 0.0
    consistency: float | None = None
    corroborated: bool = False
    duplicate_agreements: int = 0
    notes: list[str] | None = None


def band_for(score: float) -> ConfidenceBand:
    if score >= settings.confidence_high:
        return ConfidenceBand.HIGH
    if score >= settings.confidence_medium:
        return ConfidenceBand.MEDIUM
    if score >= 0.40:
        return ConfidenceBand.LOW
    return ConfidenceBand.REQUIRES_REVIEW


def score(sig: Signals) -> ConfidenceBreakdown:
    notes = list(sig.notes or [])

    corroboration = 0.0
    if sig.corroborated:
        corroboration += 0.7
        notes.append("text-layer agrees with OCR")
    if sig.duplicate_agreements > 0:
        corroboration += min(0.3, 0.15 * sig.duplicate_agreements)
        notes.append(f"value repeated consistently {sig.duplicate_agreements}x "
                     "elsewhere in the document")
    corroboration = min(1.0, corroboration)

    # An unvalidated fact is neither penalised nor rewarded: its consistency
    # component falls back to the mean of the other evidence.
    if sig.consistency is None:
        consistency = (sig.ocr + sig.semantic + sig.structure) / 3.0
        notes.append("no arithmetic check applicable")
    else:
        consistency = sig.consistency

    parts = {
        "ocr": max(0.0, min(1.0, sig.ocr)),
        "layout": max(0.0, min(1.0, sig.layout)),
        "structure": max(0.0, min(1.0, sig.structure)),
        "semantic": max(0.0, min(1.0, sig.semantic)),
        "consistency": max(0.0, min(1.0, consistency)),
        "corroboration": corroboration,
    }
    overall = sum(parts[k] * w for k, w in WEIGHTS.items())

    # Hard floor: an unreadable source can never yield a high-confidence fact,
    # however well everything else scored.
    if parts["ocr"] < 0.5:
        overall = min(overall, 0.55)
        notes.append("OCR confidence below 0.5 caps the overall score")
    if parts["semantic"] < 0.5:
        overall = min(overall, 0.6)
        notes.append("weak metric match caps the overall score")

    overall = round(max(0.0, min(1.0, overall)), 4)
    return ConfidenceBreakdown(
        ocr=round(parts["ocr"], 4), layout=round(parts["layout"], 4),
        structure=round(parts["structure"], 4), semantic=round(parts["semantic"], 4),
        consistency=round(parts["consistency"], 4),
        corroboration=round(corroboration, 4), overall=overall,
        band=band_for(overall), notes=notes)


def apply_reconciliation(breakdown: ConfidenceBreakdown, *,
                         agreeing_sources: int = 0,
                         contradicted: bool = False) -> ConfidenceBreakdown:
    """Re-score a fact once the evidence layer has compared it with the rest.

    Two different kinds of thing arrive here, and they must not be mixed.

    **Agreement is quantitative.** Another strategy reading the same figure
    from different pixels is genuine independent evidence, and it moves the
    corroboration component. Copies from the *same* strategy are not counted:
    three rows of one table saying the same thing is one method agreeing with
    itself.

    **Contradiction is categorical.** A figure the document elsewhere disputes
    does not become slightly less certain -- it becomes a figure a person has
    to look at. So it is not blended into the weighted sum at all; it caps the
    result below the review threshold, where no amount of clean OCR can lift
    it. Averaging a contradiction away is exactly what this function exists to
    prevent.
    """
    if not agreeing_sources and not contradicted:
        return breakdown

    notes = list(breakdown.notes)
    corroboration = breakdown.corroboration
    overall = breakdown.overall
    if agreeing_sources:
        corroboration = min(1.0, corroboration + min(0.6, 0.3 * agreeing_sources))
        # The *delta*, applied to the score as it stands.
        #
        # Recomputing the weighted sum from the components looks equivalent and
        # is not: by this point the score carries caps and floors that earlier
        # stages applied -- an OCR floor, a validation cap -- and none of them
        # is recoverable from the components alone. Rebuilding the sum threw
        # those away, and a fact that gained a corroborating source could come
        # out *less* certain than before, which is the opposite of the truth.
        overall += (corroboration - breakdown.corroboration) * WEIGHTS["corroboration"]
        notes.append(
            f"{agreeing_sources} independent extraction "
            f"{'strategy' if agreeing_sources == 1 else 'strategies'} read the "
            f"same value")

    if contradicted:
        overall = min(overall, settings.review_threshold - 0.01)
        notes.append("another source in this document reports a different "
                     "value for the same measure, entity and period")

    overall = round(max(0.0, min(1.0, overall)), 4)
    return ConfidenceBreakdown(
        ocr=breakdown.ocr, layout=breakdown.layout,
        structure=breakdown.structure, semantic=breakdown.semantic,
        consistency=breakdown.consistency,
        corroboration=round(corroboration, 4), overall=overall,
        band=band_for(overall), notes=notes)


def apply_validation(breakdown: ConfidenceBreakdown,
                     status: ValidationStatus) -> ConfidenceBreakdown:
    """Re-score a fact once validation has run.

    A failed arithmetic check is strong evidence something is wrong: it drives
    the fact into review regardless of how clean the OCR was.
    """
    mapping = {
        ValidationStatus.PASSED: 1.0,
        ValidationStatus.WARNING: 0.45,
        ValidationStatus.FAILED: 0.05,
        ValidationStatus.REQUIRES_REVIEW: 0.3,
        ValidationStatus.NOT_CHECKED: breakdown.consistency,
    }
    consistency = mapping.get(status, breakdown.consistency)
    parts = {
        "ocr": breakdown.ocr, "layout": breakdown.layout,
        "structure": breakdown.structure, "semantic": breakdown.semantic,
        "consistency": consistency, "corroboration": breakdown.corroboration,
    }
    overall = sum(parts[k] * w for k, w in WEIGHTS.items())
    notes = list(breakdown.notes)
    if status == ValidationStatus.FAILED:
        overall = min(overall, 0.35)
        notes.append("arithmetic validation failed")
    elif status == ValidationStatus.WARNING:
        overall = min(overall, settings.review_threshold - 0.01)
        notes.append("arithmetic validation raised a warning")
    if breakdown.ocr < 0.5:
        overall = min(overall, 0.55)

    overall = round(max(0.0, min(1.0, overall)), 4)
    return ConfidenceBreakdown(
        ocr=breakdown.ocr, layout=breakdown.layout, structure=breakdown.structure,
        semantic=breakdown.semantic, consistency=round(consistency, 4),
        corroboration=breakdown.corroboration, overall=overall,
        band=band_for(overall), notes=notes)


#: The most a fact whose value could not be parsed may score. Its figure is not
#: established, so it must never outrank one whose figure is: a results
#: release's "PAT" on a chart with no legible value scored 0.77 and was chosen
#: over the table's 7,764 (0.64) for the same measure and period.
UNPARSED_VALUE_CAP = 0.30


def cap_unparsed_value(breakdown: ConfidenceBreakdown) -> ConfidenceBreakdown:
    """Re-score a fact that has no value: its value dimension is unknown."""
    if breakdown.overall <= UNPARSED_VALUE_CAP:
        return breakdown
    notes = [*breakdown.notes, "no value could be parsed: the figure is not established"]
    return ConfidenceBreakdown(
        ocr=breakdown.ocr, layout=breakdown.layout, structure=breakdown.structure,
        semantic=breakdown.semantic, consistency=breakdown.consistency,
        corroboration=breakdown.corroboration, overall=UNPARSED_VALUE_CAP,
        band=band_for(UNPARSED_VALUE_CAP), notes=notes)


def requires_review(breakdown: ConfidenceBreakdown,
                    status: ValidationStatus) -> bool:
    if status in (ValidationStatus.FAILED, ValidationStatus.WARNING,
                  ValidationStatus.REQUIRES_REVIEW):
        return True
    return breakdown.overall < settings.review_threshold


def review_reason_for(fact, status: ValidationStatus) -> ReviewReason | None:
    """Why this figure needs a person, or None if it does not.

    The order is by what a reviewer would want to look at first: a value that
    could not be read at all, then one whose arithmetic disagrees with the rest
    of the statement, then one whose scale is unknown, and only last one that
    is merely faint on the page.

    What is deliberately **not** here is a missing period. A number read
    cleanly off the page, matched to a metric and carrying a bounding box is a
    good number whether or not its column said which quarter it belongs to.
    That gap is reported in the period column, which is where it is true; a
    warning on the value would say the value is doubtful, which it is not.
    """
    if fact.value is None:
        return ReviewReason.VALUE_UNPARSED
    # A second, independent read of the cell disagrees about the digits or
    # the sign: the value itself is in doubt, whatever else is known.
    if ((fact.meta or {}).get("second_read") or {}).get("agrees") is False:
        return ReviewReason.VALUE_DISPUTED
    # Two lines of its table read as this measure, or a name the table uses
    # in another sense: WHAT the figure is, not its value, is in doubt.
    if (fact.meta or {}).get("metric_collision") or (fact.meta or {}).get("row_misaligned"):
        return ReviewReason.METRIC_AMBIGUOUS
    # A flow figure under a bare date: its span was never stated.
    if (fact.meta or {}).get("period_span_unstated"):
        return ReviewReason.AMBIGUOUS_PERIOD
    if status == ValidationStatus.FAILED:
        return ReviewReason.VALIDATION_FAILED
    if fact.duplicate_resolution == DuplicateResolution.GENUINE_CONFLICT:
        return ReviewReason.CONFLICTING_SOURCES
    if (fact.meta or {}).get("scale_disagreement"):
        # Two sources give the same digits at different scales. The number is
        # not in doubt; which scale applies to it is, and no later stage can
        # settle that.
        return ReviewReason.UNIT_UNDETERMINED
    if status in (ValidationStatus.WARNING, ValidationStatus.REQUIRES_REVIEW):
        return ReviewReason.VALIDATION_FAILED
    if (fact.value_type == ValueType.MONETARY
            and fact.unit in (MagnitudeUnit.NONE, None)
            and fact.kind == FactKind.TABLE_CELL):
        return ReviewReason.UNIT_UNDETERMINED
    # A column that read like a period and would not resolve, in a table that
    # settled other columns, is a genuine ambiguity worth a look.  A column
    # that never offered a period is not.
    if (not fact.period
            and (fact.meta or {}).get("period_absence") == str(PeriodBasis.UNKNOWN)):
        return ReviewReason.AMBIGUOUS_PERIOD
    if (fact.kind in (FactKind.NARRATIVE, FactKind.KPI, FactKind.CHART)
            and "table_cell" not in ((fact.meta or {}).get("corroborating_sources") or [])):
        # A figure from prose, a tile or a chart that no table repeats. What it
        # measures, when and at what scale rest on a sentence's wording or a
        # layout, and a human audit of such figures found half of them wrong in
        # some dimension (Phase 22). A table that states the same figure merges
        # with it and carries it instead.
        return ReviewReason.NARRATIVE_UNCONFIRMED
    if fact.confidence < settings.review_threshold:
        return ReviewReason.LOW_CONFIDENCE
    return None
