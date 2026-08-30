"""Financial validation engine."""
from __future__ import annotations

import pytest

from app.models.enums import (
    Currency, MagnitudeUnit, StatementNature, ValidationStatus, ValueType,
)
from app.models.schemas import FinancialFact
from app.validation.engine import validate


def fact(fid, canonical, value, period, *, metric=None, unit=MagnitudeUnit.CRORE,
         vtype=ValueType.MONETARY, currency=Currency.INR, growth=None,
         growth_reported=False, cmp_period=None, cmp_value=None,
         entity="Consolidated", page=1) -> FinancialFact:
    return FinancialFact(
        fact_id=fid, document_id="d1", metric=metric or canonical,
        canonical_metric=canonical, metric_display=metric or canonical,
        entity=entity, value=value, period=period, unit=unit, value_type=vtype,
        currency=currency, growth=growth, growth_reported=growth_reported,
        comparison_period=cmp_period, comparison_value=cmp_value,
        source_page=page, confidence=0.9, nature=StatementNature.ACTUAL)


def statuses(report, family=None):
    return [r.status for r in report.results
            if family is None or r.rule_family == family]


class TestGrowthChecks:
    def test_consistent_growth_passes(self):
        r = validate("d1", [fact("f1", "gross_revenue", 12450, "Q1 FY27",
                                 growth=14.1, growth_reported=True,
                                 cmp_period="Q1 FY26", cmp_value=10910)])
        assert ValidationStatus.PASSED in statuses(r, "growth")

    def test_inconsistent_growth_is_flagged_not_corrected(self):
        f = fact("f1", "gross_revenue", 12450, "Q1 FY27", growth=99.9,
                 growth_reported=True, cmp_period="Q1 FY26", cmp_value=10910)
        r = validate("d1", [f])
        failing = [x for x in r.results if x.rule == "growth_matches_values"]
        assert failing and failing[0].status == ValidationStatus.FAILED
        # both numbers are retained; the document is never rewritten
        assert failing[0].reported_value == pytest.approx(99.9)
        assert failing[0].calculated_value == pytest.approx(14.11, abs=0.05)
        assert f.growth == 99.9

    def test_small_rounding_difference_still_passes(self):
        r = validate("d1", [fact("f1", "ebitda", 2490, "Q1 FY27", growth=14.2,
                                 growth_reported=True, cmp_period="Q1 FY26",
                                 cmp_value=2180)])
        assert ValidationStatus.PASSED in statuses(r, "growth")

    def test_unreported_growth_is_not_checked(self):
        r = validate("d1", [fact("f1", "ebitda", 2490, "Q1 FY27", growth=14.2,
                                 growth_reported=False, cmp_period="Q1 FY26",
                                 cmp_value=2180)])
        assert not statuses(r, "growth")


class TestMarginChecks:
    def test_margin_matches_components(self):
        facts = [
            fact("f1", "ebitda", 2490, "Q1 FY27"),
            fact("f2", "gross_revenue", 12450, "Q1 FY27"),
            fact("f3", "ebitda_margin", 20.0, "Q1 FY27",
                 unit=MagnitudeUnit.PERCENT, vtype=ValueType.PERCENTAGE,
                 currency=Currency.UNKNOWN),
        ]
        r = validate("d1", facts)
        assert ValidationStatus.PASSED in statuses(r, "margin")

    def test_wrong_margin_is_flagged(self):
        facts = [
            fact("f1", "ebitda", 2490, "Q1 FY27"),
            fact("f2", "gross_revenue", 12450, "Q1 FY27"),
            fact("f3", "ebitda_margin", 45.0, "Q1 FY27",
                 unit=MagnitudeUnit.PERCENT, vtype=ValueType.PERCENTAGE,
                 currency=Currency.UNKNOWN),
        ]
        r = validate("d1", facts)
        assert ValidationStatus.FAILED in statuses(r, "margin")

    def test_different_entities_are_never_mixed(self):
        """A subsidiary's EBITDA must not be divided by the group's revenue."""
        facts = [
            fact("f1", "ebitda", 2490, "Q1 FY27", entity="SubCo"),
            fact("f2", "gross_revenue", 999999, "Q1 FY27", entity="Consolidated"),
            fact("f3", "ebitda_margin", 20.0, "Q1 FY27", entity="SubCo",
                 unit=MagnitudeUnit.PERCENT, vtype=ValueType.PERCENTAGE,
                 currency=Currency.UNKNOWN),
        ]
        r = validate("d1", facts)
        assert not statuses(r, "margin")   # no comparable pair exists


class TestIdentityChecks:
    def test_pbt_minus_tax_equals_pat(self):
        facts = [fact("f1", "pbt", 1590, "Q1 FY27"),
                 fact("f2", "tax_expense", 390, "Q1 FY27"),
                 fact("f3", "pat", 1200, "Q1 FY27")]
        r = validate("d1", facts)
        derived = [x for x in r.results if x.rule == "pbt_less_tax_is_pat"]
        assert len(derived) == 1              # emitted once, not once per metric
        assert derived[0].status == ValidationStatus.PASSED

    def test_debt_minus_cash_equals_net_debt(self):
        facts = [fact("f1", "outstanding_debt", 369705, "Q1 FY27"),
                 fact("f2", "cash_and_equivalents", 246791, "Q1 FY27"),
                 fact("f3", "net_debt", 122914, "Q1 FY27")]
        r = validate("d1", facts)
        derived = [x for x in r.results if x.rule == "debt_less_cash_is_net_debt"]
        assert derived and derived[0].status == ValidationStatus.PASSED

    def test_broken_identity_is_reported(self):
        facts = [fact("f1", "pbt", 1590, "Q1 FY27"),
                 fact("f2", "tax_expense", 390, "Q1 FY27"),
                 fact("f3", "pat", 800, "Q1 FY27")]
        r = validate("d1", facts)
        derived = [x for x in r.results if x.rule == "pbt_less_tax_is_pat"]
        assert derived[0].status in (ValidationStatus.WARNING,
                                     ValidationStatus.FAILED)
        assert derived[0].reported_value == 800
        assert derived[0].calculated_value == 1200


