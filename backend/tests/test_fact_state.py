"""The evidence model: dimensions kept apart, and the weakest one deciding.

A single blended confidence lets excellent recognition hide poor attribution,
and that is the failure this project keeps finding: a figure read perfectly
from a clean page is worth nothing if its column was never matched to a
heading. So each dimension is scored separately and the weakest decides.

These tests hold that rule to its consequences, including the uncomfortable
ones -- an accounting check that passes must not rescue a fact with no period.
"""
from __future__ import annotations

import pytest

from app.extraction.fact_state import Evidence, FactState, Level, assess, state_of
from app.models.enums import (
    Currency, MagnitudeUnit, ValidationStatus, ValueType,
)


class _Prov:
    page_number = 6
    bbox = object()


class Fact:
    """The smallest thing the evidence model looks at."""

    def __init__(self, **kw):
        self.metric = kw.get("metric", "Current tax")
        self.canonical_metric = kw.get("canonical_metric", "current_tax")
        self.value = kw.get("value", 260.40)
        self.confidence = kw.get("confidence", 0.92)
        self.period = kw.get("period", "Q1 FY27")
        self.period_normalized = kw.get("period_normalized", _Period("stated"))
        self.entity = kw.get("entity", "Consolidated")
        self.entity_id = kw.get("entity_id", "ent_1")
        self.value_type = kw.get("value_type", ValueType.MONETARY)
        self.unit = kw.get("unit", MagnitudeUnit.CRORE)
        self.currency = kw.get("currency", Currency.INR)
        self.unit_inferred = kw.get("unit_inferred", False)
        self.currency_inferred = kw.get("currency_inferred", False)
        self.validation_status = kw.get("validation_status",
                                        ValidationStatus.NOT_CHECKED)
        self.corroborating_sources = kw.get("corroborating_sources", [])
        self.requires_review = kw.get("requires_review", False)
        self.duplicate_resolution = kw.get("duplicate_resolution")
        self.kind = "table_cell"
        self.provenance = _Prov()
        self.meta = {"metric_match_method": kw.get("method", "exact"),
                     "row": 3, "col": 2, **kw.get("meta", {})}


class _Period:
    def __init__(self, basis, period_type="quarter"):
        self.basis = basis
        self.period_type = period_type


def describe(fact, *, period_mandatory=True):
    ev = assess(fact, period_mandatory=period_mandatory)
    return state_of(fact, ev), ev


class TestTheWeakestDimensionDecides:

    def test_a_fully_established_fact_is_high_confidence(self):
        state, _ = describe(Fact())
        assert state == FactState.HIGH_CONFIDENCE

    def test_an_accounting_check_promotes_it_to_validated(self):
        state, _ = describe(Fact(validation_status=ValidationStatus.PASSED))
        assert state == FactState.VALIDATED

    def test_a_missing_period_overrides_everything_else(self):
        """The rule the phase exists for, stated directly.

        Perfect recognition, a known metric, a known entity, a known unit --
        and no period. It is not a high-confidence financial fact.
        """
        state, ev = describe(Fact(period=None, period_normalized=None,
                                  confidence=0.99))
        assert state == FactState.UNKNOWN
        assert ev.value == Level.HIGH
        assert ev.period == Level.UNKNOWN
        assert ev.limiting == "period"

    def test_an_accounting_pass_cannot_rescue_a_missing_period(self):
        """Arithmetic confirms a figure; it does not say which quarter it is."""
        state, _ = describe(Fact(period=None, period_normalized=None,
                                 validation_status=ValidationStatus.PASSED))
        assert state == FactState.UNKNOWN

    def test_a_missing_entity_holds_it_back(self):
        state, ev = describe(Fact(entity=None, entity_id=None))
        assert state == FactState.UNKNOWN
        assert ev.limiting == "entity"

    def test_a_monetary_fact_with_no_scale_is_unknown(self):
        state, ev = describe(Fact(unit=MagnitudeUnit.NONE))
        assert ev.unit == Level.UNKNOWN
        assert state == FactState.UNKNOWN


