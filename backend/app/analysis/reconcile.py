"""Duplicate and conflict reconciliation.

A financial document repeats itself on purpose.  The same revenue figure appears
in a highlights box, in the statement of profit and loss, and again in a segment
table -- and those are not errors.  Two figures that *look* like duplicates may
in truth differ in entity, period, definition, unit or scope.

So nothing here deletes a number.  Every collision within one
:class:`~app.analysis.identity.FactIdentity` is **classified**:

``identical_repeat``      the same measure, same scope, same value -- corroboration
``different_definition``  two printed line items the taxonomy maps to one metric
``different_unit``        a rate against an amount, or two currencies
``different_scope``       near-neighbour metrics the taxonomy keeps distinct
``genuine_conflict``      same slot, comparable, different value
``uncertain``             one side has no period, so they cannot be shown to
                          describe the same thing

Only ``identical_repeat`` collapses, and even then the merged fact records where
its copies were found.  A ``genuine_conflict`` keeps **both** facts, marks
**both** for review, and links them -- the system never silently picks a winner.

What changed, and why
---------------------
The previous identity included the printed label, the unit and the currency.
Each of those is a real distinction, and putting them in the *identity* meant
the reconciler could never see the cases it existed for: a figure printed under
two labels was two facts, and a figure in crore beside the same figure in
million was two facts.  Measured on one filing, 36 groups sharing metric,
entity and period sat in different buckets, and the classifier's eight verdicts
collapsed to two because every other discriminator was already in the key.

Those distinctions now live where they belong -- in
:func:`~app.analysis.identity.comparable` and in the classification -- so the
group is formed first and the reason for a difference is worked out second.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.analysis.identity import (
    FactIdentity, agrees, comparable, identity_of, printed_label, scale_note,
)
from app.core.ids import stable_id
from app.core.logging import get_logger
from app.extraction import taxonomy
from app.extraction.qualification import same_basis
from app.models.enums import (
    DuplicateResolution, RelationKind, Severity, ValueType,
)
from app.models.schemas import FactRelation, FinancialFact

log = get_logger("analysis.reconcile")

# Re-exported: several modules and tests import these from here.
REL_TOLERANCE = 0.005
ABS_TOLERANCE = 0.01

#: Strategies in the order their structural evidence is trusted, strongest
#: first. Used only to choose which of two agreeing facts survives as the
#: anchor -- never to decide a disagreement, which is not resolvable by
#: preference.
_STRATEGY_RANK = {
    "table_cell": 4,
    "spatial_kpi": 3,
    "spatial_row": 2,
    "narrative_sentence": 1,
}


@dataclass(slots=True)
class Reconciliation:
    facts: list[FinancialFact]
    relations: list[FactRelation] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    merged: int = 0
    classified: dict[str, int] = field(default_factory=dict)
    #: fact_id -> how many independent sources agreed with it.
    corroboration: dict[str, int] = field(default_factory=dict)


def _same_value(a: FinancialFact, b: FinancialFact) -> bool:
    """Kept for callers outside this module; agreement including scale."""
    if a.value is None or b.value is None:
        return a.value is None and b.value is None
    return agrees(a, b)


def _strategy_of(f: FinancialFact) -> str:
    if f.provenance is not None and f.provenance.extraction_method:
        return str(f.provenance.extraction_method)
    return str(f.kind or "")


def classify(a: FinancialFact, b: FinancialFact) -> DuplicateResolution:
    """Explain why two facts in one identity are or are not the same figure.

    Ordered so that the strongest reason to keep them apart is given first. A
    verdict other than ``genuine_conflict`` means the two describe different
    things and neither is evidence against the other.
    """
    # Undated: the period is the one thing that could still separate them, and
    # it is unknown. They may agree -- that is corroboration -- but they can
    # never be shown to disagree.
    if not a.period or not b.period:
        if agrees(a, b):
            return DuplicateResolution.IDENTICAL_REPEAT
        return DuplicateResolution.UNCERTAIN

    if not comparable(a, b):
        # A rate against an amount, two currencies, or an unreadable value.
        # Real difference, not disagreement.
        return DuplicateResolution.DIFFERENT_UNIT

    # Stated on two bases -- "excluding" a one-off gain and the statutory
    # figure; "Adjusted" and "Reported" -- they are two facts even where the
    # figures happen to agree, and never a contradiction.
    if not same_basis(a, b):
        return DuplicateResolution.DIFFERENT_QUALIFICATION

    if agrees(a, b):
        return DuplicateResolution.IDENTICAL_REPEAT

    # From here the two are comparable and differ. Before calling that a
    # contradiction, exhaust the ways a document legitimately carries two
    # different numbers under one canonical metric.
    if a.canonical_metric != b.canonical_metric:
        definition = taxonomy.get(a.canonical_metric or "")
        if definition and b.canonical_metric in definition.distinct_from:
            return DuplicateResolution.DIFFERENT_DEFINITION
        return DuplicateResolution.DIFFERENT_SCOPE

    if printed_label(a) != printed_label(b):
        # The taxonomy mapped two differently-printed line items to one metric.
        # "Total Throughput" and "Production meant for sale" are separate rows
        # of the same statement carrying different numbers; treating them as
        # one measure would invent a contradiction out of a taxonomy limit.
        return DuplicateResolution.DIFFERENT_DEFINITION

    if _same_printed_number(a, b) and a.unit != b.unit:
        # The same digits under two different scales. One of the two scales is
        # wrong -- a figure cannot be both 286,788 million and 286,788 billion
        # -- but the document does not disagree about the *number*, and saying
        # it does misstates what a reviewer has to settle. Measured on a bank's
        # interim report: ten of sixty-two conflict pairs were this, and every
        # one was a scale picked up from the wrong heading.
        return DuplicateResolution.DIFFERENT_UNIT

    return DuplicateResolution.GENUINE_CONFLICT


def _same_printed_number(a: FinancialFact, b: FinancialFact) -> bool:
    """Whether the two carry the same digits, before any scale is applied."""
    if a.value is None or b.value is None:
        return False
    if abs(a.value - b.value) <= ABS_TOLERANCE:
        return True
    scale = max(abs(a.value), abs(b.value)) or 1.0
    return abs(a.value - b.value) / scale <= REL_TOLERANCE


def _anchor_first(group: list[FinancialFact]) -> list[FinancialFact]:
    """Order a group so the best-evidenced fact leads.

    Strategy rank before confidence: a reconstructed table cell has two
    independent axes agreeing on what a figure is, which is structurally
    stronger evidence than a high score on a sentence.
    """
    return sorted(
        group,
        key=lambda f: (_STRATEGY_RANK.get(_strategy_of(f), 0), f.confidence),
        reverse=True)


def _record_conflict(document_id: str, anchor: FinancialFact,
                     other: FinancialFact, result: Reconciliation) -> None:
    """Keep both figures, flag both, link both, and explain the difference."""
    # Both sides carry the verdict. Setting it only on one left the survivor
    # looking like a clean high-confidence figure: `review_reason_for` decides
    # from `duplicate_resolution`, and the later confidence pass overwrites
    # `requires_review` from that reason. The reader was shown one number and
    # never told the document disagreed with it.
    for fact in (anchor, other):
        fact.duplicate_resolution = DuplicateResolution.GENUINE_CONFLICT
        fact.requires_review = True

    detail = {
        "reason": "The same measure, entity and period carry two different "
                  "values in this document.",
        "values": [
            {"fact_id": f.fact_id, "value": f.value, "page": f.source_page,
             "confidence": f.confidence, "strategy": _strategy_of(f),
             "printed_label": f.metric,
             "original_text": (f.original_text or "")[:120]}
            for f in (anchor, other)],
        "resolution": "unresolved -- both retained for human review",
    }
    for fact, counterpart in ((anchor, other), (other, anchor)):
        fact.meta.setdefault("conflicts", []).append({
            "fact_id": counterpart.fact_id,
            "value": counterpart.value,
            "page": counterpart.source_page,
            "strategy": _strategy_of(counterpart),
            "printed_label": counterpart.metric,
            "currency": str(counterpart.currency or ""),
            "unit": str(counterpart.unit or ""),
        })
    result.relations.append(FactRelation(
        id=stable_id("rel", document_id, anchor.fact_id, other.fact_id,
                     "conflicts_with"),
        document_id=document_id, from_fact_id=anchor.fact_id,
        to_fact_id=other.fact_id, kind=RelationKind.CONFLICTS_WITH,
        detail=detail, confidence=1.0))
    result.conflicts.append({
        "metric": anchor.canonical_metric,
        "metric_display": anchor.metric_display or anchor.metric,
        "entity": anchor.entity, "period": anchor.period,
        "severity": str(Severity.HIGH),
        **detail,
    })


def reconcile(document_id: str, facts: list[FinancialFact]) -> Reconciliation:
    """Group every candidate by identity and classify every collision."""
    buckets: dict[FactIdentity, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        buckets[identity_of(f)].append(f)

    result = Reconciliation(facts=[])
    counts: dict[str, int] = defaultdict(int)

    for group in buckets.values():
        if len(group) == 1:
            result.facts.append(group[0])
            continue

        ordered = _anchor_first(group)
        anchor, rest = ordered[0], ordered[1:]
        survivors: list[FinancialFact] = []
        agreeing: list[FinancialFact] = []

        for other in rest:
            verdict = classify(anchor, other)
            counts[str(verdict)] += 1
            if verdict == DuplicateResolution.IDENTICAL_REPEAT:
                other.duplicate_resolution = verdict
                agreeing.append(other)
                continue
            other.duplicate_resolution = verdict
            if verdict == DuplicateResolution.GENUINE_CONFLICT:
                _record_conflict(document_id, anchor, other, result)
            elif (verdict == DuplicateResolution.DIFFERENT_UNIT
                    and _same_printed_number(anchor, other)):
                # Not a contradiction, but not resolved either: one of the two
                # scales is wrong and nothing here can say which.
                for fact, counterpart in ((anchor, other), (other, anchor)):
                    fact.meta["scale_disagreement"] = {
                        "fact_id": counterpart.fact_id,
                        "page": counterpart.source_page,
                        "unit": str(counterpart.unit or ""),
                        "strategy": _strategy_of(counterpart),
                    }
                if anchor.duplicate_resolution is None:
                    anchor.duplicate_resolution = DuplicateResolution.DIFFERENT_UNIT
            survivors.append(other)

        # ---- corroboration: one surviving fact, provenance of all copies ----
        if agreeing:
            # A conflict verdict on the anchor is categorical and outranks a
            # repeat: a figure that two sources confirm and a third contradicts
            # still needs a person.
            if anchor.duplicate_resolution != DuplicateResolution.GENUINE_CONFLICT:
                anchor.duplicate_resolution = DuplicateResolution.IDENTICAL_REPEAT
            sources = sorted({_strategy_of(f) for f in [anchor, *agreeing]})
            anchor.meta["duplicate_count"] = len(agreeing) + 1
            anchor.meta["duplicate_pages"] = sorted(
                {f.source_page for f in [anchor, *agreeing] if f.source_page})
            anchor.meta["duplicate_fact_ids"] = [f.fact_id for f in agreeing]
            anchor.meta["corroborating_sources"] = sources
            anchor.meta["corroboration"] = [
                {"fact_id": f.fact_id, "page": f.source_page,
                 "strategy": _strategy_of(f), "printed_label": f.metric,
                 "value": f.value, "unit": str(f.unit or ""),
                 "currency": str(f.currency or "")}
                for f in agreeing]
            note = next((scale_note(anchor, f) for f in agreeing
                         if scale_note(anchor, f)), None)
            if note:
                anchor.meta["corroboration_note"] = note
            # Independent *sources*, not copies: three rows of one table saying
            # the same thing is one strategy agreeing with itself.
            result.corroboration[anchor.fact_id] = len(sources) - 1
            result.merged += len(agreeing)

        for other in agreeing:
            result.relations.append(FactRelation(
                id=stable_id("rel", document_id, other.fact_id, anchor.fact_id,
                             "duplicate_of"),
                document_id=document_id, from_fact_id=other.fact_id,
                to_fact_id=anchor.fact_id, kind=RelationKind.DUPLICATE_OF,
                detail={"reason": "the same value in the same slot",
                        "strategy": _strategy_of(other),
                        "pages": [other.source_page, anchor.source_page]},
                confidence=0.95))

        result.facts.append(anchor)
        result.facts.extend(survivors)

    result.classified = dict(counts)
    log.info("reconcile.done", document_id=document_id,
             kept=len(result.facts), merged=result.merged,
             conflicts=len(result.conflicts), classified=result.classified)
    return result


def cross_metric_conflicts(document_id: str, facts: list[FinancialFact],
                           ) -> list[FactRelation]:
    """Link near-neighbour metrics the taxonomy keeps deliberately distinct.

    These are *not* conflicts -- they are different measures that a careless
    merge would have fused.  Recording the link lets the UI show them side by
    side and explain why they were kept apart.
    """
    out: list[FactRelation] = []
    by_slot: dict[tuple, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.canonical_metric and f.period:
            by_slot[(f.entity_id or f.entity or "", f.period)].append(f)

    for members in by_slot.values():
        for a in members:
            definition = taxonomy.get(a.canonical_metric or "")
            if definition is None or not definition.distinct_from:
                continue
            for b in members:
                if b.canonical_metric not in definition.distinct_from:
                    continue
                if a.value_type != b.value_type:
                    continue
                out.append(FactRelation(
                    id=stable_id("rel", document_id, a.fact_id, b.fact_id,
                                 "distinct"),
                    document_id=document_id, from_fact_id=a.fact_id,
                    to_fact_id=b.fact_id, kind=RelationKind.RESTATES,
                    detail={"relationship": "distinct_measure",
                            "explanation":
                                f"{definition.display} and "
                                f"{taxonomy.get(b.canonical_metric).display} are "
                                f"separate measures; they are never merged.",
                            "difference": (round(a.value - b.value, 4)
                                           if a.value is not None
                                           and b.value is not None else None)},
                    confidence=0.9))
    return out
