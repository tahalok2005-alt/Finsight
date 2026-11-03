"""Phase 22: figures read from prose and highlight tiles.

A human audit of every prose figure in the corpus (section 40) found about
half wrong in some dimension and none flagged. Each test is one of the wrong
readings, restated in neutral wording: the date's day taken as the amount, a
basis-point rate taken as crore, the earlier side of "from X to Y", the change
after "grew by", a conversion's dollar sign given to a rupee figure, EBIT for
a defined EBITDA, a per-store rate as the total, a component's figure from the
auditor's sentence as the group's. And the policy that follows from the
audit: a prose or tile figure no table confirms is put in front of a person.
"""
from __future__ import annotations

import pytest

from app.extraction.confidence import review_reason_for
from app.extraction.narrative import NarrativeContext, extract_narrative_facts
from app.models.enums import (
    Currency, FactKind, MagnitudeUnit, RegionType, ReviewReason, ValidationStatus, ValueType,
)
from app.models.schemas import BBoxModel, Region
from app.normalization.periods import DEFAULT_CALENDAR, date_like_spans, is_date_like
from app.spatial.extract import CardContext, read_label_and_value


@pytest.fixture
def ctx():
    return NarrativeContext(
        document_id="doc1", entity="Consolidated", calendar=DEFAULT_CALENDAR,
        default_currency=Currency.INR, default_unit=MagnitudeUnit.CRORE,
        default_period="Q1 FY27")


def _facts(text, ctx):
    region = Region(id="r1", document_id="doc1", page_number=1, type=RegionType.PARAGRAPH,
                    bbox=BBoxModel(x=0, y=0, width=400, height=40), confidence=0.9,
                    reading_order=1, text=text)
    return [f for f in extract_narrative_facts(region, ctx) if f.value is not None]


def _values(text, ctx, metric=None):
    return [f.value for f in _facts(text, ctx) if metric is None or f.canonical_metric == metric]


class TestDates:
    @pytest.mark.parametrize("text", ["December 31, 2025", "30 Jun 26", "31.03.2026",
                                      "Q3FY26", "Q1 FY27", "JQ 25", "June 2026", "FY27", "H1 2026"])
    def test_date_tokens(self, text):
        assert is_date_like(text)

    @pytest.mark.parametrize("text", ["margin 12", "2,448 Cr", "57%", "Rs 340,257 crore", "may rise 5"])
    def test_not_dates(self, text):
        assert not is_date_like(text)

    def test_spans_cover_the_day(self):
        s = "Networth as at December 31, 2025 was 175,251 crore"
        (a, b), = date_like_spans(s)
        assert s[a:b] == "December 31, 2025"


class TestFigureSelection:
    def test_the_day_of_a_date_is_not_the_amount(self, ctx):
        assert _values("Consolidated Networth as at December 31, 2025 was  175,251 crore.",
                       ctx, "net_worth") == [175251.0]

    def test_a_basis_point_rate_is_not_an_amount(self, ctx):
        assert _values("Overall Net Payment Revenue has reduced from 8.8bps to 8.4bps of GMV YoY.",
                       ctx) == []

    def test_per_cent_written_out_is_a_rate(self, ctx):
        assert _values("Revenue grew 12 per cent to Rs 1,200 crore.", ctx) == [1200.0]

    def test_the_later_side_of_from_x_to_y(self, ctx):
        assert _values("Adjusted EBITDA losses increased from INR 45 crore to INR 94 crore.",
                       ctx) == [94.0]

    def test_a_change_is_not_the_level(self, ctx):
        assert _values("Cash balance increased by INR 316 crore QoQ to INR 18,288 crore.",
                       ctx, "cash") == [18288.0]

    def test_a_comparative_in_brackets_is_not_the_figure(self, ctx):
        vals = _values("Finance costs for the quarter were Rs. 12 crores (JQ 25: Rs. 34 crores).", ctx)
        assert vals == [12.0]

    def test_a_period_label_digit_is_not_the_figure(self, ctx):
        vals = _values("Provisions for Q3FY26 was  810 crore ( 794 crore in Q3FY25 and  947 crore in Q2FY26).",
                       ctx)
        assert vals == [810.0]

    def test_a_conversion_does_not_lend_its_currency(self, ctx):
        facts = _facts("Gross revenue at R244.8 billion ($2,585.9 million), an increase of 1.0% QoQ.", ctx)
        assert facts and all(f.currency != Currency.USD for f in facts)

    def test_a_defined_abbreviation_names_the_measure(self, ctx):
        facts = _facts("Earnings before interest, tax, depreciation and amortization (EBITDA) for the "
                       "quarter at Rs. 3,947 crores (JQ 25: Rs. 3,640 crores)", ctx)
        assert [(f.canonical_metric, f.value) for f in facts] == [("ebitda", 3947.0)]

    def test_a_rate_per_unit_is_not_the_total(self, ctx):
        assert _values("Capex per store (including warehousing): INR 2.5 crore", ctx) == []

    def test_a_components_figure_in_the_auditors_sentence(self, ctx):
        assert _values("We did not review subsidiaries whose interim financial results reflect total "
                       "revenues (before consolidation adjustments) of Rs. 42.04 crores.", ctx) == []

    def test_a_compound_scale_is_refused_not_misread(self, ctx):
        facts = _facts("Quarterly average AUM grew by 1.21 Lakh Crore from R11.40 Lakh Crore in "
                       "Q1FY26 to R12.61 Lakh Crore in Q1FY27", ctx)
        assert not [f for f in facts if f.unit == MagnitudeUnit.LAKH]

    def test_an_amount_is_never_a_ratio(self, ctx):
        facts = _facts("Credit Quality GNPA and NNPA stood at 0.29% and 0.12% respectively "
                       "Treasury Borrowings stood at  1,10,546 crore", ctx)
        assert not [f for f in facts if f.canonical_metric in ("gross_npa_ratio", "net_npa_ratio")
                    and f.value_type == ValueType.MONETARY]

    def test_a_per_share_figure_takes_no_scale(self, ctx):
        facts = _facts("Earnings per share for the quarter at Rs. 3.20, up 4% QoQ.", ctx)
        assert facts and facts[0].unit in (MagnitudeUnit.NONE, None)

    def test_a_figure_far_from_its_name_is_not_taken(self, ctx):
        assert _values("Under the offer the Bank sold equity shares of its subsidiary at 740 per share "
                       "for a consideration aggregating to Rs 10,000.00 crore.", ctx, "net_worth") == []

    def test_a_part_of_the_measure_is_not_the_measure(self, ctx):
        # a press release's one-time gain was filed as the quarter's profit
        assert _values("Consolidated profit for the quarter includes onetime gain of "
                       "Rs. 722 Crore on sale of investment", ctx) == []

    def test_the_whole_is_not_taken_from_of_which(self, ctx):
        assert _values("Revenue from operations, of which exports were Rs 1,200 crore", ctx) == []

    def test_the_plain_sentence_still_reads(self, ctx):
        facts = _facts("Net Advances increased 16% YoY to  480,673 crore as at December 31, 2025 "
                       "from  413,839 crore as at December 31, 2024.", ctx)
        assert [(f.value, f.growth) for f in facts] == [(480673.0, 16.0)]


