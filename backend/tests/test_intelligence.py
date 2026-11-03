"""Milestone 2 -- financial intelligence and semantic analysis.

These tests assert the behaviours the milestone actually requires, and several
of them exist specifically to pin defects that were found and fixed while
building it:

* a narrative sentence naming two units must attribute to the nearer one;
* consolidated and standalone are different entities and must never be merged;
* a stock divided by a quarterly flow must be annualised;
* a monetary result must be compared against a reported value on the same
  magnitude;
* differently-labelled rows that share a taxonomy key are not a contradiction;
* facts with no period are never compared with each other.

Everything here runs on constructed facts, so the suite stays fast and the
assertions are about logic rather than about one document's contents.  The
tests that do need a real document are marked ``slow``.
"""
from __future__ import annotations

import pytest

from app.analysis.anomalies import detect_anomalies
from app.analysis.derived import compute_derived
from app.analysis.explain import explain_fact
from app.analysis.insights import generate_insights
from app.analysis.reconcile import classify, cross_metric_conflicts, reconcile
from app.analysis.relations import build_relations
from app.analysis.risks import extract_risks, summarise
from app.core.geometry import BBox
from app.document.entities import EntityRegistry
from app.extraction import taxonomy
from app.models.enums import (
    Currency, DerivationStatus, DuplicateResolution, EntityType, FactKind,
    MagnitudeUnit, PeriodType, RegionType, RelationKind, StatementType,
    ValueType,
)
from app.models.schemas import (
    BBoxModel, FinancialFact, NormalizedPeriod, Provenance, Region,
)


# --------------------------------------------------------------- helpers
def period(label: str, fy: int, q: int | None = None,
           kind: PeriodType = PeriodType.QUARTER) -> NormalizedPeriod:
    return NormalizedPeriod(label=label, raw_text=label, period_type=kind,
                            fiscal_year=fy, quarter=q)


def fact(fact_id: str, metric: str, value: float, *, entity: str = "Consolidated",
         entity_id: str | None = None, parent: str | None = None,
         label: str | None = None, plabel: str = "Q1 FY27", fy: int = 2027,
         q: int | None = 1, ptype: PeriodType = PeriodType.QUARTER,
         unit: MagnitudeUnit = MagnitudeUnit.CRORE,
         currency: Currency = Currency.INR,
         vtype: ValueType = ValueType.MONETARY, page: int = 1,
         confidence: float = 0.85, no_period: bool = False) -> FinancialFact:
    definition = taxonomy.get(metric)
    return FinancialFact(
        fact_id=fact_id, document_id="D", metric=label or metric,
        canonical_metric=metric,
        metric_display=definition.display if definition else metric,
        entity=entity, entity_id=entity_id or entity, parent_entity_id=parent,
        statement=(definition.statement if definition else StatementType.UNKNOWN),
        category=(definition.category if definition else "other"),
        kind=FactKind.TABLE_CELL, value=value, value_type=vtype,
        base_value=(value * 1e7 if vtype == ValueType.MONETARY else None),
        currency=(currency if vtype == ValueType.MONETARY else Currency.UNKNOWN),
        unit=(unit if vtype == ValueType.MONETARY else MagnitudeUnit.NONE),
        period=(None if no_period else plabel),
        period_normalized=(None if no_period else period(plabel, fy, q, ptype)),
        source_page=page, confidence=confidence,
        provenance=Provenance(document_id="D", page_number=page,
                              bbox=BBoxModel(x=10, y=20, width=80, height=12),
                              original_text=f"{label or metric} {value}",
                              extraction_method="table_cell"))


def region(rid: str, text: str, page: int = 1) -> Region:
    return Region(id=rid, document_id="D", page_number=page,
                  type=RegionType.PARAGRAPH, reading_order=0,
                  bbox=BBoxModel(x=0, y=0, width=400, height=40),
                  text=text, confidence=0.9)


