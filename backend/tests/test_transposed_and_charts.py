"""Drawn grids of figures, and what a chart can honestly be asked for.

The grid geometry in these tests is taken from real pages: a deck's
"Progress since merger" panel, a filing's statement of profit and loss, and the
balance sheet whose year-on-year column sits beside three dated ones. Every
refusal asserted here is a wrong answer the detector gave before the rule that
prevents it existed.
"""
from __future__ import annotations

import pytest

from app.charts.triage import ChartEvidence, gather, summarise
from app.core.geometry import BBox
from app.models.enums import ChartType, Currency, MagnitudeUnit, ValueType
from app.models.schemas import BBoxModel, ExtractedChart, OCRElement
from app.normalization.evidence import (
    Claim, Level, resolve_value_type,
)
from app.normalization.periods import FiscalCalendar
from app.spatial.extract import CardContext, extract_from_grid
from app.spatial.transposed import detect_transposed

MARCH = FiscalCalendar(year_end_month=3)
PAGE_WIDTH = 2667
PAGE_HEIGHT = 1500
LINE = 40


def el(text: str, x: float, y: float, *, w: float | None = None,
       h: float = LINE, conf: float = 0.92) -> OCRElement:
    width = w if w is not None else max(60.0, len(text) * 17.0)
    return OCRElement(
        id=f"e{int(x)}_{int(y)}_{abs(hash(text)) % 99999}", document_id="d1",
        page_number=1, text=text, confidence=conf,
        bbox=BBoxModel(x=x, y=y, width=width, height=h))


def grid(headers: list[str], rows: list[tuple[str, list[str]]], *,
         label_x: float = 200, first_col: float = 900,
         col_gap: float = 380, top: float = 300,
         row_gap: float = 90) -> list[OCRElement]:
    """A drawn table: period heads across, metric labels down, figures inside."""
    out: list[OCRElement] = []
    for i, h in enumerate(headers):
        out.append(el(h, first_col + i * col_gap, top, w=200))
    for r, (label, values) in enumerate(rows):
        y = top + row_gap * (r + 1)
        out.append(el(label, label_x, y, w=500))
        for i, v in enumerate(values):
            out.append(el(v, first_col + i * col_gap, y, w=200))
    return out


def find(elements, **kw):
    return detect_transposed(elements, page_number=1,
                             page_width=PAGE_WIDTH, **kw)


def read(elements, *, default_unit=MagnitudeUnit.CRORE,
         default_currency=Currency.INR, **ctx_kw):
    """Read a grid the way the pipeline does: with the document's declared
    scale available as a fallback. A bare "10,000" in a statement headed
    "Rs. crore" is ten thousand crore; with no declaration anywhere it has no
    scale at all, and the shared rules refuse it."""
    finding = find(elements)
    ctx = CardContext(document_id="d1", calendar=MARCH,
                      default_unit=default_unit,
                      default_currency=default_currency, **ctx_kw)
    return finding, extract_from_grid(finding, ctx)


# ------------------------------------------------------------ drawn grids
class TestTransposedGrids:

    def test_a_grid_of_metric_rows_and_period_columns_is_read(self):
        finding, out = read(grid(
            ["Q1 FY27", "Q4 FY26", "Q1 FY26"],
            [("Revenue", ["10,000", "9,500", "8,900"]),
             ("Profit After Tax", ["2,000", "1,800", "1,600"])]))
        assert finding.orientation == "metric_rows_period_columns"
        by = {(f.canonical_metric, f.period): f.value for f in out.facts}
        assert by[("revenue", "Q1 FY27")] == 10000.0
        assert by[("revenue", "Q4 FY26")] == 9500.0
        assert by[("pat", "Q1 FY26")] == 1600.0

    def test_each_column_keeps_its_own_period(self):
        """One period for the whole table is the defect this replaces."""
        _, out = read(grid(
            ["Q1 FY27", "Q4 FY26", "Q1 FY26"],
            [("Revenue", ["10,000", "9,500", "8,900"]),
             ("Profit After Tax", ["2,000", "1,800", "1,600"])]))
        periods = {f.period for f in out.facts}
        assert periods == {"Q1 FY27", "Q4 FY26", "Q1 FY26"}

    def test_repeated_structure_is_what_makes_it_a_grid(self):
        finding = find(grid(
            ["Q1 FY27", "Q4 FY26", "Q1 FY26"],
            [("Revenue", ["10,000", "9,500", "8,900"]),
             ("Profit After Tax", ["2,000", "1,800", "1,600"]),
             ("Total Assets", ["50,000", "48,000", "45,000"])]))
        assert finding.rows >= 3 and finding.columns >= 3
        assert finding.fill >= 0.7

    def test_an_isolated_pair_is_not_a_grid(self):
        finding = find([el("Revenue", 200, 300), el("10,000", 900, 300),
                        el("Some prose about the quarter", 200, 500, w=900)])
        assert finding.candidates == []

    def test_a_half_empty_lattice_is_refused(self):
        scattered = [el("Revenue", 200, 300), el("10,000", 900, 300),
                     el("2,000", 1660, 700), el("Profit After Tax", 200, 700),
                     el("8,900", 2400, 1100), el("Total Assets", 200, 1100),
                     el("Notes", 200, 1300), el("45,000", 900, 1500)]
        finding = find(scattered)
        assert finding.candidates == []
        assert "grid_not_filled" in finding.rejected or finding.notes

    def test_mixed_monetary_and_percentage_rows_each_keep_their_kind(self):
        _, out = read(grid(
            ["Q1 FY27", "Q4 FY26", "Q1 FY26"],
            [("Revenue", ["US$ 10,000 Mn.", "US$ 9,500 Mn.", "US$ 8,900 Mn."]),
             ("Net Interest Margin", ["5.96%", "5.71%", "5.40%"])]))
        by = {f.canonical_metric: f for f in out.facts if f.period == "Q1 FY27"}
        assert by["revenue"].value_type == ValueType.MONETARY
        assert by["net_interest_margin"].value_type == ValueType.PERCENTAGE
        assert by["net_interest_margin"].unit != MagnitudeUnit.MILLION


