"""Evidence reconciliation: what makes two readings one fact, or two.

The tests are grouped the way the decisions are made -- identity first, then
comparability, then classification, then what reaches the reader. Each refusal
asserted here corresponds to a measurement in
``docs/EVIDENCE_RECONCILIATION.md``: before this layer existed, six of the
eight classification verdicts were unreachable, 78 % of one document's figures
were outside reconciliation entirely, and the surviving side of every
contradiction was presented as a clean high-confidence number.
"""
from __future__ import annotations

import pytest

from app.analysis.identity import (
    agrees, comparable, identity_of, printed_label,
)
from app.analysis.reconcile import classify, reconcile
from app.extraction import confidence as conf_mod
from app.models.enums import (
    ConfidenceBand, Currency, DuplicateResolution, FactKind, MagnitudeUnit,
    PeriodBasis, ReviewReason, StatementNature, ValidationStatus, ValueType,
)
from app.models.schemas import (
    BBoxModel, ConfidenceBreakdown, FinancialFact, NormalizedPeriod, Provenance,
)


def fact(fact_id: str, *, metric: str = "Revenue",
         canonical: str = "revenue", value: float | None = 100.0,
         period: str | None = "Q1 FY27", entity: str = "ent_1",
         unit: MagnitudeUnit = MagnitudeUnit.CRORE,
         currency: Currency = Currency.INR,
         value_type: ValueType = ValueType.MONETARY,
         base: float | None = None, strategy: str = "table_cell",
         page: int = 1, confidence: float = 0.9,
         period_type: str = "quarter") -> FinancialFact:
    """One candidate fact, with only what reconciliation looks at."""
    normalized = None
    if period:
        normalized = NormalizedPeriod(
            label=period, raw_text=period, period_type=period_type,
            confidence=0.9, basis=PeriodBasis.STATED)
    factor = {MagnitudeUnit.CRORE: 1e7, MagnitudeUnit.MILLION: 1e6,
              MagnitudeUnit.NONE: 1.0}.get(unit, 1.0)
    return FinancialFact(
        fact_id=fact_id, document_id="d1", metric=metric,
        canonical_metric=canonical, metric_display=canonical.title(),
        entity="Entity", entity_id=entity,
        kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
        value=value, raw_value=str(value), value_type=value_type,
        currency=currency, unit=unit,
        base_value=(base if base is not None
                    else (value * factor if value is not None else None)),
        period=period, period_normalized=normalized,
        confidence=confidence, confidence_band=ConfidenceBand.HIGH,
        confidence_breakdown=ConfidenceBreakdown(
            ocr=0.9, layout=0.9, structure=0.9, semantic=0.9,
            consistency=0.9, corroboration=0.0, overall=confidence,
            band=ConfidenceBand.HIGH, notes=[]),
        source_page=page, original_text=str(value), context_text=metric,
        provenance=Provenance(
            document_id="d1", page_number=page,
            bbox=BBoxModel(x=0, y=0, width=10, height=10),
            original_text=str(value), extraction_method=strategy))


# ------------------------------------------------------------------ identity
class TestFactIdentity:
    """The slot a figure occupies, deterministic and free of its value."""

    def test_identity_ignores_the_value(self):
        assert identity_of(fact("a", value=100.0)) == \
            identity_of(fact("b", value=999.0))

    def test_identity_ignores_the_printed_label(self):
        """'Depreciation' and 'Depreciation / Amortisation' are one line.

        Keying on the label put 36 such groups on one filing into separate
        buckets and lost the corroboration between them.
        """
        assert identity_of(fact("a", metric="Depreciation")) == \
            identity_of(fact("b", metric="Depreciation / Amortisation"))

    def test_identity_ignores_the_scale(self):
        assert identity_of(fact("a", unit=MagnitudeUnit.CRORE)) == \
            identity_of(fact("b", unit=MagnitudeUnit.MILLION))

    def test_identity_ignores_the_strategy(self):
        assert identity_of(fact("a", strategy="table_cell")) == \
            identity_of(fact("b", strategy="spatial_kpi"))

    def test_a_different_period_is_a_different_identity(self):
        assert identity_of(fact("a", period="Q1 FY27")) != \
            identity_of(fact("b", period="Q4 FY26"))

    def test_a_different_entity_is_a_different_identity(self):
        assert identity_of(fact("a", entity="ent_1")) != \
            identity_of(fact("b", entity="ent_2"))

    def test_a_different_metric_is_a_different_identity(self):
        assert identity_of(fact("a", canonical="revenue")) != \
            identity_of(fact("b", canonical="pat"))

    def test_identity_is_deterministic(self):
        a, b = fact("a"), fact("a")
        assert identity_of(a) == identity_of(b)
        assert hash(identity_of(a)) == hash(identity_of(b))


