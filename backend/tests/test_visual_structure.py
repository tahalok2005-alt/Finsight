"""Reading financial figures from a page's visual structure.

Where ``test_spatial_extraction.py`` covers the vertical tile, this covers the
rest of what a page can be arranged as: metrics set out one per line with the
figure beside the label, a date heading a panel, and the several things that
have exactly those shapes without being financial facts at all.

Every refusal tested here is a defect that actually occurred. The page numbers
in the docstrings are pages of ``IDFC_Q12027.pdf``, the 75-page investor
presentation this work was built against, and the geometry is taken from them.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.layout.furniture import find_templates
from app.models.enums import (
    Currency, MagnitudeUnit, PeriodBasis, RegionType, ValueType,
)
from app.models.schemas import BBoxModel, OCRElement, Region
from app.normalization.periods import FiscalCalendar
from app.spatial.cards import detect_cards
from app.spatial.extract import CardContext, extract_from_cards, extract_from_rows
from app.spatial.geometry import lattice_of
from app.spatial.rows import detect_rows

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


def panel(pairs: list[tuple[str, str]], *, label_x: float = 1477,
          value_x: float = 2182, top: float = 655, step: float = 176,
          ) -> list[OCRElement]:
    """A block of metric rows: label at the left edge, figure at the right.

    The coordinates are page 62's shareholding panel, measured.
    """
    out: list[OCRElement] = []
    for i, (label, value) in enumerate(pairs):
        y = top + i * step
        out.append(el(label, label_x, y))
        out.append(el(value, value_x, y))
    return out


def find_rows(elements: list[OCRElement], **kw):
    return detect_rows(elements, page_number=1, page_width=PAGE_WIDTH, **kw)


def read_rows(elements: list[OCRElement], *, line_height: float = LINE, **ctx_kw):
    finding = find_rows(elements)
    ctx = CardContext(document_id="d1", calendar=MARCH, **ctx_kw)
    return finding, extract_from_rows(finding, ctx, line_height=line_height)


# ------------------------------------------------- horizontal metric blocks
class TestHorizontalMetricRows:
    """A label with its figure beside it, repeated down a panel."""

    def test_a_block_of_rows_is_read(self):
        finding, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]))
        assert len(finding.rows) == 3
        by = {f.canonical_metric: f.value for f in out.facts}
        assert by["shares_outstanding"] == 861.47
        assert by["book_value_per_share"] == 56.47
        assert by["eps"] == 5.01

    def test_the_figure_beside_the_label_is_the_one_taken(self):
        """Reading order alone puts the *next row's* label after a figure."""
        _, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]))
        eps = next(f for f in out.facts if f.canonical_metric == "eps")
        assert eps.raw_value == "Rs. 5.01"
        assert eps.currency == Currency.INR

    def test_a_lone_pair_is_refused(self):
        """One label beside one number is the commonest accident there is."""
        finding = find_rows(panel([("Book Value per Share", "Rs. 56.47")])
                            + [el("Shareholding Pattern", 200, 100)])
        assert finding.blocks == []

    def test_two_rows_are_not_yet_a_block(self):
        page = panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
        ]) + [el("Shareholding Pattern", 200, 100),
              el("Scrip Name: IDFC FIRST Bank", 200, 200),
              el("Public", 300, 900), el("President of India", 300, 1000)]
        finding = find_rows(page)
        assert finding.blocks == []
        assert finding.rejected.get("block_not_repeated_enough_to_trust")

    def test_rows_must_share_both_edges(self):
        """A shared left margin is prose. A shared figure column is a panel."""
        ragged = (panel([("Total No. of shares", "861.47 Cr")])
                  + panel([("Book Value per Share", "Rs. 56.47")],
                          value_x=1900, top=831)
                  + panel([("Basic EPS", "Rs. 5.01")],
                          value_x=2500, top=1007))
        assert find_rows(ragged).blocks == []

    def test_a_word_gap_is_not_a_gutter(self):
        """"Credit Cost 1.13%" set as one phrase is not a label and a figure."""
        tight = []
        for i, (label, value) in enumerate([("Total Deposits", "US$ 32,931 Mn."),
                                            ("CASA Deposits", "US$ 16,734 Mn."),
                                            ("Advances", "US$ 31,447 Mn.")]):
            y = 400 + i * 176
            e = el(label, 300, y)
            tight.append(e)
            tight.append(el(value, e.bbox.x + e.bbox.width + 6, y))
        finding = find_rows(tight)
        assert finding.blocks == []
        assert finding.rejected.get("no_gutter_between_the_label_and_the_figure")