class TestWhatIsNotAGap:

    def test_a_segment_table_fact_owes_no_period(self):
        state, ev = describe(Fact(period=None, period_normalized=None),
                             period_mandatory=False)
        assert "period" in ev.not_applicable
        assert state == FactState.HIGH_CONFIDENCE

    def test_a_percentage_needs_no_currency(self):
        state, _ = describe(Fact(value_type=ValueType.PERCENTAGE,
                                 unit=MagnitudeUnit.PERCENT,
                                 currency=Currency.UNKNOWN))
        assert state == FactState.HIGH_CONFIDENCE

    def test_a_per_share_figure_needs_no_scale(self):
        state, _ = describe(Fact(value_type=ValueType.PER_SHARE,
                                 unit=MagnitudeUnit.NONE))
        assert state == FactState.HIGH_CONFIDENCE


class TestAccountingIsEvidenceNotAuthority:

    def test_a_failed_check_on_an_otherwise_sound_fact_asks_for_review(self):
        """Everything is established and the arithmetic still disagrees.

        That is worth a person's attention precisely because nothing else
        looks wrong -- it is how a silent parsing error was found.
        """
        state, _ = describe(Fact(validation_status=ValidationStatus.FAILED))
        assert state == FactState.REQUIRES_REVIEW

    def test_a_check_never_alters_the_value(self):
        fact = Fact(validation_status=ValidationStatus.FAILED)
        before = fact.value
        describe(fact)
        assert fact.value == before


class TestOcrConfidenceCannotMaskAttribution:

    @pytest.mark.parametrize("confidence", [0.99, 0.95, 0.90])
    def test_however_good_the_recognition(self, confidence):
        state, _ = describe(Fact(confidence=confidence, period=None,
                                 period_normalized=None))
        assert state == FactState.UNKNOWN

    def test_poor_recognition_alone_asks_for_review(self):
        state, ev = describe(Fact(confidence=0.4))
        assert ev.value == Level.LOW
        assert state == FactState.REQUIRES_REVIEW


class TestPhase22Dimensions:
    """Duration, currency and structure are dimensions of their own, and the
    value is the reading of the digits."""

    def test_an_unknown_span_holds_it_back(self):
        state, ev = describe(Fact(period_normalized=_Period("stated", "unknown")))
        assert ev.duration == Level.UNKNOWN
        assert state == FactState.UNKNOWN and ev.limiting == "duration"

    def test_a_currency_read_from_magnitude_words_is_medium(self):
        state, ev = describe(Fact(currency_inferred=True))
        assert ev.currency == Level.MEDIUM and ev.unit == Level.HIGH
        assert state == FactState.MEDIUM_CONFIDENCE and ev.limiting == "currency"

    def test_a_second_read_that_disagrees_asks_for_review(self):
        state, ev = describe(Fact(meta={"second_read": {"agrees": False}}))
        assert ev.value == Level.LOW
        assert state == FactState.REQUIRES_REVIEW

    def test_a_flagged_fact_is_review_even_when_incomplete(self):
        state, _ = describe(Fact(period=None, period_normalized=None,
                                 requires_review=True))
        assert state == FactState.REQUIRES_REVIEW

    def test_a_contradicted_fact_is_never_validated(self):
        state, _ = describe(Fact(validation_status=ValidationStatus.PASSED,
                                 duplicate_resolution="genuine_conflict"))
        assert state != FactState.VALIDATED

    def test_a_held_column_is_weak_structure(self):
        state, ev = describe(Fact(meta={"safety_gate": {"passed": False}}))
        assert ev.structure == Level.LOW and state == FactState.REQUIRES_REVIEW

    def test_corroboration_is_read_where_reconciliation_records_it(self):
        _, ev = describe(Fact(meta={"corroborating_sources": ["spatial_kpi", "table_cell"]}))
        assert ev.corroboration == Level.MEDIUM
