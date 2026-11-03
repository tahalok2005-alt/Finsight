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
from app.normalization.numbers import (
    detect_magnitude, infer_document_units, normalize_number,
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
        "Particulars",
    ])
    def test_unknown_labels_are_not_forced(self, label):
        assert match_metric(label) is None

    def test_a_component_line_is_not_claimed_by_a_short_alias(self):
        """The 6-character alias "income" must not claim an OCI tax line by
        prefix. Since Phase 21 the line is a Schedule III line of its own."""
        m = match_metric("Income tax relating to items that will not be "
                         "reclassified to Profit or Loss")
        assert m is not None and m.key == "oci_tax_not_reclassified"
        m = match_metric("Income tax relating to items that will be "
                         "reclassified to Profit or Loss")
        assert m is not None and m.key == "oci_tax_reclassified"

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

    def test_a_negated_label_never_matches_its_opposite(self):
        """"Total non-current assets" is not a near-miss for current assets.

        The two strings differ by one short token and score 0.91 on similarity,
        which is well over the match threshold — so before the contrast rule
        the non-current half of a balance sheet was reported as the current
        half.  Since Phase 21 the non-current totals are metrics of their own;
        what must never happen is either half reaching the other.
        """
        assert match_metric("Total non-current assets").key == "non_current_assets"
        assert match_metric("Total non-current liabilities").key == \
            "non_current_liabilities"
        assert match_metric("Non-current investments") is None

        # The rule must not cost the unqualified labels their match.
        assert match_metric("Total current assets").key == "current_assets"
        assert match_metric("Total current liabilities").key == \
            "current_liabilities"

    def test_contrast_words_do_not_block_declared_aliases(self):
        """The rule governs guesses, not the taxonomy's own statements."""
        assert match_metric("Profit Before Tax").key == "pbt"
        assert match_metric("Profit After Tax").key == "pat"
        assert match_metric("Diluted Earnings Per Share").key == "diluted_eps"

    def test_a_label_whose_spaces_ocr_lost_keeps_its_qualifier(self):
        """A degraded scan reads "Profit Before Tax" as "ProfitBeforeTax".

        Looking for the qualifier between spaces finds none there and calls the
        label unqualified, which turns a rule meant to catch opposites into one
        that rejects a correct match. It cost 32 facts on the scanner-degraded
        benchmark before the qualifier was looked for in the text itself.
        """
        assert match_metric("ProfitBeforeTax").key == "pbt"
        assert match_metric("ProfitAfterTax").key == "pat"
        assert match_metric("Totalnon-currentassets").key == "non_current_assets"

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

# ------------------------------------------------ §6 unit and value-type truth
def _unit_table(*, rows, caption, unit_hint, currency_hint,
                headers=("Particulars", "1Q FY27"),
                periods=("Q1 FY27",), uom_header=None,
                document_id="unitdoc", table_id="ut1", page=1,
                title="Statement of Operations") -> ExtractedTable:
    """A small statement table built the way the reconstructor builds them.

    ``rows`` is a list of ``(label, uom, value...)`` tuples; ``uom`` is the
    per-row unit-of-measure cell and is omitted from the grid entirely unless
    ``uom_header`` is given, exactly as a real table without that column.
    """
    head = list(headers)
    if uom_header:
        head = [head[0], uom_header, *head[1:]]
    cells: list[TableCell] = []
    for c_i, h in enumerate(head):
        cells.append(TableCell(
            id=f"{table_id}h{c_i}", table_id=table_id, document_id=document_id,
            page_number=page, row=0, col=c_i, text=h,
            role=CellRole.CORNER if c_i == 0 else CellRole.COLUMN_HEADER,
            bbox=BBoxModel(x=100 * c_i, y=0, width=90, height=12),
            confidence=0.95))
    for r_i, row in enumerate(rows, start=1):
        label, uom, *values = row
        line = [label] + ([uom] if uom_header else []) + list(values)
        for c_i, val in enumerate(line):
            numeric = c_i >= (2 if uom_header else 1)
            parsed = None
            if numeric:
                try:
                    parsed = float(val.replace(",", "").replace("%", "")
                                   .replace("(", "-").replace(")", ""))
                except ValueError:
                    parsed = None
            cells.append(TableCell(
                id=f"{table_id}c{r_i}_{c_i}", table_id=table_id,
                document_id=document_id, page_number=page, row=r_i, col=c_i,
                text=val,
                role=CellRole.ROW_LABEL if c_i == 0 else CellRole.DATA,
                bbox=BBoxModel(x=100 * c_i, y=20 * r_i, width=90, height=12),
                confidence=0.93, numeric_value=parsed,
                is_numeric=parsed is not None))
    columns = [TableColumn(index=0, header=head[0], numeric_ratio=0.0)]
    offset = 1
    if uom_header:
        columns.append(TableColumn(index=1, header=uom_header, numeric_ratio=0.0))
        offset = 2
    for i, period in enumerate(periods):
        columns.append(TableColumn(index=offset + i, header=head[offset + i],
                                   normalized_period=period, numeric_ratio=1.0))
    return ExtractedTable(
        id=table_id, document_id=document_id, page_number=page, region_id="r1",
        title=title, caption=caption,
        bbox=BBoxModel(x=0, y=0, width=400, height=120),
        n_rows=len(rows) + 1, n_cols=len(head), columns=columns, cells=cells,
        confidence=0.95, unit_hint=unit_hint, currency_hint=currency_hint)