# ------------------------------------------------------------- comparability
class TestComparability:
    """Whether two figures can be held against each other at all."""

    def test_the_same_scale_compares_directly(self):
        assert comparable(fact("a"), fact("b"))

    def test_different_scales_compare_through_the_base_value(self):
        crore = fact("a", value=1.0, unit=MagnitudeUnit.CRORE)
        million = fact("b", value=10.0, unit=MagnitudeUnit.MILLION)
        assert comparable(crore, million)
        assert agrees(crore, million)

    def test_different_currencies_are_not_comparable(self):
        rupees = fact("a", currency=Currency.INR)
        dollars = fact("b", currency=Currency.USD)
        assert not comparable(rupees, dollars)

    def test_a_rate_and_an_amount_are_not_comparable(self):
        amount = fact("a", value_type=ValueType.MONETARY)
        rate = fact("b", value_type=ValueType.PERCENTAGE)
        assert not comparable(amount, rate)

    def test_an_unreadable_value_is_not_comparable(self):
        assert not comparable(fact("a"), fact("b", value=None))

    def test_rounding_within_half_a_percent_agrees(self):
        assert agrees(fact("a", value=1000.0), fact("b", value=1004.0))

    def test_a_real_difference_does_not_agree(self):
        assert not agrees(fact("a", value=1000.0), fact("b", value=1100.0))


# ---------------------------------------------------------- classification
class TestClassification:
    """All eight verdicts must be reachable; six were not."""

    def test_the_same_value_is_a_repeat(self):
        assert classify(fact("a"), fact("b")) == \
            DuplicateResolution.IDENTICAL_REPEAT

    def test_the_same_value_at_another_scale_is_a_repeat(self):
        crore = fact("a", value=1.0, unit=MagnitudeUnit.CRORE)
        million = fact("b", value=10.0, unit=MagnitudeUnit.MILLION)
        assert classify(crore, million) == \
            DuplicateResolution.IDENTICAL_REPEAT

    def test_a_different_value_in_the_same_slot_is_a_conflict(self):
        assert classify(fact("a", value=100.0), fact("b", value=200.0)) == \
            DuplicateResolution.GENUINE_CONFLICT

    def test_two_printed_line_items_are_different_definitions(self):
        """'Total current assets' and 'Other current assets' both map to
        ``current_assets`` and carry different numbers. They are separate rows
        of one balance sheet, not a disagreement about one row."""
        total = fact("a", metric="Total current assets", value=122491.0)
        other = fact("b", metric="Other current assets", value=14359.0)
        assert classify(total, other) == \
            DuplicateResolution.DIFFERENT_DEFINITION

    def test_two_currencies_are_a_unit_difference_not_a_conflict(self):
        rupees = fact("a", currency=Currency.INR, value=100.0)
        dollars = fact("b", currency=Currency.USD, value=1.2)
        assert classify(rupees, dollars) == DuplicateResolution.DIFFERENT_UNIT

    def test_a_rate_against_an_amount_is_a_unit_difference(self):
        assert classify(fact("a"), fact("b", value_type=ValueType.PERCENTAGE)) \
            == DuplicateResolution.DIFFERENT_UNIT

    def test_an_undated_disagreement_is_uncertain_not_a_conflict(self):
        """Two figures with no period may belong to different quarters.

        Calling that a contradiction would manufacture one out of a gap.
        """
        assert classify(fact("a", period=None, value=100.0),
                        fact("b", period=None, value=200.0)) == \
            DuplicateResolution.UNCERTAIN

    def test_an_undated_agreement_is_still_corroboration(self):
        assert classify(fact("a", period=None), fact("b", period=None)) == \
            DuplicateResolution.IDENTICAL_REPEAT

    def test_the_same_digits_at_two_scales_is_a_scale_problem(self):
        """286,788 million and 286,788 billion are one figure, misread once.

        The document does not disagree about the number; one of the two scales
        was picked up from the wrong heading. Calling it a value contradiction
        misstates what a reviewer has to settle.
        """
        a = fact("a", value=286788.0, unit=MagnitudeUnit.MILLION,
                 base=286788.0 * 1e6)
        b = fact("b", value=286788.0, unit=MagnitudeUnit.BILLION,
                 base=286788.0 * 1e9)
        assert classify(a, b) == DuplicateResolution.DIFFERENT_UNIT

    def test_a_scale_disagreement_still_reaches_review(self):
        a = fact("a", value=286788.0, unit=MagnitudeUnit.MILLION,
                 base=286788.0 * 1e6)
        b = fact("b", value=286788.0, unit=MagnitudeUnit.BILLION,
                 base=286788.0 * 1e9)
        out = reconcile("d1", [a, b])
        assert len(out.facts) == 2
        flagged = [f for f in out.facts if f.meta.get("scale_disagreement")]
        assert len(flagged) == 2
        reason = conf_mod.review_reason_for(flagged[0],
                                            ValidationStatus.PASSED)
        assert reason == ReviewReason.UNIT_UNDETERMINED

    def test_different_digits_at_two_scales_is_still_a_unit_difference(self):
        a = fact("a", value=100.0, unit=MagnitudeUnit.MILLION, base=1e8)
        b = fact("b", value=250.0, unit=MagnitudeUnit.BILLION, base=2.5e11)
        assert classify(a, b) == DuplicateResolution.GENUINE_CONFLICT

    def test_near_neighbour_metrics_are_a_scope_difference(self):
        a = fact("a", canonical="revenue", value=100.0)
        b = fact("b", canonical="revenue", value=200.0)
        b.canonical_metric = "gross_revenue"
        assert classify(a, b) in (DuplicateResolution.DIFFERENT_SCOPE,
                                  DuplicateResolution.DIFFERENT_DEFINITION)