# ------------------------------------------------------------- §2, §3 taxonomy
class TestTaxonomy:
    def test_metrics_are_placed_in_statements_and_categories(self):
        for key in ("revenue_from_operations", "ebitda", "total_assets",
                    "operating_cash_flow", "ebitda_margin"):
            definition = taxonomy.get(key)
            assert definition is not None, key
            assert definition.statement != StatementType.UNKNOWN, key
            assert definition.category, key

    def test_operational_kpis_are_not_forced_into_a_financial_statement(self):
        """Section 2: operational metrics are their own statement type."""
        for key in ("subscribers", "arpu", "stores", "throughput"):
            definition = taxonomy.get(key)
            if definition is None:
                continue
            assert definition.statement == StatementType.OPERATIONAL
            assert definition.is_operational

    def test_near_neighbour_metrics_stay_distinct(self):
        """Section 3: 'PAT' and 'PAT incl. associates' are different measures."""
        pat = taxonomy.get("pat")
        assert "pat_after_associates" in pat.distinct_from
        after = taxonomy.get("pat_after_associates")
        assert after is not None and after.key != pat.key

    def test_original_label_is_preserved_on_the_fact(self):
        f = fact("f1", "pat", 100.0, label="Profit for the period")
        assert f.metric == "Profit for the period"      # as printed
        assert f.canonical_metric == "pat"              # normalised
        assert f.metric_display == taxonomy.get("pat").display


# ------------------------------------------------------------- §4 entities
class TestEntityModel:
    def test_hierarchy_is_inferred_not_hardcoded(self):
        reg = EntityRegistry("D", "Acme Industries Limited")
        seg = reg.register_heading("Widgets Division",
                                   raw_heading="WIDGETS DIVISION SEGMENT",
                                   page=4, abbreviation="WD")
        ref = reg.get(seg)
        assert ref.entity_type == EntityType.SEGMENT
        assert ref.parent_id == reg.consolidated_id
        assert reg.is_group_level(reg.consolidated_id)
        assert not reg.is_group_level(seg)

    def test_consolidated_subsidiary_is_not_the_group(self):
        """A subsidiary's consolidated result is not the group's."""
        reg = EntityRegistry("D", "Acme Industries Limited")
        sub = reg.register_heading("Consolidated Beta Platforms Limited",
                                   raw_heading="CONSOLIDATED BETA PLATFORMS "
                                               "LIMITED", page=5)
        assert sub != reg.consolidated_id
        assert reg.get(sub).entity_type == EntityType.SUBSIDIARY

    def test_standalone_and_consolidated_are_separate_entities(self):
        reg = EntityRegistry("D", "Acme Industries Limited")
        standalone = reg.register_basis(EntityType.STANDALONE, page=28,
                                        evidence="page header")
        assert standalone != reg.consolidated_id
        assert reg.get(standalone).entity_type == EntityType.STANDALONE

    def test_nearest_named_entity_wins_in_a_run_of_bullets(self):
        """Pins a real defect: a page-1 consolidated figure was attributed to a
        segment named later in the same block of bullets."""
        reg = EntityRegistry("D", "Acme Industries Limited")
        reg.register_heading("Widgets Division",
                             raw_heading="WIDGETS DIVISION SEGMENT", page=4,
                             abbreviation="WD")
        text = ("Consolidated Revenue at 340,257 crore, up 24.5% Y-o-Y. "
                "WD EBITDA up 23.9% Y-o-Y at 17,010 crore.")
        near_revenue = reg.find_in_text(text, anchor=text.index("Revenue"))
        near_ebitda = reg.find_in_text(text, anchor=text.index("EBITDA"))
        assert near_revenue.entity_type == EntityType.CONSOLIDATED
        assert near_ebitda.name == "Widgets Division"

    def test_unknown_names_are_never_invented(self):
        reg = EntityRegistry("D", "Acme Industries Limited")
        assert reg.find_in_text("Gamma Corporation reported strong growth") is None


