"""Reading financial figures that are arranged, not tabulated.

An investor presentation states its headline numbers in tiles rather than rows,
and table reconstruction has nothing to work with there: no grid, no ruling, no
header row. What there is instead is geometry.

These tests build that geometry directly -- elements at explicit coordinates --
so each one pins a single spatial relationship rather than a whole document.
The coordinates are taken from a real results slide: a 4-column grid at
x-centres near 330, 1020, 1710 and 2400, with tiles about 40 px per line.
"""
from __future__ import annotations

import pytest

from app.models.enums import Currency, MagnitudeUnit, PeriodBasis, ValueType
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.spatial.cards import detect_cards
from app.spatial.extract import CardContext, extract_from_cards

MARCH = FiscalCalendar(year_end_month=3)
PAGE_WIDTH = 2560
LINE = 40


def el(text: str, x: float, y: float, *, w: float | None = None,
       h: float = LINE, conf: float = 0.92) -> OCRElement:
    """One recognised element at an explicit position."""
    width = w if w is not None else max(60.0, len(text) * 17.0)
    return OCRElement(
        id=f"e{int(x)}_{int(y)}_{abs(hash(text)) % 9999}", document_id="d1",
        page_number=1, text=text, confidence=conf,
        bbox=BBoxModel(x=x, y=y, width=width, height=h))


def tile(label: str, value: str, *, cx: float, top: float,
         period: str | None = None, change: str | None = None
         ) -> list[OCRElement]:
    """A label above a value, optionally with a period and a change note."""
    out: list[OCRElement] = []
    y = top
    out.append(el(label, cx - len(label) * 8, y))
    y += LINE + 10
    if period:
        out.append(el(period, cx - len(period) * 8, y))
        y += LINE + 10
    out.append(el(value, cx - len(value) * 8, y))
    if change:
        y += LINE + 10
        out.append(el(change, cx - len(change) * 8, y))
    return out


def grid(rows: list[list[tuple]], *, band_gap: float = 260) -> list[OCRElement]:
    """A repeated layout: each row is a band of tiles across the columns."""
    elements: list[OCRElement] = []
    for band, row in enumerate(rows):
        top = 200 + band * band_gap
        for column, spec in enumerate(row):
            label, value = spec[0], spec[1]
            period = spec[2] if len(spec) > 2 else None
            change = spec[3] if len(spec) > 3 else None
            elements.extend(tile(label, value, cx=330 + column * 690,
                                 top=top, period=period, change=change))
    return elements


def find(elements: list[OCRElement]):
    return detect_cards(elements, page_number=1, page_width=PAGE_WIDTH)


def read(elements: list[OCRElement], **ctx_kw):
    finding = find(elements)
    ctx = CardContext(document_id="d1", calendar=MARCH, **ctx_kw)
    return finding, extract_from_cards(finding, ctx)