class TestUnitSafety:
    def test_mismatched_units_are_converted_not_ignored(self):
        facts = [fact("f1", "ebitda", 2490, "Q1 FY27", unit=MagnitudeUnit.CRORE),
                 fact("f2", "gross_revenue", 124500, "Q1 FY27",
                      unit=MagnitudeUnit.MILLION),
                 fact("f3", "ebitda_margin", 20.0, "Q1 FY27",
                      unit=MagnitudeUnit.PERCENT, vtype=ValueType.PERCENTAGE,
                      currency=Currency.UNKNOWN)]
        r = validate("d1", facts)
        margin = [x for x in r.results if x.rule_family == "margin"]
        # 124,500 million == 12,450 crore, so the margin is 20% and passes
        assert margin and margin[0].status == ValidationStatus.PASSED

    def test_incomparable_currencies_are_refused(self):
        facts = [fact("f1", "ebitda", 2490, "Q1 FY27", currency=Currency.INR),
                 fact("f2", "gross_revenue", 12450, "Q1 FY27",
                      currency=Currency.USD),
                 fact("f3", "ebitda_margin", 20.0, "Q1 FY27",
                      unit=MagnitudeUnit.PERCENT, vtype=ValueType.PERCENTAGE,
                      currency=Currency.UNKNOWN)]
        r = validate("d1", facts)
        margin = [x for x in r.results if x.rule_family == "margin"]
        assert all(m.status != ValidationStatus.FAILED for m in margin)


class TestSanityChecks:
    def test_negative_positive_metric_warns(self):
        r = validate("d1", [fact("f1", "depreciation", -600, "Q1 FY27")])
        assert any(x.rule == "unexpected_negative" for x in r.results)

    def test_implausible_percentage_warns(self):
        r = validate("d1", [fact("f1", "ebitda_margin", 950.0, "Q1 FY27",
                                 unit=MagnitudeUnit.PERCENT,
                                 vtype=ValueType.PERCENTAGE,
                                 currency=Currency.UNKNOWN)])
        assert any(x.rule == "implausible_percentage" for x in r.results)

    def test_missing_period_is_surfaced(self):
        r = validate("d1", [fact("f1", "ebitda", 100, None)])
        assert any(x.rule == "facts_without_period" for x in r.results)


class TestAggregation:
    def test_segments_below_total_is_acceptable(self):
        facts = [fact("s1", "segment_revenue", 400, "Q1 FY27", entity="A"),
                 fact("s2", "segment_revenue", 500, "Q1 FY27", entity="B"),
                 fact("t", "segment_revenue", 950, "Q1 FY27",
                      entity="Consolidated")]
        r = validate("d1", facts)
        agg = [x for x in r.results if x.rule_family == "aggregation"]
        assert agg and agg[0].status == ValidationStatus.PASSED

    def test_segments_exceeding_total_warns(self):
        facts = [fact("s1", "segment_revenue", 800, "Q1 FY27", entity="A"),
                 fact("s2", "segment_revenue", 900, "Q1 FY27", entity="B"),
                 fact("t", "segment_revenue", 950, "Q1 FY27",
                      entity="Consolidated")]
        r = validate("d1", facts)
        agg = [x for x in r.results if x.rule_family == "aggregation"]
        assert agg and agg[0].status == ValidationStatus.WARNING


class TestRollup:
    def test_status_rolls_up_worst_wins(self):
        good = fact("f1", "gross_revenue", 12450, "Q1 FY27", growth=14.1,
                    growth_reported=True, cmp_period="Q1 FY26", cmp_value=10910)
        bad = fact("f2", "finance_costs", 300, "Q1 FY27", growth=99.0,
                   growth_reported=True, cmp_period="Q1 FY26", cmp_value=280)
        r = validate("d1", [good, bad])
        assert r.fact_status["f1"] == ValidationStatus.PASSED
        assert r.fact_status["f2"] == ValidationStatus.FAILED

    def test_a_broken_rule_does_not_kill_the_run(self, monkeypatch):
        import app.validation.engine as engine

        def boom(*a, **k):
            raise RuntimeError("synthetic rule failure")

        monkeypatch.setattr(engine, "check_margins", boom)
        facts = [fact("f1", "gross_revenue", 12450, "Q1 FY27", growth=14.1,
                      growth_reported=True, cmp_period="Q1 FY26",
                      cmp_value=10910),
                 fact("f2", "depreciation", -600, "Q1 FY27")]
        r = engine.validate("d1", facts)
        # the broken family is reported as a failure rather than silently skipped
        broken = [x for x in r.results if x.rule == "margin_rule_error"]
        assert broken and broken[0].status == ValidationStatus.FAILED
        # and every other family still ran
        assert any(x.rule_family == "growth" for x in r.results)
        assert any(x.rule == "unexpected_negative" for x in r.results)