# ------------------------------------------------------- §7 units & currency
class TestUnitsAndCurrency:
    def test_indian_magnitude_beats_a_borrowed_currency_symbol(self):
        """Pins a real defect: '201,803 crore ($ 21.3 billion)' was read as USD
        because the rupee glyph is routinely lost in extraction."""
        from app.normalization.numbers import normalize_number
        n = normalize_number(
            "201,803 crore",
            context="Record Quarterly Revenue at 201,803 crore ($ 21.3 billion)")
        assert n.currency == Currency.INR
        assert n.magnitude == MagnitudeUnit.CRORE
        assert n.currency_inferred is True

    def test_a_genuine_dollar_figure_is_still_dollars(self):
        from app.normalization.numbers import normalize_number
        n = normalize_number("$104.5", context="Dated Brent averaged $104.5/bbl")
        assert n.currency == Currency.USD

    def test_a_caption_naming_two_currencies_decides_nothing(self):
        from app.normalization.numbers import unambiguous_currency
        assert unambiguous_currency("Revenue of Rs 100 crore ($ 12 million)") is None
        assert unambiguous_currency("(Rs in crore)") == Currency.INR

    def test_currencies_are_never_silently_converted(self):
        facts = [fact("a", "ebitda", 100.0, currency=Currency.USD),
                 fact("b", "revenue_from_operations", 500.0,
                      currency=Currency.INR)]
        assert compute_derived("D", facts) == []


# --------------------------------------------------------- §11 derived metrics
class TestDerivedMetrics:
    def test_derived_metric_records_formula_and_inputs(self):
        facts = [fact("a", "ebitda", 54067.0),
                 fact("b", "revenue_from_operations", 311850.0)]
        derived = compute_derived("D", facts)
        margin = next(m for m in derived if m.canonical_metric == "ebitda_margin")
        assert margin.derivation == DerivationStatus.CALCULATED
        assert margin.formula
        assert set(margin.input_fact_ids) == {"a", "b"}
        assert margin.value == pytest.approx(17.34, abs=0.01)

    def test_calculated_is_never_presented_as_reported(self):
        facts = [fact("a", "ebitda", 100.0),
                 fact("b", "revenue_from_operations", 500.0)]
        derived = compute_derived("D", facts)
        assert all(m.derivation == DerivationStatus.CALCULATED for m in derived)
        assert all("not a number the document reported" in m.notes
                   or "compared against" in m.notes for m in derived)

    def test_stock_over_quarterly_flow_is_annualised(self):
        """Pins a real defect: net debt / one quarter of EBITDA read four times
        too high and contradicted the document's own figure."""
        facts = [fact("a", "outstanding_debt", 300000.0),
                 fact("b", "cash_and_equivalents", 177086.0),
                 fact("c", "ebitda", 54067.0),
                 fact("d", "net_debt_to_ebitda", 0.57, vtype=ValueType.RATIO)]
        derived = compute_derived("D", facts)
        lev = next(m for m in derived
                   if m.canonical_metric == "net_debt_to_ebitda")
        assert lev.agrees_with_reported is True
        assert "annualised" in lev.formula

    def test_monetary_result_is_compared_on_the_reported_magnitude(self):
        """Pins a real defect: a base-unit result was compared against a figure
        printed in crore and reported a discrepancy of ten million to one."""
        facts = [fact("a", "outstanding_debt", 300000.0),
                 fact("b", "cash_and_equivalents", 177086.0),
                 fact("c", "net_debt", 122914.0)]
        derived = compute_derived("D", facts)
        net = next(m for m in derived if m.canonical_metric == "net_debt")
        assert net.unit == MagnitudeUnit.CRORE
        assert net.value == pytest.approx(122914.0, abs=1.0)
        assert net.agrees_with_reported is True

    def test_flow_ratios_are_not_computed_against_a_balance_sheet_date(self):
        facts = [fact("a", "ebitda", 100.0, plabel="As at 2026-06-30",
                      ptype=PeriodType.POINT_IN_TIME, q=None),
                 fact("b", "revenue_from_operations", 500.0,
                      plabel="As at 2026-06-30",
                      ptype=PeriodType.POINT_IN_TIME, q=None)]
        derived = compute_derived("D", facts)
        assert not [m for m in derived if m.canonical_metric == "ebitda_margin"]

    def test_the_denominator_the_document_used_is_identified(self):
        """A filing may publish margins on gross revenue while also printing
        revenue from operations.  Which basis it used is a fact about the
        document, and is reported rather than treated as a discrepancy."""
        facts = [fact("a", "ebitda", 54067.0),
                 fact("b", "revenue_from_operations", 311850.0),
                 fact("c", "gross_revenue", 340257.0),
                 fact("d", "ebitda_margin", 15.9, vtype=ValueType.PERCENTAGE)]
        derived = compute_derived("D", facts)
        margin = next(m for m in derived if m.canonical_metric == "ebitda_margin")
        assert margin.agrees_with_reported is True
        assert "gross_revenue" in margin.formula
        assert "reconciles against" in margin.notes


