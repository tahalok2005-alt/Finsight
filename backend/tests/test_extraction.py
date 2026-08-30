"""Taxonomy matching, table structure, fact extraction and confidence."""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.extraction import confidence as conf_mod
from app.extraction.facts import TableFactContext, deduplicate, extract_from_table
from app.extraction.narrative import (
    NarrativeContext, classify_topics, extract_commentary, extract_guidance,
    extract_narrative_facts, is_forward_looking,
)
from app.extraction.taxonomy import match_metric, normalize_label
from app.models.enums import (
    CellRole, CommentaryTopic, ConfidenceBand, Currency, MagnitudeUnit,
    RegionType, ValidationStatus, ValueType,
)
from app.models.schemas import (
    BBoxModel, ConfidenceBreakdown, ExtractedTable, Region, TableCell,
    TableColumn,
)
from app.normalization.periods import DEFAULT_CALENDAR


class TestTaxonomy:
    @pytest.mark.parametrize("label,key", [
        ("Gross Revenue", "gross_revenue"),
        ("EBITDA", "ebitda"),
        ("EBITDA margin (%)", "ebitda_margin"),
        ("Profit Before Tax", "pbt"),
        ("Profit After Tax", "pat"),
        ("Tax Expenses", "tax_expense"),
        ("Cash & Cash Equivalents", "cash_and_equivalents"),
        ("Net Debt to EBITDA*", "net_debt_to_ebitda"),
        ("Capital Expenditure #", "capex"),
        ("Revenue from Operations", "revenue_from_operations"),
        ("ARPU (Rs/Sub/month)", "arpu"),
        ("Number of Stores", "store_count"),
        ("Area Operated", "area_operated"),
        ("Customer Base", "subscribers"),
    ])
    def test_real_labels_match(self, label, key):
        m = match_metric(label)
        assert m is not None and m.key == key

    @pytest.mark.parametrize("label", [
        "Random Nonsense Label",
        "Income tax relating to items that will not be reclassified to Profit or Loss",
        "Particulars",
    ])
    def test_unknown_labels_are_not_forced(self, label):
        assert match_metric(label) is None

    def test_near_synonyms_stay_distinct(self):
        """Revenue variants are related but different measures."""
        keys = {match_metric(x).key for x in
                ("Revenue", "Gross Revenue", "Revenue from Operations",
                 "Net Revenue")}
        assert len(keys) == 4

    def test_pat_variants_stay_distinct(self):
        assert match_metric("Profit After Tax").key == "pat"
        assert match_metric(
            "Profit After Tax and Share of Profit/(Loss) of Associates & JVs"
        ).key == "pat_after_associates"

    def test_expense_lines_get_their_own_metrics(self):
        assert match_metric("Total Expenses").key == "total_expenses"
        assert match_metric("Other Expenses").key == "other_expenses"

    def test_normalize_label(self):
        assert normalize_label("EBITDA margin (%)") == "ebitda margin"
        assert normalize_label("Cash & Cash Equivalents") == \
            "cash and cash equivalents"


def _make_table(document_id="doc1", page=1) -> ExtractedTable:
    """A 4-column results table mirroring the shape of a real release."""
    headers = ["Particulars", "1Q FY27", "1Q FY26", "% chg. Y-o-Y"]
    rows = [
        ("Gross Revenue", "12,450", "10,910", "14.1"),
        ("EBITDA", "2,490", "2,180", "14.2"),
        ("EBITDA margin (%)", "20.0", "20.0", "-"),
        ("Profit After Tax", "1,200", "1,020", "17.6"),
    ]
    cells: list[TableCell] = []
    for c_i, h in enumerate(headers):
        cells.append(TableCell(
            id=f"h{c_i}", table_id="t1", document_id=document_id,
            page_number=page, row=0, col=c_i, text=h,
            role=CellRole.CORNER if c_i == 0 else CellRole.COLUMN_HEADER,
            bbox=BBoxModel(x=100 * c_i, y=0, width=90, height=12),
            confidence=0.95))
    for r_i, row in enumerate(rows, start=1):
        for c_i, val in enumerate(row):
            is_num = c_i > 0 and val != "-"
            cells.append(TableCell(
                id=f"c{r_i}_{c_i}", table_id="t1", document_id=document_id,
                page_number=page, row=r_i, col=c_i, text=val,
                role=CellRole.ROW_LABEL if c_i == 0 else CellRole.DATA,
                bbox=BBoxModel(x=100 * c_i, y=20 * r_i, width=90, height=12),
                confidence=0.93,
                numeric_value=float(val.replace(",", "")) if is_num else None,
                is_numeric=bool(is_num or val == "-")))
    columns = [
        TableColumn(index=0, header="Particulars", numeric_ratio=0.0),
        TableColumn(index=1, header="1Q FY27", normalized_period="Q1 FY27",
                    numeric_ratio=1.0),
        TableColumn(index=2, header="1Q FY26", normalized_period="Q1 FY26",
                    numeric_ratio=1.0),
        TableColumn(index=3, header="% chg. Y-o-Y", is_growth_column=True,
                    numeric_ratio=0.75),
    ]
    return ExtractedTable(
        id="t1", document_id=document_id, page_number=page,
        region_id="r1", title="Consolidated Financial Highlights",
        caption="(Rs in crore)",
        bbox=BBoxModel(x=0, y=0, width=400, height=120),
        n_rows=5, n_cols=4, columns=columns, cells=cells, confidence=0.95,
        unit_hint=MagnitudeUnit.CRORE, currency_hint=Currency.INR)