class TestTransposedRefusals:

    def test_a_growth_column_is_not_a_period_value(self):
        """A balance sheet's rows read 4,088 / 5,000 / 5,137 / 25.7 %.

        The last is the year-on-year change. Read as a fourth balance it is
        both the wrong kind of number and dated to the quarter beside it.
        """
        finding = find(grid(
            ["Jun-25", "Mar-26", "Jun-26", "YoY"],
            [("Shareholders' Funds", ["4,088", "5,000", "5,137", "25.7%"]),
             ("Deposits", ["27,977", "31,092", "32,931", "17.7%"]),
             ("Advances", ["24,000", "29,000", "31,447", "31.0%"])]))
        values = {c.value_text for c in finding.candidates}
        assert "25.7%" not in values
        assert "17.7%" not in values
        assert finding.rejected.get("figure_disagrees_with_its_row")

    def test_a_header_serving_two_columns_dates_neither(self):
        """A quarter-ended head sitting between two columns identifies neither.

        On a real filing it sat between the quarter column and the year-ended
        column beside it; taking it for both dated a full year as a quarter.
        """
        elements = grid(
            ["Q1 FY27", "Q4 FY26"],
            [("Revenue", ["10,000", "9,500"]),
             ("Profit After Tax", ["2,000", "1,800"])])
        # move the second head so it sits midway between the two columns
        elements = [e for e in elements if e.text != "Q4 FY26"]
        elements.append(el("Q4 FY26", 1090, 300, w=200))
        finding = find(elements)
        dated = [c for c in finding.candidates if c.period is not None]
        assert not dated or len({c.period_text for c in dated}) <= 1

    def test_a_misaligned_header_yields_no_period_rather_than_a_wrong_one(self):
        elements = grid(
            ["Q1 FY27", "Q4 FY26", "Q1 FY26"],
            [("Revenue", ["10,000", "9,500", "8,900"]),
             ("Profit After Tax", ["2,000", "1,800", "1,600"])])
        elements = [e for e in elements if not e.text.startswith("Q")]
        elements.append(el("As at 30 June 2026", 2200, 300, w=400))
        _, out = read(elements)
        assert all(f.period is None for f in out.facts)

    def test_a_grid_of_ratings_produces_nothing(self):
        finding, out = read(grid(
            ["Long term", "Short term"],
            [("CRISIL", ["AAA", "A1+"]),
             ("ICRA", ["AA+", "A1+"]),
             ("CARE Ratings", ["AA+", "A1+"])]))
        assert out.facts == []

    def test_a_table_of_contents_produces_nothing(self):
        _, out = read(grid(
            ["Page"],
            [("Background", ["3"]), ("Liabilities", ["8"]),
             ("Assets", ["19"]), ("Asset Quality", ["32"])]))
        assert out.facts == []


# --------------------------------------------------- shared unit evidence
class TestValueTypeEvidence:
    """Source evidence beats the taxonomy, and the nearest source wins."""

    def test_the_cell_beats_the_taxonomy(self):
        """"CASA deposits 50.8 %" is a percentage, whatever the metric means.

        The spatial path always refused this; the table path recorded it as an
        amount of money.
        """
        v = resolve_value_type([
            Claim(ValueType.MONETARY, Level.TAXONOMY),
            Claim(ValueType.PERCENTAGE, Level.CELL, "the figure itself"),
        ])
        assert v.value_type == ValueType.PERCENTAGE
        assert not v.conflicted

    def test_the_row_beats_the_table(self):
        v = resolve_value_type([
            Claim(ValueType.MONETARY, Level.TABLE),
            Claim(ValueType.PERCENTAGE, Level.ROW),
        ])
        assert v.value_type == ValueType.PERCENTAGE

    def test_the_cell_beats_the_row(self):
        v = resolve_value_type([
            Claim(ValueType.PERCENTAGE, Level.ROW),
            Claim(ValueType.MONETARY, Level.CELL),
        ])
        assert v.value_type == ValueType.MONETARY

    def test_two_claims_at_one_level_conflict_rather_than_pick(self):
        v = resolve_value_type([
            Claim(ValueType.MONETARY, Level.CELL),
            Claim(ValueType.PERCENTAGE, Level.CELL),
        ])
        assert v.conflicted
        assert v.value_type == ValueType.UNKNOWN

    def test_nothing_stated_says_so(self):
        v = resolve_value_type([Claim(ValueType.UNKNOWN, Level.TAXONOMY)])
        assert v.value_type == ValueType.UNKNOWN
        assert not v.conflicted

    def test_a_rate_never_takes_a_document_scale(self):
        v = resolve_value_type([Claim(ValueType.PERCENTAGE, Level.CELL)])
        assert not v.takes_a_document_scale

    def test_a_per_share_figure_never_takes_a_document_scale(self):
        v = resolve_value_type([Claim(ValueType.PER_SHARE, Level.TAXONOMY)])
        assert not v.takes_a_document_scale

    def test_an_amount_does_take_one(self):
        v = resolve_value_type([Claim(ValueType.MONETARY, Level.TABLE)])
        assert v.takes_a_document_scale