# ------------------------------------------------- §10 segments never combine
class TestSegmentIsolation:
    def test_facts_from_different_entities_are_never_combined(self):
        facts = [
            fact("g1", "ebitda", 54067.0, entity="Consolidated"),
            fact("g2", "revenue_from_operations", 311850.0,
                 entity="Consolidated"),
            fact("s1", "ebitda", 17010.0, entity="O2C", parent="Consolidated"),
            fact("s2", "revenue_from_operations", 201803.0, entity="O2C",
                 parent="Consolidated"),
        ]
        derived = compute_derived("D", facts)
        by_entity = {m.entity_name: m for m in derived
                     if m.canonical_metric == "ebitda_margin"}
        assert set(by_entity) == {"Consolidated", "O2C"}
        # each margin uses only its own entity's numbers
        assert by_entity["O2C"].value == pytest.approx(
            17010.0 / 201803.0 * 100, abs=0.01)
        assert by_entity["Consolidated"].value == pytest.approx(
            54067.0 / 311850.0 * 100, abs=0.01)

    def test_a_segment_share_never_exceeds_the_reported_total(self):
        facts = [
            fact("g", "revenue_from_operations", 311850.0, entity="Consolidated"),
            fact("s", "revenue_from_operations", 201803.0, entity="O2C",
                 parent="Consolidated", page=11),
        ]
        insights = generate_insights("D", facts)
        share = [i for i in insights
                 if str(i.insight_type) == "segment_contribution"]
        assert share and 0 < share[0].change <= 100.0
        assert set(share[0].supporting_fact_ids) == {"s", "g"}


# ------------------------------------------- §17, §18 duplicates and conflicts
class TestReconciliation:
    def test_identical_repeats_merge_and_corroborate(self):
        facts = [fact("a", "revenue_from_operations", 100.0, page=1),
                 fact("b", "revenue_from_operations", 100.0, page=7)]
        result = reconcile("D", facts)
        assert len(result.facts) == 1
        survivor = result.facts[0]
        assert survivor.duplicate_resolution == DuplicateResolution.IDENTICAL_REPEAT
        assert survivor.meta["duplicate_pages"] == [1, 7]

    def test_a_genuine_conflict_keeps_both_and_picks_neither(self):
        facts = [fact("a", "revenue_from_operations", 100.0, page=1),
                 fact("b", "revenue_from_operations", 115.0, page=9)]
        result = reconcile("D", facts)
        assert len(result.facts) == 2
        assert all(f.requires_review for f in result.facts)
        assert len(result.conflicts) == 1
        assert "unresolved" in result.conflicts[0]["resolution"]
        kinds = {str(r.kind) for r in result.relations}
        assert str(RelationKind.CONFLICTS_WITH) in kinds

    def test_different_entities_are_not_a_conflict(self):
        facts = [fact("a", "revenue_from_operations", 100.0, entity="Consolidated"),
                 fact("b", "revenue_from_operations", 40.0, entity="O2C")]
        result = reconcile("D", facts)
        assert len(result.facts) == 2
        assert not result.conflicts

    def test_differently_labelled_rows_are_not_a_conflict(self):
        """Pins a real defect: 'Total Throughput' and 'Production meant for
        sale' both match the taxonomy's throughput and were called
        contradictory."""
        a = fact("a", "throughput", 18.1, label="Total Throughput", page=14,
                 vtype=ValueType.COUNT)
        b = fact("b", "throughput", 15.6, label="Production meant for sale",
                 page=14, vtype=ValueType.COUNT)
        assert classify(a, b) == DuplicateResolution.DIFFERENT_DEFINITION
        result = reconcile("D", [a, b])
        assert len(result.facts) == 2
        assert not result.conflicts

    def test_facts_without_a_period_are_never_compared(self):
        """Pins a real defect: several columns of one table failed period
        resolution together and were declared to contradict each other."""
        facts = [fact("a", "ebitda", 827.0, no_period=True, page=18),
                 fact("b", "ebitda", 1049.0, no_period=True, page=18)]
        result = reconcile("D", facts)
        assert len(result.facts) == 2
        assert not result.conflicts

    def test_distinct_measures_are_linked_not_merged(self):
        facts = [fact("a", "pat", 23001.0),
                 fact("b", "pat_after_associates", 23000.0)]
        relations = cross_metric_conflicts("D", facts)
        assert relations
        assert "never merged" in relations[0].detail["explanation"]