@pytest.fixture
def fact_context():
    return TableFactContext(
        document_id="doc1", entity="Consolidated", calendar=DEFAULT_CALENDAR,
        default_currency=Currency.INR, default_unit=MagnitudeUnit.CRORE)


class TestTableFactExtraction:
    def test_extracts_one_fact_per_period_cell(self, fact_context):
        facts = extract_from_table(_make_table(), fact_context)
        by = {(f.canonical_metric, f.period): f for f in facts}
        assert by[("gross_revenue", "Q1 FY27")].value == 12450.0
        assert by[("gross_revenue", "Q1 FY26")].value == 10910.0
        assert by[("ebitda", "Q1 FY27")].value == 2490.0
        assert by[("pat", "Q1 FY27")].value == 1200.0

    def test_units_and_currency_inherited_from_table(self, fact_context):
        facts = extract_from_table(_make_table(), fact_context)
        rev = next(f for f in facts
                   if f.canonical_metric == "gross_revenue" and f.period == "Q1 FY27")
        assert rev.unit == MagnitudeUnit.CRORE
        assert rev.currency == Currency.INR
        assert rev.base_value == pytest.approx(12450 * 1e7)
        assert rev.raw_value == "12,450"

    def test_margin_row_is_not_given_a_monetary_unit(self, fact_context):
        facts = extract_from_table(_make_table(), fact_context)
        margin = next(f for f in facts
                      if f.canonical_metric == "ebitda_margin"
                      and f.period == "Q1 FY27")
        assert margin.value_type == ValueType.PERCENTAGE
        assert margin.unit == MagnitudeUnit.PERCENT
        assert margin.currency == Currency.UNKNOWN
        assert margin.base_value is None

    def test_reported_growth_is_flagged_as_reported(self, fact_context):
        facts = extract_from_table(_make_table(), fact_context)
        rev = next(f for f in facts
                   if f.canonical_metric == "gross_revenue" and f.period == "Q1 FY27")
        assert rev.growth == pytest.approx(14.1)
        assert rev.growth_reported is True
        assert rev.comparison_period == "Q1 FY26"
        assert rev.comparison_value == 10910.0

    def test_margin_change_is_points_not_relative_percent(self, fact_context):
        facts = extract_from_table(_make_table(), fact_context)
        margin = next(f for f in facts
                      if f.canonical_metric == "ebitda_margin"
                      and f.period == "Q1 FY27")
        assert margin.growth is None            # no misleading relative growth
        assert margin.meta.get("change_pp") == pytest.approx(0.0)

    def test_every_fact_carries_provenance(self, fact_context):
        for f in extract_from_table(_make_table(), fact_context):
            assert f.provenance is not None
            assert f.provenance.bbox is not None
            assert f.provenance.cell_id
            assert f.provenance.page_number == 1
            assert f.provenance.extraction_method == "table_cell"

    def test_unknown_row_labels_produce_no_facts(self, fact_context):
        table = _make_table()
        for c in table.cells:
            if c.row == 1 and c.col == 0:
                c.text = "Some Unrecognised Line Item"
        facts = extract_from_table(table, fact_context)
        assert not any(f.metric == "Some Unrecognised Line Item" for f in facts)


class TestDeduplication:
    def test_agreeing_duplicates_raise_confidence(self, fact_context):
        a = extract_from_table(_make_table(), fact_context)
        b = extract_from_table(_make_table(page=9), fact_context)
        for f in b:
            f.fact_id += "_p9"
            f.source_page = 9
        merged = deduplicate(a + b)
        rev = next(f for f in merged
                   if f.canonical_metric == "gross_revenue" and f.period == "Q1 FY27")
        assert rev.meta["duplicate_count"] == 2
        assert not rev.requires_review

    def test_conflicting_duplicates_are_kept_and_flagged(self, fact_context):
        a = extract_from_table(_make_table(), fact_context)
        b = extract_from_table(_make_table(page=9), fact_context)
        for f in b:
            f.fact_id += "_p9"
            f.source_page = 9
            if f.canonical_metric == "gross_revenue" and f.period == "Q1 FY27":
                f.value = 99999.0
        merged = deduplicate(a + b)
        conflicting = [f for f in merged
                       if f.canonical_metric == "gross_revenue"
                       and f.period == "Q1 FY27"]
        assert len(conflicting) == 2           # neither is silently discarded
        assert all(f.requires_review for f in conflicting)