class TestHorizontalRefusals:
    """The shapes that look like metric rows and are not."""

    def test_a_credit_rating_grid_produces_nothing(self):
        """Page 64: five perfect repetitions, no financial value anywhere.

        The strongest horizontal geometry in the whole document. If layout
        alone were the evidence, this is what it would buy.
        """
        finding, out = read_rows(panel([
            ("CRISIL", "AAA"), ("ICRA", "AA+ (stable)"),
            ("India Ratings", "AA+ (stable)"), ("CARE Ratings", "AA+ (stable)"),
        ]))
        assert out.facts == []

    def test_a_table_row_without_column_heads_is_refused(self):
        """Page 7: five dated columns, of which the first is another company.

        Taking the nearest figure reports Capital First's 2018 loan book as the
        bank's own. There is no column head available here to prevent that, so
        the whole baseline is refused.
        """
        rows: list[OCRElement] = []
        for i, (label, figures) in enumerate([
                ("Loan Book", ["2,850", "5,809", "8,954", "31,447"]),
                ("Net Worth", ["309", "1,560", "1,940", "5,137"]),
                ("Customer Deposit", ["0", "3,840", "4,060", "31,613"])]):
            y = 400 + i * 90
            rows.append(el(label, 200, y))
            for j, figure in enumerate(figures):
                rows.append(el(figure, 700 + j * 350, y))
        finding = find_rows(rows)
        assert finding.blocks == []
        assert finding.rejected["several_figures_on_the_baseline_no_column_heads"]

    def test_a_sentence_with_a_number_is_not_a_metric_row(self):
        """Page 27: "strong deposit franchise" beside a chart's data label."""
        prose = panel([
            ("strong deposit franchise", "32,243"),
            ("driven by the merger", "11,657"),
            ("across the deposit ratio", "97.9%"),
        ])
        _, out = read_rows(prose)
        assert out.facts == []
        assert out.rejected["label_not_a_known_metric"] == 3

    def test_garbled_rotated_axis_labels_are_refused(self):
        """Pages 21, 35, 36: axis text recognised sideways."""
        _, out = read_rows(panel([
            ("%SO'T", "%96'0"), ("T-en", "81-qe"), ("9z-AeW", "91-qe"),
        ]))
        assert out.facts == []


# ------------------------------------------------------- period-block rules
class TestPeriodBlocks:
    """A date heading a panel governs the panel. Nothing else does."""

    def _heading_and_panel(self, heading: str):
        return [el(heading, 1500, 420, w=700)] + panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ])

    def test_a_heading_above_the_block_is_inherited(self):
        _, out = read_rows(self._heading_and_panel("Shareholding (June 30, 2026)"))
        eps = next(f for f in out.facts if f.canonical_metric == "eps")
        assert eps.period_normalized is not None
        assert eps.period_normalized.basis == PeriodBasis.INHERITED
        assert "June 30, 2026" in eps.meta["period_inherited_from"]

    def test_a_heading_beside_the_block_is_not_inherited(self):
        """Page 62's own heading sits over the pie chart, not the panel.

        Proximity on the page is not containment of the block, and the
        difference is the whole rule.
        """
        beside = [el("Shareholding (June 30, 2026)", 200, 420, w=700)] + panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ])
        _, out = read_rows(beside)
        assert out.facts
        assert all(f.period is None for f in out.facts)

    def test_a_period_in_the_row_s_own_label_is_stated(self):
        _, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS (Annualized Q1 FY27)", "Rs. 5.01"),
        ]))
        eps = next(f for f in out.facts if f.canonical_metric == "eps")
        assert eps.period == "Q1 FY27"
        assert eps.period_normalized.basis == PeriodBasis.STATED

    def test_the_row_s_own_period_beats_the_block_heading(self):
        elements = self._heading_and_panel("Shareholding (June 30, 2026)")
        elements = [e for e in elements if e.text != "Basic EPS"]
        elements += [el("Basic EPS (Annualized Q1 FY27)", 1477, 1007)]
        _, out = read_rows(elements)
        eps = next(f for f in out.facts if f.canonical_metric == "eps")
        assert eps.period == "Q1 FY27"
        assert eps.period_normalized.basis == PeriodBasis.STATED

    def test_a_corner_date_stamp_never_reaches_a_block(self):
        """"Jun-26" in the top corner governs the block below it in no way.

        It gave a share count a period taken from a page decoration. A block
        occupies a corner of a page that may hold three others, so only a
        heading that spans *this* block may lend it one.
        """
        page = [el("Jun-26", 2554, 131, w=86)] + panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Total Deposits", "US$ 32,931 Mn."),
        ])
        _, out = read_rows(page)
        shares = next(f for f in out.facts
                      if f.canonical_metric == "shares_outstanding")
        assert shares.period is None

    def test_no_heading_and_no_stated_period_leaves_it_unknown(self):
        _, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]))
        for fact in out.facts:
            assert fact.period is None
            assert fact.meta["period_absence"] == str(PeriodBasis.UNKNOWN)