# ------------------------------------------------------- §12 relationship graph
class TestRelations:
    def test_period_comparisons_are_linked_with_the_computed_change(self):
        facts = [fact("a", "ebitda", 54067.0, plabel="Q1 FY27", fy=2027, q=1),
                 fact("b", "ebitda", 44000.0, plabel="Q1 FY26", fy=2026, q=1)]
        relations = build_relations("D", facts)
        comparison = next(r for r in relations
                          if r.kind == RelationKind.COMPARISON)
        assert comparison.detail["basis"] == "yoy"
        assert comparison.detail["change_pct"] == pytest.approx(22.88, abs=0.1)

    def test_incomparable_facts_are_not_linked(self):
        facts = [fact("a", "ebitda", 54067.0, currency=Currency.INR,
                      plabel="Q1 FY27", fy=2027, q=1),
                 fact("b", "ebitda", 6400.0, currency=Currency.USD,
                      plabel="Q1 FY26", fy=2026, q=1)]
        relations = build_relations("D", facts)
        assert not [r for r in relations if r.kind == RelationKind.COMPARISON]

    def test_derived_metrics_link_back_to_their_inputs(self):
        facts = [fact("a", "ebitda", 100.0),
                 fact("b", "revenue_from_operations", 500.0)]
        derived = compute_derived("D", facts)
        relations = build_relations("D", facts, derived)
        inputs = [r for r in relations if r.kind == RelationKind.INPUT_TO]
        assert {r.from_fact_id for r in inputs} == {"a", "b"}


# --------------------------------------------------------------- §15 risks
class TestRisks:
    def test_cautionary_statements_are_quoted_with_their_category(self):
        risks = extract_risks("D", [region(
            "r1", "Margins remain exposed to volatility in crude oil prices, "
                  "which could adversely impact profitability.")])
        assert len(risks) == 1
        assert str(risks[0].category) == "commodity_price"
        assert risks[0].trigger_phrase
        assert risks[0].text.startswith("Margins remain exposed")

    def test_the_safe_harbour_disclaimer_is_not_a_business_risk(self):
        risks = extract_risks("D", [region(
            "r1", "This presentation contains forward-looking statements within "
                  "the meaning of applicable securities laws. Actual results "
                  "may differ materially from those expressed.")])
        assert risks == []

    def test_ordinary_prose_is_not_a_risk(self):
        risks = extract_risks("D", [region(
            "r1", "Revenue grew 11.1% year on year, driven by higher volumes "
                  "across the retail business.")])
        assert risks == []

    def test_no_overall_risk_score_is_invented(self):
        risks = extract_risks("D", [region(
            "r1", "Regulatory developments could adversely impact margins.")])
        summary = summarise(risks)
        assert "score" not in summary
        assert set(summary) >= {"total", "by_category", "by_severity"}
        assert "does not compute an overall risk score" in summary["note"]