def _ctx(unit=None, currency=None, document_id="unitdoc"):
    return TableFactContext(
        document_id=document_id, entity="Consolidated",
        calendar=DEFAULT_CALENDAR, default_currency=currency,
        default_unit=unit)


def _by_metric(facts):
    return {f.canonical_metric: f for f in facts}


class TestUnitAndValueTypeEvidence:
    """What the document says about a figure beats what its name suggests.

    The taxonomy knows that a metric *called* a margin is usually a rate. That
    is a claim about the name. A statement declaring itself in millions and
    printing a "Gross margin" line of 43,718 is a claim about the figure, and
    where the two disagree the figure wins -- otherwise the fact reads "43,718
    percent", which is not a number any reader would accept and no later stage
    can catch.
    """

    # --- A. the source says percent, and is believed ----------------------
    def test_an_explicit_percent_marker_makes_a_rate(self):
        table = _unit_table(
            rows=[("Gross Revenue", "", "12,450"),
                  ("EBITDA margin (%)", "", "20.0")],
            caption="(Rs in crore)", unit_hint=MagnitudeUnit.CRORE,
            currency_hint=Currency.INR)
        facts = _by_metric(extract_from_table(table, _ctx(MagnitudeUnit.CRORE,
                                                          Currency.INR)))
        margin = facts["ebitda_margin"]
        assert margin.value_type == ValueType.PERCENTAGE
        assert margin.unit == MagnitudeUnit.PERCENT
        assert margin.currency == Currency.UNKNOWN
        # and the monetary row beside it is untouched
        assert facts["gross_revenue"].unit == MagnitudeUnit.CRORE

    def test_a_row_priced_in_cents_does_not_inherit_the_table_magnitude(self):
        """A "$million" statement still prints EPS in cents, and says so.

        Reading 151.6 there as 151.6 million dollars is wrong by eight orders
        of magnitude and wrong confidently: nothing downstream can tell that
        the table's declaration was never meant to reach that row. There is no
        magnitude for a cent, so the figure is reported exactly as printed and
        the label's own "(cents)" is left to say what it is.
        """
        table = _unit_table(
            rows=[("Profit for the period", "", "3,671"),
                  ("Earnings per share (cents)", "", "151.6")],
            caption="($million)", unit_hint=MagnitudeUnit.MILLION,
            currency_hint=Currency.USD)
        facts = _by_metric(extract_from_table(table, _ctx(MagnitudeUnit.MILLION,
                                                          Currency.USD)))
        eps = facts["eps"]
        assert eps.value == 151.6
        assert eps.unit != MagnitudeUnit.MILLION, \
            "the table's magnitude reached a row that declared its own"
        # the monetary row beside it keeps the table's declaration
        assert facts["pat"].unit == MagnitudeUnit.MILLION
        assert facts["pat"].currency == Currency.USD

    def test_a_percent_sign_in_the_cell_is_enough(self):
        table = _unit_table(
            rows=[("Gross margin", "", "46.1%")],
            caption="(In millions)", unit_hint=MagnitudeUnit.MILLION,
            currency_hint=Currency.USD)
        margin = _by_metric(extract_from_table(
            table, _ctx(MagnitudeUnit.MILLION, Currency.USD)))["gross_margin"]
        assert margin.value_type == ValueType.PERCENTAGE
        assert margin.unit == MagnitudeUnit.PERCENT

    # --- B. the same metric name, but the source says money ---------------
    def test_a_margin_line_in_a_monetary_statement_stays_monetary(self):
        """Pins the reported defect: an income statement's "Gross margin" is a
        currency amount, and the taxonomy calling that metric a percentage must
        not turn 43,718 million into 43,718 percent."""
        table = _unit_table(
            rows=[("Net sales", "", "94,036"),
                  ("Cost of sales", "", "50,318"),
                  ("Gross margin", "", "43,718")],
            caption="(In millions)", unit_hint=MagnitudeUnit.MILLION,
            currency_hint=Currency.USD)
        facts = _by_metric(extract_from_table(
            table, _ctx(MagnitudeUnit.MILLION, Currency.USD)))
        margin = facts["gross_margin"]
        assert margin.value == 43718.0
        assert margin.value_type == ValueType.MONETARY
        assert margin.unit == MagnitudeUnit.MILLION
        assert margin.currency == Currency.USD
        assert margin.base_value == 43718.0 * 1e6
        # and the decision is recorded rather than made silently
        assert margin.meta.get("taxonomy_rate_overridden") is True

    def test_a_plausible_rate_is_never_reclassified(self):
        """The size of the figure only ever rescues a value that *cannot* be a
        rate.  A margin row printed without its "(%)" still reads 20.0 as a
        percentage, because 20.0 is a perfectly good percentage."""
        table = _unit_table(
            rows=[("EBITDA margin", "", "20.0")],
            caption="(Rs in crore)", unit_hint=MagnitudeUnit.CRORE,
            currency_hint=Currency.INR)
        margin = _by_metric(extract_from_table(
            table, _ctx(MagnitudeUnit.CRORE, Currency.INR)))["ebitda_margin"]
        assert margin.value_type == ValueType.PERCENTAGE
        assert margin.unit == MagnitudeUnit.PERCENT
        assert "taxonomy_rate_overridden" not in margin.meta

    def test_without_a_declaration_nothing_is_reclassified(self):
        """A table that declares no unit offers nothing to prefer, so the
        taxonomy's reading stands rather than being replaced by a guess."""
        table = _unit_table(
            rows=[("Gross margin", "", "43,718")],
            caption="Selected data", unit_hint=None, currency_hint=None)
        margin = _by_metric(extract_from_table(table, _ctx()))["gross_margin"]
        assert margin.value_type == ValueType.PERCENTAGE

    # --- C / D. the declared magnitude is the one that is used ------------
    @pytest.mark.parametrize("caption,unit,factor", [
        ("(In millions)", MagnitudeUnit.MILLION, 1e6),
        ("(Rs in thousands)", MagnitudeUnit.THOUSAND, 1e3),
        ("(Rs in crore)", MagnitudeUnit.CRORE, 1e7),
    ])
    def test_the_declared_magnitude_reaches_the_fact(self, caption, unit, factor):
        table = _unit_table(
            rows=[("Net income", "", "79,000")],
            caption=caption, unit_hint=unit, currency_hint=None)
        fact = _by_metric(extract_from_table(table, _ctx(unit)))["net_income"]
        assert fact.unit == unit
        assert fact.value == 79000.0
        assert fact.base_value == 79000.0 * factor

    def test_a_figure_ending_in_a_zero_group_is_not_a_thousands_declaration(self):
        """Pins the reported defect: the comma before the last group of
        "79,000" is a word boundary, so a magnitude pattern written as
        ``\\b000s?\\b`` reads the number itself as a declaration of thousands and
        overrules the statement that declared millions."""
        table = _unit_table(
            rows=[("Net income", "", "79,000"),
                  ("Operating profit", "", "28,202")],
            caption="(In millions)", unit_hint=MagnitudeUnit.MILLION,
            currency_hint=Currency.USD)
        facts = _by_metric(extract_from_table(
            table, _ctx(MagnitudeUnit.MILLION, Currency.USD)))
        assert facts["net_income"].unit == MagnitudeUnit.MILLION
        assert facts["net_income"].base_value == 79000.0 * 1e6
        # its neighbour, whose digits happen not to end in zeros, always worked
        assert facts["ebit"].unit == MagnitudeUnit.MILLION

    def test_a_list_marker_is_not_a_magnitude(self):
        """Indian filings enumerate statement rows "a)", "b)" ... "k)", and a
        "k" matched as a bare word reads that marker as thousands.  As a
        magnitude "k" is a suffix on a figure -- "50k" -- never a word."""
        assert detect_magnitude("k) Operating margin (%)") is None
        assert detect_magnitude("k) Revenue from operations") is None
        assert detect_magnitude("50k") == MagnitudeUnit.THOUSAND
        assert detect_magnitude("1.2k") == MagnitudeUnit.THOUSAND

    # --- E. a continuation fragment inherits its parent's unit ------------
    def test_a_continuation_fragment_inherits_the_declared_unit(self):
        """A fragment cut from a longer statement carries no caption of its
        own; the unit reaches it through the document-level default the parent
        established, and must survive the trip."""
        table = _unit_table(
            rows=[("Total assets", "", "331,495")],
            caption="", unit_hint=None, currency_hint=None,
            title="Balance Sheet (continued)", table_id="ut2")
        fact = _by_metric(extract_from_table(
            table, _ctx(MagnitudeUnit.MILLION, Currency.USD)))["total_assets"]
        assert fact.unit == MagnitudeUnit.MILLION
        assert fact.currency == Currency.USD
        assert fact.base_value == 331495.0 * 1e6

    # --- F. a row's own unit beats the table's -----------------------------
    def test_a_row_unit_of_measure_cell_beats_the_table_declaration(self):
        """The narrower statement wins.  A table declared in crore may still
        hold a row measured in millions and a row that is a plain count, and
        the row's own unit-of-measure cell is the more specific evidence."""
        table = _unit_table(
            rows=[("Gross Revenue", "Million", "12,450"),
                  ("EBITDA", "Cr", "2,490"),
                  ("Stores", "Number", "3,120")],
            caption="(Rs in crore)", unit_hint=MagnitudeUnit.CRORE,
            currency_hint=Currency.INR, uom_header="UoM")
        facts = _by_metric(extract_from_table(
            table, _ctx(MagnitudeUnit.CRORE, Currency.INR)))
        # the row overrides the table
        assert facts["gross_revenue"].unit == MagnitudeUnit.MILLION
        assert facts["gross_revenue"].value == 12450.0
        # the row that agrees with the table keeps the table's unit
        assert facts["ebitda"].unit == MagnitudeUnit.CRORE
        # and a plain count is not money in any unit
        assert facts["store_count"].unit == MagnitudeUnit.NONE
        assert facts["store_count"].value_type == ValueType.COUNT

    def test_a_magnitude_alone_does_not_make_a_count_into_money(self):
        """"396 million registered customers" is a count of 396 million.

        A magnitude word is a scale, not a currency, but it is enough to type a
        figure as monetary -- and the document's currency then rides along, so
        a customer base gets reported in rupees.  Only a currency on the token
        itself makes a figure money.
        """
        counted = normalize_number(
            "396 million", context="RRVL had 396 million registered customers",
            default_currency=Currency.INR, default_magnitude=MagnitudeUnit.CRORE,
            expected_type=ValueType.COUNT)
        assert counted.value_type == ValueType.COUNT
        assert counted.magnitude == MagnitudeUnit.MILLION   # the scale survives
        assert counted.currency == Currency.UNKNOWN         # the currency does not

        # a currency written on the token is the document speaking, and wins
        priced = normalize_number(
            "Rs 396 million", context="fee income of Rs 396 million",
            default_currency=Currency.INR, expected_type=ValueType.COUNT)
        assert priced.value_type == ValueType.MONETARY
        assert priced.currency == Currency.INR

    def test_a_declaration_carve_out_does_not_set_the_document_unit(self):
        """"(In millions, except number of shares, which are reflected in
        thousands)" declares millions.  What follows "except" describes the
        excluded item, and counting it as the statement's own unit reports the
        whole filing a thousand times too small -- which is exactly what
        happens once OCR damages the leading word of the real declaration."""
        assert infer_document_units(
            ["(In millions, except number of shares, "
             "which are reflected in thousands, and per-share amounts)"]
        )[1] == MagnitudeUnit.MILLION
        # a document that really is in thousands is still read as thousands
        assert infer_document_units(["(Rs in thousands)"])[1] == \
            MagnitudeUnit.THOUSAND
        # and the misread that made this reachable: "In" seen as "ln"
        assert infer_document_units(
            ["(ln millions, except number of shares, reflected in thousands)",
             "(In millions)"])[1] == MagnitudeUnit.MILLION

    def test_prose_does_not_vote_on_the_document_currency(self):
        """"custome-rs", "numbe-rs", "quarte-rs": the letters, not the unit.

        A declaration is "(Rs. in crore)". The bare letters "rs" inside an
        ordinary English word are not one, and counting them cast eighty
        phantom rupee votes on a deck that reports in US dollars -- after which
        every figure that fell back to the document's currency was wrong by an
        exchange rate rather than merely unlabelled.
        """
        prose = ["39 million customers served through these channels",
                 "Numbers are gross of IBPC and consumer loan assignments",
                 "Figures for prior quarters have been regrouped"]
        assert infer_document_units(prose)[0] is None
        # the deck's own declarations still decide it
        assert infer_document_units(prose + ["(US$ Mn.)", "US$ 114 Mn."])[0] \
            == Currency.USD
        # and a genuine rupee declaration is unaffected
        assert infer_document_units(prose + ["(Rs. in crore)"])[0] \
            == Currency.INR