# ------------------------------------------------------ units and value types
class TestUnitsAndValueTypes:

    def test_a_multiple_is_never_an_amount(self):
        """"2.5X" beside a balance says it grew, not what it is."""
        _, out = read_rows(panel([
            ("Total Deposits", "2.5X"), ("CASA Deposits", "5.8X"),
            ("Advances", "7.0X"),
        ]))
        assert out.facts == []
        assert out.rejected["value_is_a_multiple_not_an_amount"] == 3

    def test_a_percentage_is_refused_for_a_monetary_metric(self):
        _, out = read_rows(panel([
            ("Total Deposits", "18%"), ("CASA Deposits", "25%"),
            ("Advances", "20%"),
        ]))
        assert out.facts == []
        assert out.rejected["percentage_value_for_a_monetary_metric"] == 3

    def test_a_monetary_row_with_no_scale_is_refused(self):
        _, out = read_rows(panel([
            ("Total Deposits", "32931"), ("CASA Deposits", "16734"),
            ("Advances", "31447"),
        ]))
        assert out.facts == []
        assert out.rejected["no_scale_or_currency_on_a_monetary_figure"] == 3

    def test_mixed_units_in_one_block_are_each_read_as_printed(self):
        _, out = read_rows(panel([
            ("Total Deposits", "US$ 32,931 Mn."),
            ("Net Interest Margin", "5.96%"),
            ("Total No. of shares", "861.47 Cr"),
        ]))
        by = {f.canonical_metric: f for f in out.facts}
        assert by["total_deposits"].unit == MagnitudeUnit.MILLION
        assert by["total_deposits"].currency == Currency.USD
        assert by["net_interest_margin"].value_type == ValueType.PERCENTAGE
        assert by["shares_outstanding"].unit == MagnitudeUnit.CRORE


# ----------------------------------------------------------------- evidence
class TestEvidenceAndProvenance:

    def _read(self):
        return read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]))

    def test_every_row_fact_carries_its_provenance(self):
        _, out = self._read()
        assert out.facts
        for fact in out.facts:
            assert fact.provenance is not None
            assert fact.provenance.page_number == 1
            assert fact.provenance.extraction_method == "spatial_row"
            assert len(fact.provenance.ocr_element_ids) >= 2
            assert fact.provenance.original_text == fact.raw_value

    def test_the_evidence_names_what_was_measured(self):
        finding, _ = self._read()
        row = finding.rows[0]
        assert row.evidence["rows_in_block"] == 3
        assert row.evidence["gutter_px"] > 0
        assert "label_edge_px" in row.evidence
        assert "value_edge_px" in row.evidence

    def test_a_row_is_trusted_less_than_a_tile(self):
        """The same figure, read two ways, must not claim equal certainty.

        A tile is enclosed by whitespace on four sides; a row is separated
        along one axis, which is also the shape of a table row and a legend.
        """
        _, rows_out = read_rows(panel([
            ("Total Deposits", "US$ 32,931 Mn."),
            ("CASA Deposits", "US$ 16,734 Mn."),
            ("Advances", "US$ 31,447 Mn."),
        ]))
        tiles: list[OCRElement] = []
        for i, (label, value) in enumerate([
                ("Total Deposits", "US$ 32,931 Mn."),
                ("CASA Deposits", "US$ 16,734 Mn."),
                ("Advances", "US$ 31,447 Mn.")]):
            cx = 330 + i * 690
            tiles.append(el(label, cx - len(label) * 8, 200))
            tiles.append(el(value, cx - len(value) * 8, 250))
        for i, (label, value) in enumerate([
                ("Profit After Tax", "US$ 114 Mn."),
                ("Net Worth", "US$ 5,137 Mn."),
                ("Loan Book", "US$ 32,243 Mn.")]):
            cx = 330 + i * 690
            tiles.append(el(label, cx - len(label) * 8, 460))
            tiles.append(el(value, cx - len(value) * 8, 510))
        finding = detect_cards(tiles, page_number=1, page_width=2560)
        tiles_out = extract_from_cards(
            finding, CardContext(document_id="d1", calendar=MARCH))

        row_deposits = next(f for f in rows_out.facts
                            if f.canonical_metric == "total_deposits")
        tile_deposits = next(f for f in tiles_out.facts
                             if f.canonical_metric == "total_deposits")
        assert row_deposits.confidence < tile_deposits.confidence


