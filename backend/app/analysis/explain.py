"""Fact explanation.

Answers, for any single extracted number, the question a reviewer actually
asks: *why does the system believe this?*

The answer is assembled from what the pipeline already recorded -- the page and
bounding box it came from, the row label that gave it meaning, the column header
that dated it, the unit declaration that scaled it, the entity it belongs to,
the checks it passed, and every confidence component with its weight.  It is
prose produced by templates over stored evidence, so the same fact always yields
the same explanation and every clause can be checked against the page.

There is no language model here and no API call.  A sentence that cannot be
grounded in a recorded value is not written at all.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.extraction import taxonomy
from app.extraction.confidence import WEIGHTS
from app.models.enums import (
    ConfidenceBand, Currency, DerivationStatus, DuplicateResolution,
    EntityType, MagnitudeUnit, ValidationStatus, ValueType,
)
from app.models.schemas import (
    ConfidenceComponent, DerivedMetric, FactExplanation, FactRelation,
    FinancialFact, ValidationResult,
)

log = get_logger("analysis.explain")

# What each confidence channel means, in a reviewer's terms.
_CHANNEL_REASON = {
    "ocr": "How clearly the characters were recognised in the page image.",
    "layout": "How confidently the region was classified on the page.",
    "structure": "How well the value sits inside a reconstructed table grid.",
    "semantic": "How closely the row label matches a known financial metric.",
    "consistency": "Whether the value agrees with related figures.",
    "corroboration": "Whether an independent signal agreed with the reading.",
}

_RESOLUTION_REASON = {
    DuplicateResolution.IDENTICAL_REPEAT:
        "The same value was found elsewhere in the document and merged into "
        "this fact; the agreement raised confidence.",
    DuplicateResolution.DIFFERENT_ENTITY:
        "A similar figure exists for a different reporting entity. The two "
        "were kept separate rather than merged.",
    DuplicateResolution.DIFFERENT_PERIOD:
        "A similar figure exists for a different period. The two were kept "
        "separate rather than merged.",
    DuplicateResolution.DIFFERENT_DEFINITION:
        "A near-neighbour metric carries a similar value, but the taxonomy "
        "keeps the two measures deliberately distinct.",
    DuplicateResolution.DIFFERENT_UNIT:
        "The same measure appears in another unit or currency. No silent "
        "conversion was applied.",
    DuplicateResolution.DIFFERENT_SCOPE:
        "A similar figure exists at a different level of aggregation.",
    DuplicateResolution.GENUINE_CONFLICT:
        "Another figure in this document contradicts this one for the same "
        "measure, entity, period and unit. Both were kept and both flagged; "
        "FinSight does not choose between contradictory source figures.",
    DuplicateResolution.UNCERTAIN:
        "A similar figure could not be conclusively distinguished from this "
        "one, so both were kept.",
}


def format_value(fact: FinancialFact) -> str:
    if fact.value is None:
        return "no numeric value"
    if fact.value_type == ValueType.PERCENTAGE:
        return f"{fact.value:,.2f}%"
    if fact.value_type == ValueType.RATIO:
        return f"{fact.value:,.2f}x"
    if fact.value_type == ValueType.COUNT:
        return f"{fact.value:,.0f}"
    parts: list[str] = []
    if fact.currency and fact.currency != Currency.UNKNOWN:
        parts.append(str(fact.currency))
    parts.append(f"{fact.value:,.2f}")
    if fact.unit and fact.unit != MagnitudeUnit.NONE:
        parts.append(str(fact.unit))
    return " ".join(parts)


def _components(fact: FinancialFact) -> list[ConfidenceComponent]:
    """Break the score into named, weighted, individually explained parts.

    Nothing is averaged into a single opaque number: each channel keeps its own
    value and the weight it actually carried.
    """
    breakdown = fact.confidence_breakdown
    if breakdown is None:
        return [ConfidenceComponent(
            name="overall", value=round(fact.confidence, 4), weight=1.0,
            reason="No component breakdown was recorded for this fact.")]

    out: list[ConfidenceComponent] = []
    for name, weight in WEIGHTS.items():
        value = getattr(breakdown, name, None)
        if value is None:
            continue
        out.append(ConfidenceComponent(
            name=name, value=round(float(value), 4), weight=weight,
            reason=_CHANNEL_REASON.get(name, "")))

    duplicates = int(fact.meta.get("duplicate_count") or 0)
    if duplicates > 1:
        pages = ", ".join(str(p) for p in (fact.meta.get("duplicate_pages") or []))
        out.append(ConfidenceComponent(
            name="repetition", value=1.0, weight=0.0,
            reason=(f"The same value appears {duplicates} times in this "
                    f"document (pages {pages}), which raised confidence above "
                    f"the weighted base score.")))

    if fact.validation_status != ValidationStatus.NOT_CHECKED:
        passed = fact.validation_status == ValidationStatus.PASSED
        out.append(ConfidenceComponent(
            name="validation", value=1.0 if passed else 0.0, weight=0.0,
            reason=(f"Arithmetic validation returned "
                    f"{fact.validation_status}, which adjusted the final "
                    f"score after the weighted channels were combined.")))

    if fact.unit_inferred:
        out.append(ConfidenceComponent(
            name="unit_inference", value=0.0, weight=0.0,
            reason=("The magnitude was inherited from a document-level default "
                    "rather than declared by this table, which lowers "
                    "confidence in the scale of the figure.")))
    if fact.currency_inferred:
        out.append(ConfidenceComponent(
            name="currency_inference", value=0.0, weight=0.0,
            reason=("The currency was inferred from magnitude words read in "
                    "the page image rather than from a currency symbol, which "
                    "lowers confidence in the currency.")))
    return out


def _why_metric(fact: FinancialFact) -> str:
    definition = taxonomy.get(fact.canonical_metric or "")
    if definition is None:
        return (f'The row label "{fact.metric}" did not match any metric in the '
                f'taxonomy, so the figure is reported under the name the '
                f'document printed.')
    parts = [f'The row label "{fact.metric}" was matched to '
             f'{definition.display}.']
    if definition.definition:
        parts.append(definition.definition)
    if definition.distinct_from:
        neighbours = ", ".join(
            (taxonomy.get(k).display if taxonomy.get(k) else k)
            for k in definition.distinct_from)
        parts.append(f"This measure is held distinct from {neighbours}; "
                     f"FinSight never merges them, because they are different "
                     f"measures even when their values are close.")
    return " ".join(parts)


def _why_period(fact: FinancialFact) -> str:
    if not fact.period:
        return ("No column resolved to a reporting period, so this value "
                "carries no period attribution. It is excluded from "
                "period-based comparisons and from validation, because an "
                "unattributed figure cannot safely be compared with a dated "
                "one.")
    period = fact.period_normalized
    if period is None:
        return f'The column header dated this value to {fact.period}.'
    basis = str(period.period_type)
    detail = (f'The column header "{period.raw_text}" resolved to '
              f'{fact.period} ({basis}')
    if period.fiscal_year:
        detail += f", fiscal year {period.fiscal_year}"
    if period.quarter:
        detail += f", quarter {period.quarter}"
    return detail + ")."


def _why_entity(fact: FinancialFact) -> str:
    if not fact.entity:
        return ("No reporting entity was resolved for this figure, so it is "
                "not attributed to one.")
    described = {
        EntityType.CONSOLIDATED: "the consolidated group",
        EntityType.STANDALONE: "the standalone parent entity",
        EntityType.SEGMENT: f'the reporting segment "{fact.entity}"',
        EntityType.SUBSIDIARY: f'the subsidiary "{fact.entity}"',
        EntityType.JOINT_VENTURE: f'the joint venture "{fact.entity}"',
    }.get(EntityType(str(fact.entity_type)), f'the reporting unit '
                                             f'"{fact.entity}"')
    text = f"This value is attributed to {described}."
    if fact.parent_entity_id:
        text += (" It rolls up under a parent entity, so it is never summed "
                 "together with that parent's own figures.")
    if fact.meta.get("entity_overridden"):
        text += (" The sentence named this entity explicitly, overriding the "
                 "entity of the section it appears in.")
    return text


def _why_unit(fact: FinancialFact) -> str:
    if fact.value_type != ValueType.MONETARY:
        return (f"This is a {fact.value_type} value, so no currency or "
                f"magnitude applies.")
    parts: list[str] = []
    if fact.unit == MagnitudeUnit.NONE:
        parts.append("No magnitude was declared, so the figure is taken at "
                     "face value.")
    elif fact.unit_inferred:
        parts.append(f"The magnitude {fact.unit} was inherited from the "
                     f"document-level default; this table did not declare its "
                     f"own.")
    else:
        parts.append(f"The magnitude {fact.unit} came from a unit declaration "
                     f"belonging to this table, not from surrounding prose.")
    if fact.currency == Currency.UNKNOWN:
        parts.append("No currency could be established, so none is asserted.")
    elif fact.currency_inferred:
        parts.append(f"The currency {fact.currency} was inferred from magnitude "
                     f"words in the page image rather than a currency symbol.")
    else:
        parts.append(f"The currency {fact.currency} was read from the source.")
    if fact.base_value is not None:
        parts.append(f"Normalised to base units this is {fact.base_value:,.0f}, "
                     f"which is what comparisons and ratios use.")
    return " ".join(parts)


def _derivation_note(fact: FinancialFact,
                     derived: DerivedMetric | None) -> tuple[str, list[dict]]:
    inputs: list[dict] = []
    if fact.derivation == DerivationStatus.CALCULATED:
        note = ("This figure was calculated by FinSight"
                + (f" as {fact.formula}." if fact.formula else "."))
        inputs = [{"fact_id": fid, "role": "input"}
                  for fid in fact.input_fact_ids]
    else:
        note = ("This figure was reported by the document. FinSight did not "
                "compute it.")

    if fact.growth is not None:
        source = ("stated by the document itself" if fact.growth_reported
                  else "computed by FinSight from the two period values")
        note += (f" The {fact.growth:+,.2f}% change carried on this fact was "
                 f"{source}.")
        if fact.growth_reported and fact.growth_calculated is not None:
            agrees = abs(fact.growth - fact.growth_calculated) <= 0.5
            note += (f" Recomputing it from the two period values gives "
                     f"{fact.growth_calculated:+,.2f}%, which "
                     + ("agrees with the printed figure."
                        if agrees else
                        "does not agree with the printed figure; both are "
                        "retained so the discrepancy stays visible."))

    if derived is not None:
        verdict = ("agrees with" if derived.agrees_with_reported
                   else "does not agree with")
        note += (f" Independently recomputing {derived.metric_display} as "
                 f"{derived.formula} from other reported figures gives "
                 f"{derived.value:,.2f}, which {verdict} this printed value.")
        inputs.extend({"fact_id": fid, "role": "recomputation_input"}
                      for fid in derived.input_fact_ids)
    return note, inputs


def explain_fact(fact: FinancialFact, *,
                 validations: list[ValidationResult] | None = None,
                 derived: list[DerivedMetric] | None = None,
                 relations: list[FactRelation] | None = None,
                 review_history: list[dict] | None = None) -> FactExplanation:
    """Build the complete, deterministic explanation of one fact."""
    mine = [v for v in (validations or []) if fact.fact_id in (v.fact_ids or [])]
    match = next((d for d in (derived or [])
                  if d.reported_fact_id == fact.fact_id), None)

    validation_summary = [
        {"rule": v.rule, "rule_family": v.rule_family,
         "status": str(v.status), "message": v.message,
         "reported_value": v.reported_value,
         "calculated_value": v.calculated_value,
         "difference": v.difference, "tolerance": v.tolerance}
        for v in mine
    ] or [{"rule": "none", "status": str(ValidationStatus.NOT_CHECKED),
           "message": "No arithmetic check applies to this fact. A check is "
                      "only run where the document supplies every figure the "
                      "rule needs, for the same entity, period and unit."}]

    related: list[dict] = []
    for relation in (relations or []):
        if relation.from_fact_id == fact.fact_id:
            related.append({"fact_id": relation.to_fact_id,
                            "kind": str(relation.kind),
                            "direction": "outgoing", **relation.detail})
        elif relation.to_fact_id == fact.fact_id:
            related.append({"fact_id": relation.from_fact_id,
                            "kind": str(relation.kind),
                            "direction": "incoming", **relation.detail})

    derivation_note, inputs = _derivation_note(fact, match)
    resolution = fact.duplicate_resolution
    if resolution is not None:
        reason = _RESOLUTION_REASON.get(DuplicateResolution(str(resolution)))
        if reason:
            derivation_note += " " + reason

    definition = taxonomy.get(fact.canonical_metric or "")

    return FactExplanation(
        fact_id=fact.fact_id, document_id=fact.document_id,
        metric=fact.metric,
        metric_display=fact.metric_display or fact.metric,
        canonical_metric=fact.canonical_metric or "",
        metric_definition=(definition.definition if definition else ""),
        value=fact.value, formatted_value=format_value(fact),
        entity_name=fact.entity, entity_type=fact.entity_type,
        period=fact.period,
        period_basis=(str(fact.period_normalized.period_type)
                      if fact.period_normalized else ""),
        unit=fact.unit, currency=fact.currency,
        source_page=fact.source_page or 0,
        why_this_metric=_why_metric(fact),
        why_this_period=_why_period(fact),
        why_this_entity=_why_entity(fact),
        why_this_unit=_why_unit(fact),
        confidence=fact.confidence,
        confidence_band=ConfidenceBand(str(fact.confidence_band)),
        confidence_components=_components(fact),
        validation_summary=validation_summary,
        derivation=fact.derivation, derivation_note=derivation_note,
        formula=fact.formula,
        inputs=inputs, related_facts=related[:25],
        provenance=fact.provenance,
        review_history=list(review_history or []),
        requires_review=fact.requires_review,
    )