# ------------------------------------------------------------- §23 insights
class TestInsights:
    def test_every_insight_names_the_facts_it_came_from(self):
        facts = [fact("a", "ebitda", 54067.0, plabel="Q1 FY27", fy=2027, q=1),
                 fact("b", "ebitda", 44000.0, plabel="Q1 FY26", fy=2026, q=1)]
        insights = generate_insights("D", facts)
        assert insights
        for i in insights:
            assert i.supporting_fact_ids
            assert set(i.supporting_fact_ids) <= {f.fact_id for f in facts}

    def test_reported_and_calculated_rates_are_never_compared(self):
        """A rate the document printed and one FinSight computed are not two
        points on one series; comparing them would report an arithmetic
        difference as a business movement."""
        facts = [
            fact("a", "ebitda_margin", 21.5, vtype=ValueType.PERCENTAGE,
                 plabel="Q1 FY27", fy=2027, q=1),
            fact("b", "ebitda", 42000.0, plabel="Q1 FY27", fy=2027, q=1),
            fact("c", "revenue_from_operations", 231000.0, plabel="Q1 FY27",
                 fy=2027, q=1),
            fact("d", "ebitda", 35000.0, plabel="Q1 FY26", fy=2026, q=1),
            fact("e", "revenue_from_operations", 208000.0, plabel="Q1 FY26",
                 fy=2026, q=1),
        ]
        derived = compute_derived("D", facts)
        margins = [i for i in generate_insights("D", facts, derived)
                   if str(i.insight_type) == "margin_movement"
                   and i.metric == "ebitda_margin"]
        for insight in margins:
            # the movement must be between two points of the same kind
            assert insight.derivation == DerivationStatus.CALCULATED


# ------------------------------------------------------------ §24 anomalies
class TestAnomalies:
    def test_every_anomaly_states_its_threshold_and_evidence(self):
        facts = [fact("a", "ebitda", 54067.0, plabel="Q1 FY27", fy=2027, q=1),
                 fact("b", "ebitda", 1000.0, plabel="Q1 FY26", fy=2026, q=1)]
        found = detect_anomalies("D", facts)
        assert found
        for a in found:
            assert a.reason and a.message
            assert a.fact_ids
            assert a.threshold is not None

    def test_a_reported_ratio_that_does_not_reproduce_is_flagged(self):
        facts = [fact("a", "ebitda", 100.0),
                 fact("b", "revenue_from_operations", 500.0),
                 fact("c", "ebitda_margin", 35.0, vtype=ValueType.PERCENTAGE)]
        derived = compute_derived("D", facts)
        found = detect_anomalies("D", facts, derived)
        kinds = {str(a.anomaly_type) for a in found}
        assert "reported_vs_calculated" in kinds

    def test_one_anomaly_per_contradicting_pair(self):
        facts = [fact("a", "revenue_from_operations", 100.0, page=1),
                 fact("b", "revenue_from_operations", 115.0, page=9)]
        result = reconcile("D", facts)
        found = [a for a in detect_anomalies("D", result.facts)
                 if str(a.anomaly_type) == "contradictory_duplicate"]
        assert len(found) == 1


# ---------------------------------------------------- §19, §21 explanation
class TestExplanation:
    def test_explanation_is_deterministic_and_source_grounded(self):
        f = fact("a", "ebitda", 54067.0, label="EBITDA")
        first = explain_fact(f)
        second = explain_fact(f)
        assert first.model_dump() == second.model_dump()
        assert "no language model" in first.generated_by
        assert first.why_this_metric and first.why_this_period
        assert first.why_this_entity and first.why_this_unit
        assert first.provenance is not None

    def test_confidence_is_broken_into_weighted_components(self):
        from app.extraction import confidence as conf_mod
        f = fact("a", "ebitda", 54067.0)
        f.confidence_breakdown = conf_mod.score(conf_mod.Signals(
            ocr=0.9, layout=0.8, structure=0.85, semantic=0.95))
        explanation = explain_fact(f)
        names = {c.name for c in explanation.confidence_components}
        assert {"ocr", "layout", "structure", "semantic"} <= names
        assert all(c.reason for c in explanation.confidence_components)
        weighted = [c for c in explanation.confidence_components if c.weight > 0]
        assert weighted, "at least one component must carry a real weight"

    def test_a_period_less_fact_says_so_plainly(self):
        f = fact("a", "ebitda", 54067.0, no_period=True)
        explanation = explain_fact(f)
        assert "no period attribution" in explanation.why_this_period

    def test_explanation_reports_a_conflict_without_resolving_it(self):
        facts = [fact("a", "revenue_from_operations", 100.0, page=1),
                 fact("b", "revenue_from_operations", 115.0, page=9)]
        result = reconcile("D", facts)
        explanation = explain_fact(result.facts[0], relations=result.relations)
        assert "does not choose" in explanation.derivation_note.lower() or \
               "both were kept" in explanation.derivation_note.lower()