class TestNarrative:
    @pytest.fixture
    def ctx(self):
        return NarrativeContext(
            document_id="doc1", entity="Consolidated",
            calendar=DEFAULT_CALENDAR, default_currency=Currency.INR,
            default_unit=MagnitudeUnit.CRORE, default_period="Q1 FY27")

    def _region(self, text: str, rtype=RegionType.PARAGRAPH) -> Region:
        return Region(id="r1", document_id="doc1", page_number=1, type=rtype,
                      bbox=BBoxModel(x=0, y=0, width=400, height=40),
                      confidence=0.9, reading_order=1, text=text)

    def test_sentence_fact(self, ctx):
        facts = extract_narrative_facts(self._region(
            "Revenue increased 24.5% Y-o-Y to Rs 340,257 crore."), ctx)
        assert facts
        f = facts[0]
        assert f.canonical_metric.startswith("revenue")
        assert f.value == 340257.0
        assert f.unit == MagnitudeUnit.CRORE
        assert f.growth == pytest.approx(24.5)

    def test_decline_is_negative_growth(self, ctx):
        facts = extract_narrative_facts(self._region(
            "EBITDA declined by 5.2% Y-o-Y to Rs 1,000 crore."), ctx)
        assert facts and facts[0].growth == pytest.approx(-5.2)

    def test_guidance_is_detected_and_separated(self, ctx):
        text = ("The company expects capital expenditure of Rs 3,000 crore in "
                "FY28.")
        assert is_forward_looking(text)
        items = extract_guidance(self._region(text), ctx)
        assert items
        g = items[0]
        assert g.canonical_metric == "capex"
        assert g.target_value == 3000.0
        assert g.target_period in ("FY28", "FY28")

    def test_boilerplate_disclaimer_is_not_guidance(self, ctx):
        text = ("This document contains forward-looking statements which may "
                "differ materially from actual results.")
        assert not is_forward_looking(text)

    def test_commentary_topics(self, ctx):
        c = extract_commentary(self._region(
            "Operating revenue increased driven by strong demand across the "
            "retail segment, while margins improved on better realisation."),
            ctx)
        assert c is not None
        assert c.primary_topic in {t for t in CommentaryTopic}
        assert c.sentiment in ("positive", "neutral", "mixed", "negative")

    def test_classify_topics_picks_the_dominant_one(self):
        primary, topics = classify_topics(
            "Net debt reduced further and leverage improved; debt levels are "
            "now lower.")
        assert primary == CommentaryTopic.DEBT


class TestConfidence:
    def test_band_thresholds(self):
        assert conf_mod.band_for(0.95) == ConfidenceBand.HIGH
        assert conf_mod.band_for(0.70) == ConfidenceBand.MEDIUM
        assert conf_mod.band_for(0.50) == ConfidenceBand.LOW
        assert conf_mod.band_for(0.10) == ConfidenceBand.REQUIRES_REVIEW

    def test_poor_ocr_caps_the_score(self):
        bd = conf_mod.score(conf_mod.Signals(
            ocr=0.30, layout=1.0, structure=1.0, semantic=1.0))
        assert bd.overall <= 0.55

    def test_corroboration_and_duplicates_help(self):
        base = conf_mod.score(conf_mod.Signals(
            ocr=0.9, layout=0.9, structure=0.9, semantic=0.9))
        better = conf_mod.score(conf_mod.Signals(
            ocr=0.9, layout=0.9, structure=0.9, semantic=0.9,
            corroborated=True, duplicate_agreements=2))
        assert better.overall > base.overall

    def test_failed_validation_forces_review(self):
        bd = conf_mod.score(conf_mod.Signals(
            ocr=0.99, layout=0.99, structure=0.99, semantic=0.99))
        assert bd.band == ConfidenceBand.HIGH
        after = conf_mod.apply_validation(bd, ValidationStatus.FAILED)
        assert after.overall <= 0.35
        assert conf_mod.requires_review(after, ValidationStatus.FAILED)

    def test_warning_also_forces_review(self):
        bd = conf_mod.score(conf_mod.Signals(
            ocr=0.95, layout=0.95, structure=0.95, semantic=0.95))
        after = conf_mod.apply_validation(bd, ValidationStatus.WARNING)
        assert conf_mod.requires_review(after, ValidationStatus.WARNING)