class TestTiles:
    def test_a_date_under_a_tile_label_is_not_a_value(self):
        c = CardContext(document_id="d", calendar=DEFAULT_CALENDAR, default_currency=Currency.INR,
                        default_unit=MagnitudeUnit.CRORE)
        assert read_label_and_value("AUM", "30 Jun 26", c) == "value_is_a_date"


class TestReviewPolicy:
    def test_a_prose_figure_no_table_confirms_is_reviewed(self, ctx):
        f = _facts("Revenue increased 24.5% Y-o-Y to Rs 340,257 crore.", ctx)[0]
        assert f.kind == FactKind.NARRATIVE
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) == ReviewReason.NARRATIVE_UNCONFIRMED

    def test_a_prose_figure_a_table_confirms_is_not(self, ctx):
        f = _facts("Revenue increased 24.5% Y-o-Y to Rs 340,257 crore.", ctx)[0]
        f.meta["corroborating_sources"] = ["narrative_sentence", "table_cell"]
        assert review_reason_for(f, ValidationStatus.NOT_CHECKED) != ReviewReason.NARRATIVE_UNCONFIRMED


def test_the_auditors_component_paragraph_in_other_words(ctx):
    assert _values("One subsidiary, whose unaudited interim financial result and other financial "
                   "information includes total revenues of Rs. Nil, total net loss after tax of Rs. 0.18 "
                   "crore and total comprehensive loss of Rs. 0.18 crore.", ctx) == []


class TestEveryMeasureInASentence:
    """A results list has no full stop in it, so it arrives as one sentence."""

    def test_a_list_of_measures_is_read_in_full(self, ctx):
        facts = _facts("Revenue from operations stood at Rs. 9,579 Crore , EBITDA was "
                       "Rs. 1,382 Crore , Profit before tax was Rs. 2,126 Crore and Profit "
                       "after tax was Rs. 1,543 Crore", ctx)
        assert [(f.canonical_metric, f.value) for f in facts] == [
            ("revenue_from_operations", 9579.0), ("ebitda", 1382.0),
            ("pbt", 2126.0), ("pat", 1543.0)]

    def test_a_measure_mentioned_in_passing_gets_no_figure(self, ctx):
        # prose, not a list: EBITDA is named but has no figure of its own, so
        # the sentence is read as being about revenue alone
        facts = _facts("Revenue from operations grew to Rs. 9,579 Crore , driven by "
                       "EBITDA expansion", ctx)
        assert [(f.canonical_metric, f.value) for f in facts] == [
            ("revenue_from_operations", 9579.0)]

    def test_a_figure_after_the_next_name_is_not_borrowed(self, ctx):
        facts = _facts("Revenue from operations Rs. 9,579 Crore EBITDA Rs. 1,382 Crore "
                       "Profit after tax Rs. 1,543 Crore", ctx)
        assert [f.value for f in facts] == [9579.0, 1382.0, 1543.0]

    def test_a_growth_rate_belongs_to_its_own_measure(self, ctx):
        facts = _facts("Revenue from operations at Rs. 9,579 Crore , up 12% YoY , "
                       "EBITDA at Rs. 1,382 Crore", ctx)
        growth = {f.canonical_metric: f.growth for f in facts}
        assert growth.get("revenue_from_operations") == 12.0
        assert growth.get("ebitda") is None