# ------------------------------------------------- §5 period model robustness
class TestPeriodModel:
    def test_a_march_year_end_is_not_assumed(self):
        """The same calendar date is a different fiscal quarter depending on
        the year end, and the year end is inferred from the document."""
        from app.normalization.periods import FiscalCalendar, parse_period
        march = parse_period("Quarter ended 31 Mar 2026",
                             calendar=FiscalCalendar(year_end_month=3))
        december = parse_period("Quarter ended 31 Mar 2026",
                                calendar=FiscalCalendar(year_end_month=12))
        assert march.label == "Q4 FY26"
        assert december.label == "Q1 FY26"

    def test_an_unparseable_period_yields_nothing_rather_than_a_guess(self):
        from app.normalization.periods import FiscalCalendar, parse_period
        assert parse_period("Particulars",
                            calendar=FiscalCalendar(year_end_month=3)) is None

    def test_column_periods_are_repaired_across_a_page_break(self):
        """Pins a real defect: a statement continuing onto the next page
        repeated its date headers without the 'Quarter Ended' banner, so every
        column degraded to a balance-sheet date."""
        from app.models.schemas import ExtractedTable, TableColumn
        from app.normalization.periods import FiscalCalendar
        from app.tables.periods import harmonise_periods

        def table(tid, headers, periods):
            return ExtractedTable(
                id=tid, document_id="D", page_number=1,
                bbox=BBoxModel(x=0, y=0, width=500, height=200),
                n_rows=2, n_cols=len(headers),
                columns=[TableColumn(index=i, header=h, x_start=0, x_end=1,
                                     normalized_period=p)
                         for i, (h, p) in enumerate(zip(headers, periods))])

        first = table("t1", ["Quarter Ended 30th Jun'26", "Year Ended 31st Mar'26"],
                      ["Q1 FY27", "FY26"])
        continuation = table("t2", ["30th Jun'26", "31st Mar'26"],
                             ["As at 2026-06-30", "As at 2026-03-31"])
        stats = harmonise_periods([first, continuation],
                                  calendar=FiscalCalendar(year_end_month=3))
        assert stats["harmonised"] >= 1
        assert continuation.columns[0].normalized_period == "Q1 FY27"


# --------------------------------------------------------- §6 comparatives
class TestComparatives:
    def test_reported_and_calculated_change_are_both_stored(self):
        from app.extraction.facts import TableFactContext
        f = fact("a", "ebitda", 110.0)
        f.growth = 10.0
        f.growth_reported = True
        f.growth_calculated = 9.5
        explanation = explain_fact(f)
        assert f.growth_reported is True
        assert f.growth == 10.0 and f.growth_calculated == 9.5
        assert "+10.00%" in explanation.derivation_note
        assert "+9.50%" in explanation.derivation_note
        assert "stated by the document" in explanation.derivation_note
        assert TableFactContext is not None