# -------------------------------------------------------------- chart triage
def chart(page: int = 1, *, x=200.0, y=300.0, w=1200.0, h=600.0,
          kind: ChartType = ChartType.COLUMN, legend=None) -> ExtractedChart:
    return ExtractedChart(
        id=f"chart_{page}_{int(x)}", document_id="d1", page_number=page,
        region_id="r1", chart_type=kind, type_confidence=0.8, title="",
        legend_labels=list(legend or []),
        bbox=BBoxModel(x=x, y=y, width=w, height=h),
        confidence=0.5, values_extracted=False, requires_review=True, notes="")


class TestChartTriage:

    def _labelled(self) -> list[OCRElement]:
        """A bar chart with printed values and a period axis, labels *outside*
        the plotted ink -- which is where a designer puts them."""
        out = [el("Net Interest Margin", 300, 250, w=500)]
        for i, (v, p) in enumerate([("5.96%", "Q1 FY27"), ("5.71%", "Q4 FY26"),
                                    ("5.40%", "Q1 FY26")]):
            out.append(el(v, 400 + i * 300, 270, w=150))
            out.append(el(p, 400 + i * 300, 930, w=180))
        return out

    def test_labels_outside_the_plot_are_still_found(self):
        """Containment kept none of seven printed percentages on a real page."""
        ev = gather(chart(), self._labelled(),
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert ev.has_data_labels
        assert ev.has_period_labels

    def test_a_recognised_metric_is_required(self):
        """"In US$ Mn." and "#Excluding Money Market Borrowings" are not
        metrics. Asking only whether text was wordy passed every chart in a
        75-page deck and not one had a metric name among them."""
        elements = [el("In Uss Mn.", 300, 250, w=300),
                    el("#Excluding Money Market Borrowings", 300, 1000, w=800)]
        for i, v in enumerate(["5.96%", "5.71%", "5.40%"]):
            elements.append(el(v, 400 + i * 300, 270, w=150))
        ev = gather(chart(), elements,
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert not ev.has_metric_labels
        assert not ev.extraction_possible

    def test_a_chart_with_everything_is_extraction_possible(self):
        ev = gather(chart(), self._labelled(),
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert ev.extraction_possible

    def test_more_values_than_periods_cannot_be_associated(self):
        """Seventeen figures against fourteen ticks is an association problem,
        and guessing at it reports one year's figure under another's label."""
        elements = self._labelled()
        elements.append(el("6.10%", 1400, 270, w=150))
        ev = gather(chart(), elements,
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert ev.has_data_labels and ev.has_period_labels
        assert not ev.extraction_possible

    def test_a_decorative_graphic_is_not_financial(self):
        ev = gather(chart(kind=ChartType.UNKNOWN),
                    [el("IDFC FIRST", 300, 320, w=300)],
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert not ev.likely_financial
        assert not ev.extraction_possible

    def test_an_axis_without_data_labels_is_recorded_and_refused(self):
        """A value axis is a scale to interpolate against. Reading a bar
        against it means measuring pixels, which this system does not do."""
        elements = [el("Net Interest Margin", 300, 250, w=500)]
        for i, tick in enumerate(["0", "20", "40", "60"]):
            elements.append(el(tick, 250, 350 + i * 130, w=80))
        for i, p in enumerate(["Q1 FY27", "Q4 FY26"]):
            elements.append(el(p, 500 + i * 300, 930, w=180))
        ev = gather(chart(), elements,
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert ev.has_numeric_axis
        assert not ev.extraction_possible

    def test_a_legend_entry_never_counts_as_a_metric(self):
        ev = gather(chart(legend=["Revenue", "PAT"]), self._labelled(),
                    page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        assert "Revenue" not in ev.metric_labels

    def test_the_summary_counts_what_was_measured(self):
        good = gather(chart(), self._labelled(),
                      page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        bare = gather(chart(page=2), [el("Logo", 300, 320, w=200)],
                      page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT)
        s = summarise([good, bare])
        assert s["charts"] == 2
        assert s["extraction_possible"] == 1