# ------------------------------------------------------------------ routing
class TestRouting:
    """Two strategies must never report one piece of evidence twice."""

    def test_ground_a_tile_claimed_is_not_read_again_as_a_row(self):
        elements = panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ])
        claimed = [BBox.union_all(
            BBox.from_dict(e.bbox.model_dump()) for e in elements)]
        finding = find_rows(elements, occupied=claimed)
        assert finding.blocks == []
        assert finding.rows_claimed_by_other_strategies == 3

    def test_ground_no_strategy_claimed_is_still_offered(self):
        elements = panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ])
        elsewhere = [BBox(0, 0, 100, 100)]
        finding = find_rows(elements, occupied=elsewhere)
        assert len(finding.rows) == 3
        assert finding.rows_claimed_by_other_strategies == 0

    def test_routing_is_deterministic(self):
        elements = panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ])
        first = [f.fact_id for f in read_rows(elements)[1].facts]
        second = [f.fact_id for f in read_rows(elements)[1].facts]
        assert first == second and first


# --------------------------------------------------- the grid-completeness rule
class TestTilesMustFormAGrid:
    """Page 74's defect: four "tiles" assembled from a six-panel chart page.

    A chart title, an axis tick and a bar's data label became a KPI tile whose
    figure was a different year's. Counting tiles did not catch it; measuring
    whether they fill a grid does.
    """

    def test_a_filled_grid_is_accepted(self):
        grid = lattice_of([BBox(x, y, 300, 200)
                           for y in (200, 500, 800) for x in (100, 800, 1500)],
                          tolerance=120)
        assert grid.rows == 3 and grid.columns == 3
        assert grid.is_a_grid

    def test_a_scattered_lattice_is_refused(self):
        """Four boxes over a 3x2 lattice: the page-74 arrangement."""
        grid = lattice_of([BBox(2042, 296, 392, 109), BBox(312, 730, 415, 241),
                           BBox(1200, 300, 300, 150), BBox(300, 1100, 300, 150)],
                          tolerance=120)
        assert not grid.is_a_grid
        assert grid.filled < 0.9

    def test_a_single_column_of_tiles_is_not_a_grid(self):
        grid = lattice_of([BBox(100, y, 300, 200) for y in (200, 500, 800)],
                          tolerance=120)
        assert grid.columns == 1
        assert not grid.is_a_grid

    def test_a_single_row_of_tiles_is_a_grid(self):
        """A band of four highlight tiles across the top of a slide."""
        grid = lattice_of([BBox(x, 200, 300, 200)
                           for x in (100, 800, 1500, 2200)], tolerance=120)
        assert grid.rows == 1 and grid.columns == 4
        assert grid.is_a_grid

    def test_chart_fragments_do_not_become_tiles(self):
        """The page-74 geometry, replayed through the detector itself."""
        elements = [
            el("Mar-16", 130, 730, w=108, h=33), el("Mar-17", 340, 730, w=108, h=33),
            el("Mar-18", 557, 730, w=108, h=33), el("Sep-18", 770, 730, w=108, h=33),
            el("CASA Deposits (US$ Mn.)", 312, 849, w=415, h=43),
            el("47", 150, 934, w=40, h=37), el("221", 350, 934, w=55, h=37),
            el("603", 583, 934, w=64, h=37), el("678", 790, 934, w=64, h=37),
            el("Net Interest Margin (%)", 2042, 296, w=392, h=47),
            el("2.0%", 1925, 370, w=80, h=35), el("2.1%", 2114, 370, w=80, h=35),
        ]
        finding = detect_cards(elements, page_number=74, page_width=PAGE_WIDTH)
        assert finding.cards == []


