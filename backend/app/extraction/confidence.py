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
from app.models.enums import ConfidenceBand, ValidationStatus
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


def requires_review(breakdown: ConfidenceBreakdown,
                    status: ValidationStatus) -> bool:
    if status in (ValidationStatus.FAILED, ValidationStatus.WARNING,
                  ValidationStatus.REQUIRES_REVIEW):
        return True
    return breakdown.overall < settings.review_threshold