# ------------------------------------------------------------ reconciliation
class TestReconciliation:

    def test_duplicates_merge_into_one_fact(self):
        out = reconcile("d1", [fact("a"), fact("b", strategy="spatial_kpi")])
        assert len(out.facts) == 1
        assert out.merged == 1

    def test_a_merged_fact_keeps_every_source(self):
        out = reconcile("d1", [fact("a", page=18),
                               fact("b", strategy="spatial_kpi", page=2)])
        kept = out.facts[0]
        assert kept.meta["duplicate_pages"] == [2, 18]
        assert set(kept.meta["corroborating_sources"]) == \
            {"table_cell", "spatial_kpi"}
        assert kept.meta["corroboration"][0]["page"] == 2

    def test_cross_strategy_agreement_counts_once_per_strategy(self):
        """Three rows of one table agreeing is one method agreeing with itself."""
        out = reconcile("d1", [fact("a"), fact("b"), fact("c")])
        assert out.corroboration[out.facts[0].fact_id] == 0

    def test_a_contradiction_keeps_both_facts(self):
        out = reconcile("d1", [fact("a", value=100.0),
                               fact("b", value=200.0)])
        assert len(out.facts) == 2

    def test_a_contradiction_flags_both_sides(self):
        """The survivor used to be presented as a clean high-confidence number.

        `review_reason_for` decides from `duplicate_resolution`, and the anchor's
        was never set, so the later confidence pass cleared its review flag.
        """
        out = reconcile("d1", [fact("a", value=100.0),
                               fact("b", value=200.0)])
        for f in out.facts:
            assert f.duplicate_resolution == DuplicateResolution.GENUINE_CONFLICT
            assert f.requires_review

    def test_a_contradiction_records_both_provenances(self):
        out = reconcile("d1", [fact("a", value=100.0, page=3),
                               fact("b", value=200.0, page=9)])
        pages = {c["page"] for f in out.facts for c in f.meta["conflicts"]}
        assert pages == {3, 9}

    def test_different_periods_never_conflict(self):
        out = reconcile("d1", [fact("a", period="Q1 FY27", value=100.0),
                               fact("b", period="Q4 FY26", value=200.0)])
        assert len(out.facts) == 2
        assert not any(f.requires_review for f in out.facts)

    def test_different_entities_never_conflict(self):
        out = reconcile("d1", [fact("a", entity="ent_1", value=100.0),
                               fact("b", entity="ent_2", value=200.0)])
        assert not any(f.requires_review for f in out.facts)

    def test_different_currencies_never_conflict(self):
        out = reconcile("d1", [fact("a", currency=Currency.INR, value=100.0),
                               fact("b", currency=Currency.USD, value=1.2)])
        assert not any(f.requires_review for f in out.facts)

    def test_the_strongest_strategy_survives_a_merge(self):
        out = reconcile("d1", [fact("a", strategy="narrative_sentence",
                                    confidence=0.99),
                               fact("b", strategy="table_cell",
                                    confidence=0.7)])
        assert out.facts[0].provenance.extraction_method == "table_cell"

    def test_reconciliation_is_deterministic(self):
        facts = [fact("a"), fact("b", strategy="spatial_kpi"),
                 fact("c", value=200.0)]
        first = [f.fact_id for f in reconcile("d1", list(facts)).facts]
        second = [f.fact_id for f in reconcile("d1", list(facts)).facts]
        assert first == second