# ------------------------------------------------------------ page furniture
class TestPageFurniture:
    """A logo is not a chart, however often it is detected as one."""

    def _pages(self, n: int, *, elements_in_block: int = 1):
        out = []
        for page in range(1, n + 1):
            regions = [Region(
                id=f"r{page}", document_id="d1", page_number=page,
                type=RegionType.CHART,
                bbox=BBoxModel(x=1813, y=0, width=854, height=62),
                confidence=0.5, reading_order=0, detector="t")]
            elements = [el(f"IDFC {i}", 1820 + i * 10, 10)
                        for i in range(elements_in_block)]
            for e in elements:
                e.page_number = page
            out.append((page, PAGE_WIDTH, PAGE_HEIGHT, regions, elements))
        return out

    def test_a_block_repeating_across_pages_with_no_data_is_furniture(self):
        found = find_templates(self._pages(6), types=(RegionType.CHART,))
        assert len(found) == 6
        assert "appears at this position on 6 pages" in \
            next(iter(found.values())).describe()

    def test_two_pages_are_not_yet_a_template(self):
        assert find_templates(self._pages(2), types=(RegionType.CHART,)) == {}

    def test_a_chart_template_repeated_with_data_is_not_furniture(self):
        """Pages 35-37 run one chart layout three times with different data."""
        pages = self._pages(3, elements_in_block=38)
        assert find_templates(pages, types=(RegionType.CHART,)) == {}

    def test_the_one_page_where_content_overlaps_the_band_is_kept(self):
        pages = self._pages(6)
        page, w, h, regions, _ = pages[3]
        loaded = [el(f"{i}00", 1820 + i * 40, 10) for i in range(9)]
        for e in loaded:
            e.page_number = page
        pages[3] = (page, w, h, regions, loaded)
        found = find_templates(pages, types=(RegionType.CHART,))
        assert len(found) == 5
        assert regions[0].id not in found


# --------------------------------------------------------- page-level headings
class TestPageHeadings:
    """Which text on a page may lend its period to the tiles below it."""

    def _grid(self):
        tiles: list[OCRElement] = []
        for i, (label, value) in enumerate([
                ("Total Deposits", "US$ 32,931 Mn."),
                ("CASA Deposits", "US$ 16,734 Mn."),
                ("Advances", "US$ 31,447 Mn.")]):
            cx = 400 + i * 700
            tiles.append(el(label, cx, 300, w=380))
            tiles.append(el(value, cx, 360, w=380))
        for i, (label, value) in enumerate([
                ("Profit After Tax", "US$ 114 Mn."),
                ("Net Worth", "US$ 5,137 Mn."),
                ("Loan Book", "US$ 32,243 Mn.")]):
            cx = 400 + i * 700
            tiles.append(el(label, cx, 700, w=380))
            tiles.append(el(value, cx, 760, w=380))
        return tiles

    def test_the_slide_title_heads_the_tiles(self):
        page = self._grid() + [el("Bank at Glance, Q1 FY27 results", 110, 99, w=795)]
        finding = detect_cards(page, page_number=1, page_width=PAGE_WIDTH)
        assert finding.cards
        assert [h.text for h in finding.headings] == [
            "Bank at Glance, Q1 FY27 results"]

    def test_a_corner_date_stamp_is_not_a_heading(self):
        """"Jun-26" printed in the top corner spans nothing and governs nothing."""
        page = self._grid() + [el("Jun-26", 2554, 60, w=86)]
        finding = detect_cards(page, page_number=1, page_width=PAGE_WIDTH)
        assert finding.cards
        assert finding.headings == []

    def test_a_footnote_is_not_a_heading(self):
        """Page 74's defect: a note about one figure lending its quarter to all."""
        page = self._grid() + [el(
            "*The bank took one-time provisions relating to stressed "
            "infrastructure loans. Without such charge off, the PAT for "
            "H1 FY19 would be US$ 9 Mn.", 110, 1300, w=2400)]
        finding = detect_cards(page, page_number=1, page_width=PAGE_WIDTH)
        assert finding.cards
        assert finding.headings == []

    def test_text_below_the_tiles_is_not_a_heading(self):
        page = self._grid() + [el("Q1 FY27 results", 110, 1200, w=795)]
        finding = detect_cards(page, page_number=1, page_width=PAGE_WIDTH)
        assert finding.cards
        assert finding.headings == []