# ------------------------------------------------ §7 hierarchical column heads
class TestHierarchicalColumnHeads:
    """Reconstructing a column head that is spread over rows and space.

    Built from bare geometry rather than a PDF, so each test states exactly
    which pixels it is about.  Nothing here names a filer: the shapes are the
    ones statements print, and the layout numbers are invented.
    """

    @staticmethod
    def _line(idx, y, items, page=1):
        """One text line: ``items`` is a list of (x, width, text)."""
        from app.models.schemas import BBoxModel, OCRElement
        from app.ocr.lines import TextLine
        els = [OCRElement(id=f"e{idx}_{i}", document_id="D", page_number=page,
                          text=t, confidence=0.99,
                          bbox=BBoxModel(x=x, y=y, width=w, height=18))
               for i, (x, w, t) in enumerate(items)]
        return TextLine(id=f"l{idx}", page_number=page, elements=els,
                        bbox=BBox.union_all(e.bbox.geom for e in els))

    def _statement(self):
        """A statement whose head sits above the band of figures.

        Two banners each cover a pair of dated columns, the dates sit under
        them and the years wrap onto a third row -- the shape that leaves a
        layout cut running between the head and the figures::

              Three Months Ended        Nine Months Ended
             June 28,   June 29,      June 28,   June 29,
               2025       2024          2025       2024
        """
        head = [
            self._line(0, 40, [(300, 700, "A CONDENSED STATEMENT OF OPERATIONS")]),
            self._line(1, 70, [(430, 250, "(In millions)")]),
            self._line(2, 110, [(920, 270, "Three Months Ended"),
                                (1290, 270, "Nine Months Ended")]),
            self._line(3, 140, [(870, 100, "June 28,"), (1050, 100, "June 29,"),
                                (1240, 100, "June 28,"), (1420, 100, "June 29,")]),
            self._line(4, 170, [(890, 70, "2025"), (1070, 70, "2024"),
                                (1260, 70, "2025"), (1440, 70, "2024")]),
        ]
        body = [
            self._line(5, 210, [(140, 300, "Net sales"), (900, 130, "66,613"),
                                (1080, 130, "61,564"), (1270, 140, "233,287"),
                                (1450, 130, "224,908")]),
            self._line(6, 240, [(140, 300, "Cost of sales"), (900, 130, "36,435"),
                                (1080, 130, "32,748"), (1270, 140, "127,192"),
                                (1450, 130, "121,246")]),
            self._line(7, 270, [(140, 300, "Gross margin"), (900, 130, "30,178"),
                                (1080, 130, "28,816"), (1270, 140, "106,095"),
                                (1450, 130, "103,662")]),
        ]
        return head, body

    def _build(self, calendar):
        from app.tables.structure import build_table
        head, body = self._statement()
        return build_table(
            document_id="D", page_number=1, region_id="r1", lines=body,
            v_lines=[], page_width=1700, page_height=2200, table_index=0,
            page_lines=head + body, calendar=calendar)

    def test_a_head_printed_above_the_band_is_still_found(self):
        """Layout analysis cuts the region at the first row of figures.

        The head is still on the page, directly above the cut and within the
        table's own horizontal extent.  Losing it leaves every column undated.
        """
        from app.normalization.periods import FiscalCalendar
        table = self._build(FiscalCalendar(year_end_month=9))
        assert table is not None
        value_cols = [c for c in table.columns if c.index > 0]
        assert [c.normalized_period for c in value_cols] == [
            "Q3 FY25", "Q3 FY24", "9M FY25", "9M FY24"]

    def test_a_banner_reaches_every_column_it_covers(self):
        """"Nine Months Ended" is centred over its pair and is not wide enough
        to be 1.3x either band, so a width ratio misses the second column.
        Counting the bands it touches does not."""
        from app.normalization.periods import FiscalCalendar
        table = self._build(FiscalCalendar(year_end_month=9))
        last = table.columns[-1]
        assert last.header_path[0] == "Nine Months Ended"
        assert last.header_path[-1] == "2024"

    def test_the_page_title_is_not_mistaken_for_a_banner(self):
        """A title beginning inside the row-label column addresses the page,
        not the value columns.  Reading it as a banner would give every column
        the title's own period."""
        from app.normalization.periods import FiscalCalendar
        table = self._build(FiscalCalendar(year_end_month=9))
        for column in table.columns:
            assert not any("CONDENSED" in part for part in column.header_path)
        labels = [c.normalized_period for c in table.columns if c.index > 0]
        assert len(set(labels)) == 4      # no two columns collapsed together

    def test_an_unknown_year_end_dates_the_columns_without_labelling_them(self):
        """The dates are read from the page and are certain.  The fiscal year
        is not, and must not be invented to fill the field."""
        from app.normalization.periods import DEFAULT_CALENDAR
        table = self._build(DEFAULT_CALENDAR)
        labels = [c.normalized_period for c in table.columns if c.index > 0]
        assert labels == ["3M ended 2025-06-28", "3M ended 2024-06-29",
                          "9M ended 2025-06-28", "9M ended 2024-06-29"]