# --------------------------------------------------------------- confidence
class TestConfidenceReflectsEvidence:

    def _breakdown(self, overall: float = 0.9) -> ConfidenceBreakdown:
        return ConfidenceBreakdown(
            ocr=0.9, layout=0.9, structure=0.9, semantic=0.9,
            consistency=0.9, corroboration=0.0, overall=overall,
            band=ConfidenceBand.HIGH, notes=[])

    def test_agreement_raises_confidence(self):
        base = self._breakdown()
        raised = conf_mod.apply_reconciliation(base, agreeing_sources=1)
        assert raised.overall > base.overall
        assert raised.corroboration > base.corroboration

    def test_a_contradiction_drives_a_fact_into_review(self):
        base = self._breakdown(0.95)
        capped = conf_mod.apply_reconciliation(base, contradicted=True)
        assert capped.overall < base.overall
        assert capped.band in (ConfidenceBand.LOW, ConfidenceBand.MEDIUM,
                               ConfidenceBand.REQUIRES_REVIEW)

    def test_agreement_cannot_average_a_contradiction_away(self):
        """A figure two sources confirm and a third disputes still needs a
        person. Corroboration and contradiction are not opposite ends of one
        scale; one is quantitative and the other categorical."""
        both = conf_mod.apply_reconciliation(
            self._breakdown(0.95), agreeing_sources=3, contradicted=True)
        assert both.overall < 0.9

    def test_nothing_changes_without_evidence(self):
        base = self._breakdown()
        assert conf_mod.apply_reconciliation(base) is base

    def test_the_review_reason_names_the_conflict(self):
        f = fact("a")
        f.duplicate_resolution = DuplicateResolution.GENUINE_CONFLICT
        reason = conf_mod.review_reason_for(f, ValidationStatus.PASSED)
        assert reason is not None
        assert "conflict" in str(reason)


# ---------------------------------------------------------------- provenance
class TestProvenanceSurvives:

    def test_every_surviving_fact_keeps_its_provenance(self):
        out = reconcile("d1", [fact("a"), fact("b", value=200.0),
                               fact("c", strategy="spatial_kpi")])
        for f in out.facts:
            assert f.provenance is not None
            assert f.provenance.page_number

    def test_a_merge_links_the_absorbed_fact(self):
        out = reconcile("d1", [fact("a"), fact("b", strategy="spatial_kpi")])
        links = [r for r in out.relations if str(r.kind) == "duplicate_of"]
        assert len(links) == 1

    def test_a_conflict_links_both_directions_of_evidence(self):
        out = reconcile("d1", [fact("a", value=100.0), fact("b", value=200.0)])
        conflicts = [r for r in out.relations if str(r.kind) == "conflicts_with"]
        assert len(conflicts) == 1
        assert len(conflicts[0].detail["values"]) == 2

    def test_the_printed_label_survives_a_merge(self):
        """The label leaves the *identity*, not the record. A reader still has
        to be able to find the row on the page."""
        out = reconcile("d1", [
            fact("a", metric="Depreciation"),
            fact("b", metric="Depreciation / Amortisation",
                 strategy="spatial_grid")])
        kept = out.facts[0]
        assert kept.metric
        labels = {c["printed_label"] for c in kept.meta["corroboration"]}
        assert "Depreciation / Amortisation" in labels