# ------------------------------------------------------------- per-share scale
class TestPerShareNeverTakesADocumentScale:
    """A deck reporting in millions still prints EPS in rupees and cents."""

    def test_a_per_share_figure_refuses_the_document_magnitude(self):
        _, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]), default_unit=MagnitudeUnit.MILLION, default_currency=Currency.USD)
        by = {f.canonical_metric: f for f in out.facts}
        assert by["book_value_per_share"].value == 56.47
        assert by["book_value_per_share"].unit == MagnitudeUnit.NONE
        assert by["eps"].unit == MagnitudeUnit.NONE

    def test_a_count_carries_no_currency(self):
        """"861.47 Cr" against a share count is shares, not rupees.

        Crore and lakh imply rupees for money -- OCR cannot emit the rupee
        glyph, so the word is the only evidence there is. For a count the same
        inference renders eight and a half billion shares as a rupee amount.
        """
        _, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]))
        shares = next(f for f in out.facts
                      if f.canonical_metric == "shares_outstanding")
        assert shares.value == 861.47
        assert shares.unit == MagnitudeUnit.CRORE
        assert shares.currency == Currency.UNKNOWN

    def test_it_still_keeps_the_currency_printed_beside_it(self):
        _, out = read_rows(panel([
            ("Total No. of shares", "861.47 Cr"),
            ("Book Value per Share", "Rs. 56.47"),
            ("Basic EPS", "Rs. 5.01"),
        ]), default_unit=MagnitudeUnit.MILLION, default_currency=Currency.USD)
        eps = next(f for f in out.facts if f.canonical_metric == "eps")
        assert eps.currency == Currency.INR


# ------------------------------------------- a word is not a unit declaration
class TestWordFragmentsAreNotUnits:
    """"quarte-rs", "custome-rs", "numbe-rs": the letters, not "Rs.".

    The same missing word boundary appeared twice in the codebase and did
    different damage each time. In ``infer_document_units`` it set a
    dollar-reporting deck's currency to rupees. In the narrative extractor it
    licensed a bare number as a financial figure, and the phrase "GNPA trend
    analysis (5 quarters)" was recorded as a gross NPA ratio of 5% -- at medium
    confidence, with nothing flagged for review.

    Both are guarded here together, because the defect is the pattern rather
    than either site.
    """

    @pytest.mark.parametrize("prose", [
        "Product-wise GNPA trend analysis (5 quarters) Jun-26",
        "Numbers are gross of IBPC and consumer loan assignments",
        "Figures for prior quarters have been regrouped",
        "served through these channels to customers",
    ])
    def test_prose_does_not_license_a_bare_number(self, prose):
        from app.extraction.narrative import _SCALE_NEARBY
        assert not _SCALE_NEARBY.search(prose)

    @pytest.mark.parametrize("declared", [
        "revenue of Rs. 500 crore", "Rs 1,200", "EBITDA of 2,490 crore",
        "PAT of US$ 114 Mn", "profit of 45 lakh",
    ])
    def test_a_real_scale_still_licenses_one(self, declared):
        from app.extraction.narrative import _SCALE_NEARBY
        assert _SCALE_NEARBY.search(declared)

    def test_the_document_currency_is_not_decided_by_prose(self):
        from app.normalization.numbers import infer_document_units
        prose = ["39 million customers served through these channels",
                 "Numbers are gross of IBPC and consumer loan assignments"]
        assert infer_document_units(prose)[0] is None
        assert infer_document_units(prose + ["(US$ Mn.)"])[0] == Currency.USD