# ------------------------------------------------------- spatial association
class TestSpatialAssociation:
    """Which number a label names, decided by geometry rather than order."""

    def test_a_label_above_a_value_is_read_as_naming_it(self):
        finding = find(grid([[("Profit After Tax", "US$ 114 Mn."),
                              ("Total Deposits", "US$ 32,931 Mn."),
                              ("Net Interest Margin", "5.96%")]]))
        by_label = {c.label_text: c.value_text for c in finding.cards}
        assert by_label["Profit After Tax"] == "US$ 114 Mn."
        assert by_label["Total Deposits"] == "US$ 32,931 Mn."
        assert by_label["Net Interest Margin"] == "5.96%"

    def test_columns_do_not_borrow_each_other_s_values(self):
        """The nearest number in reading order is the wrong number.

        Read as a sequence, "Profit After Tax" is followed by the *next
        column's* label. Only the shared centre line makes the association
        right, which is the whole reason this works on geometry.
        """
        finding = find(grid([[("Profit After Tax", "114"),
                              ("Total Deposits", "32931"),
                              ("Advances", "31447")]]))
        by_label = {c.label_text: c.value_text for c in finding.cards}
        assert by_label == {"Profit After Tax": "114",
                            "Total Deposits": "32931",
                            "Advances": "31447"}

    def test_two_numbers_in_one_tile_become_two_tiles(self):
        """A footnote between two tiles leaves no whitespace to split on.

        The figures themselves mark the boundary: everything before a number
        describes it.

        The bands are set close together on purpose: at this spacing the gap
        between the tiles is no larger than the gaps inside them, so nothing
        but the figures can separate one from the next.
        """
        finding = find(grid([
            [("Risk Adjusted NII", "4.75%"), ("Total Deposits", "32931")],
            [("Return on Assets", "1.06%"), ("Advances", "31447")],
        ], band_gap=140))
        labels = {c.label_text for c in finding.cards}
        assert "Risk Adjusted NII" in labels
        assert "Return on Assets" in labels
        assert "Total Deposits" in labels
        assert "Advances" in labels

    def test_a_lone_tile_is_not_trusted(self):
        """One label above one number is a guess, not a layout."""
        finding = find(tile("Profit After Tax", "US$ 114 Mn.",
                            cx=330, top=200))
        assert finding.cards == []
        assert any("repeated" in n or "few" in n for n in finding.notes)


# ------------------------------------------------------------- KPI cards
class TestKpiCards:
    """What a tile yields once the taxonomy and normaliser see it."""

    def test_a_currency_tile_keeps_its_own_currency_and_scale(self):
        _, out = read(grid([[("Profit After Tax", "US$ 114 Mn."),
                             ("Total Deposits", "US$ 32,931 Mn."),
                             ("Advances", "US$ 31,447 Mn.")]]))
        pat = next(f for f in out.facts if f.canonical_metric == "pat")
        assert pat.value == 114.0
        assert pat.currency == Currency.USD
        assert pat.unit == MagnitudeUnit.MILLION

    def test_a_percentage_tile_stays_a_percentage(self):
        _, out = read(grid([[("Net Interest Margin", "5.96%"),
                             ("GNPA ratio", "1.51%"),
                             ("Return on Assets", "1.06%")]]))
        nim = next(f for f in out.facts
                   if f.canonical_metric == "net_interest_margin")
        assert nim.value == pytest.approx(5.96)
        assert nim.value_type == ValueType.PERCENTAGE
        assert nim.currency == Currency.UNKNOWN

    def test_a_movement_note_is_kept_as_context_not_as_the_figure(self):
        _, out = read(grid([[("Profit After Tax", "US$ 114 Mn.", None,
                              "(132.4% YoY I 237.0% QoQ)"),
                             ("Total Deposits", "US$ 32,931 Mn.", None,
                              "(18% YoY I 6% QoQ)"),
                             ("Advances", "US$ 31,447 Mn.", None,
                              "(20% YoY I 5% QoQ)")]]))
        pat = next(f for f in out.facts if f.canonical_metric == "pat")
        assert pat.value == 114.0, "the movement note must not become the value"
        assert "YoY" in pat.meta["change_note"]

    def test_every_fact_records_the_strategy_that_found_it(self):
        _, out = read(grid([[("Profit After Tax", "US$ 114 Mn."),
                             ("Total Deposits", "US$ 32,931 Mn."),
                             ("Advances", "US$ 31,447 Mn.")]]))
        assert out.facts
        for f in out.facts:
            assert f.meta["extraction_strategy"] == "spatial_kpi"
            assert f.provenance.extraction_method == "spatial_kpi"
            assert f.provenance.ocr_element_ids, "the tile's own elements"
            assert f.meta["spatial_evidence"]["sibling_tiles_on_the_same_line"]

    def test_a_growth_multiple_is_not_an_amount(self):
        """"2.5X" beside "Total Deposits" is growth, not a balance."""
        _, out = read(grid([[("Total Deposits", "2.5X"),
                             ("Advances", "2.8X"),
                             ("CASA Deposits", "2.4X")]]))
        assert out.facts == []
        assert out.rejected["value_is_a_multiple_not_an_amount"] == 3

    def test_a_share_of_mix_percentage_is_not_a_balance(self):
        """A chart legend reading "Retail Deposits 9%" is not a deposit total."""
        _, out = read(grid([[("Retail Deposits", "9%"),
                             ("Total Deposits", "18%"),
                             ("CASA Deposits", "69%")]]))
        assert not [f for f in out.facts
                    if f.value_type == ValueType.MONETARY]
        assert out.rejected.get("percentage_value_for_a_monetary_metric")

    def test_an_unrecognised_label_yields_nothing(self):
        _, out = read(grid([[("Awards and Accolades", "12"),
                             ("Branches Opened", "45"),
                             ("Cities Covered", "700")]]))
        assert out.rejected.get("label_not_a_known_metric")

    def test_a_monetary_tile_with_no_stated_scale_is_refused(self):
        """No column head, no caption -- nothing says thousands or millions."""
        _, out = read(grid([[("Profit After Tax", "114"),
                             ("Total Deposits", "32931"),
                             ("Advances", "31447")]]))
        assert out.facts == []
        assert out.rejected["no_scale_or_currency_on_a_monetary_figure"] == 3


# --------------------------------------------------------- period evidence
class TestPeriodAssociationIsConservative:
    """The rule the misaligned-header defect taught, applied to geometry."""

    def test_a_period_inside_the_tile_is_used(self):
        _, out = read(grid([[("Profit After Tax", "US$ 114 Mn.", "(Q1-FY27)"),
                             ("Core Operating Profit", "US$ 250 Mn.", "(Q1-FY27)"),
                             ("Return on Assets", "1.06%", "(Q1-FY27)")]]))
        pat = next(f for f in out.facts if f.canonical_metric == "pat")
        assert pat.period == "Q1 FY27"
        assert pat.period_normalized.basis == PeriodBasis.STATED

    def test_a_page_banner_is_inherited_and_says_so(self):
        elements = grid([[("Profit After Tax", "US$ 114 Mn."),
                          ("Core Operating Profit", "US$ 250 Mn."),
                          ("Advances", "US$ 31,447 Mn.")]])
        from app.normalization.periods import parse_period
        banner = parse_period("Q1 FY27", calendar=MARCH)
        _, out = read(elements, page_period=banner,
                      page_period_source="the page banner 'Q1 FY27 results'")
        pat = next(f for f in out.facts if f.canonical_metric == "pat")
        assert pat.period == "Q1 FY27"
        assert pat.period_normalized.basis == PeriodBasis.INHERITED
        assert "banner" in pat.meta["period_inherited_from"]

    def test_a_tile_with_no_period_evidence_gets_none(self):
        _, out = read(grid([[("Profit After Tax", "US$ 114 Mn."),
                             ("Core Operating Profit", "US$ 250 Mn."),
                             ("Advances", "US$ 31,447 Mn.")]]))
        pat = next(f for f in out.facts if f.canonical_metric == "pat")
        assert pat.period is None
        assert pat.meta["period_absence"] == str(PeriodBasis.UNKNOWN)

    def test_a_neighbouring_tile_s_period_is_never_borrowed(self):
        """Proximity is not evidence. Only containment is.

        The first tile states its quarter; the second states none. Nothing may
        carry the first tile's period across to the second.
        """
        elements = (tile("Profit After Tax", "US$ 114 Mn.", cx=330, top=200,
                         period="(Q1-FY27)")
                    + tile("Core Operating Profit", "US$ 250 Mn.",
                           cx=1020, top=200)
                    + tile("Advances", "US$ 31,447 Mn.", cx=1710, top=200))
        _, out = read(elements)
        ppop = next(f for f in out.facts
                    if f.canonical_metric == "pre_provision_operating_profit")
        assert ppop.period is None, "a sibling tile's quarter is not evidence"
        pat = next(f for f in out.facts if f.canonical_metric == "pat")
        assert pat.period == "Q1 FY27"


# ------------------------------------------------------------ strategy routing
class TestEachFigureIsExplainedOnce:
    """Ground a table has already read is not read again as tiles.

    On a degraded statement scan the tile reader assembled "Revenue = 2" and
    "EBITDA = 4" out of stray fragments, and those stood beside the real
    figures as alternatives to them -- costing the benchmark a match. A figure
    a table has explained must not be explained a second time from its
    position on the page.
    """

    def test_text_a_table_explained_is_skipped(self):
        from app.core.geometry import BBox
        elements = grid([[("Profit After Tax", "US$ 114 Mn."),
                          ("Total Deposits", "US$ 32,931 Mn."),
                          ("Advances", "US$ 31,447 Mn.")]])
        covering = BBox(x=0, y=0, width=PAGE_WIDTH, height=2000)
        finding = detect_cards(elements, page_number=1,
                               page_width=PAGE_WIDTH, occupied=[covering])
        assert finding.cards == []
        assert finding.excluded_by_tables == len(elements)

    def test_a_table_that_explained_nothing_does_not_reserve_the_page(self):
        """A detector's low-quality grid over a KPI band explains nothing.

        The pipeline passes only the boxes of tables that actually produced
        facts, so a spurious table laid over a tile band leaves those tiles
        available. Passing no boxes is that case.
        """
        elements = grid([[("Profit After Tax", "US$ 114 Mn."),
                          ("Total Deposits", "US$ 32,931 Mn."),
                          ("Advances", "US$ 31,447 Mn.")]])
        finding = detect_cards(elements, page_number=1,
                               page_width=PAGE_WIDTH, occupied=[])
        assert len(finding.cards) == 3

    def test_only_the_covered_region_is_reserved(self):
        """Four tiles, one column already read: the other three still are."""
        from app.core.geometry import BBox
        elements = grid([[("Profit After Tax", "US$ 114 Mn."),
                          ("Total Deposits", "US$ 32,931 Mn."),
                          ("Advances", "US$ 31,447 Mn."),
                          ("CASA Deposits", "US$ 16,734 Mn.")]])
        # A table covering the first column only.
        covering = BBox(x=0, y=0, width=600, height=2000)
        finding = detect_cards(elements, page_number=1,
                               page_width=PAGE_WIDTH, occupied=[covering])
        labels = {c.label_text for c in finding.cards}
        assert "Profit After Tax" not in labels
        assert {"Total Deposits", "Advances", "CASA Deposits"} <= labels


# ------------------------------------------------------------- orientation
class TestLayoutOrientation:
    """What the detector refuses, so a table is never read as tiles."""

    def test_a_dense_single_column_of_prose_yields_no_tiles(self):
        elements = [el(f"This is an ordinary sentence number {i} on the page.",
                       130, 200 + i * (LINE + 8), w=1400) for i in range(12)]
        finding = find(elements)
        assert finding.cards == []

    def test_a_conventional_table_is_not_read_as_tiles(self):
        """Rows of numbers under one header are a table's shape, not a tile's.

        Each column holds a stack of bare figures with no label of its own, so
        every run is refused for having nothing that names it.
        """
        elements: list[OCRElement] = []
        for row in range(6):
            elements.append(el(f"Metric {row}", 150, 200 + row * 60, w=300))
            for col in range(3):
                elements.append(el(f"{1000 + row * 37 + col}",
                                   900 + col * 400, 200 + row * 60, w=180))
        finding = find(elements)
        assert not any(c.label_text.startswith("Metric") and
                       c.value_text.isdigit() and len(finding.cards) > 6
                       for c in finding.cards)
