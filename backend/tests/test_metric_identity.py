"""Which measure a figure is filed under -- the dimension the scorer never saw.

The corpus scorer pairs each golden row with a fact by printed label and cell,
then checks value, period, entity, unit and currency. The golden files record
the label as printed, not a taxonomy key, so the canonical metric FinSight
files a figure under was never checked. Reading all 377 label-to-metric
pairings among found golden facts by eye found these, each with a figure read
correctly and filed under the wrong measure:

  * ICICI Bank, consolidated: "Other income (e)+(f)" with the next line run
    in -- "... Premium and other operating income from insurance" -- filed as
    insurance premium income;
  * Bharti Airtel: "Profit for the quarter / year attributable to :" -- the
    total profit, with the heading of the split below it run in -- filed as
    the owners' share;
  * SAP: "Attributable to owners of parent" and "Attributable to
    non-controlling interests" under "Profit (loss) after tax", filed as
    balance-sheet equity and as other comprehensive income;
  * Wipro: "Share-based payment reserve", an equity balance, filed as the
    period's share-based payment expense;
  * SBI's analyst deck: "Total Provisions" on a P&L slide filed as the
    balance-sheet line.
"""
from __future__ import annotations

import pytest

from app.core.geometry import BBox
from app.extraction.facts import TableFactContext, _compose, extract_from_table
from app.extraction.taxonomy import match_metric
from app.models.enums import Currency, EntityType, MagnitudeUnit
from app.models.schemas import BBoxModel, OCRElement
from app.normalization.periods import FiscalCalendar
from app.ocr.lines import TextLine
from app.tables.structure import build_table


def _key(label, stock=None):
    m = match_metric(label, stock=stock)
    return m.key if m else None


class TestTwoLinesRunIntoOneLabel:
    """Nothing in the text says which of two run-together lines the figures
    belong to: on ICICI's page it was the first, on Eternal's image statement
    the last. Taking either would be a guess, so neither is taken."""

    @pytest.mark.parametrize("label", [
        "Other income (e)+(f) Premium and other operating income from insurance",
        "IX Profit for the period / year (VIl-VU) Other comprehensive income / "
        "(loss) (a) Items that will not be reclassified to profit or loss: "
        "(iii) Income tax relating to above items",
        "Income tax relating to above Items that will be reclassified to profit "
        "or loss",
    ])
    def test_is_not_guessed(self, label):
        assert _key(label) is None

    def test_one_line_ending_in_its_formula_still_reads(self):
        assert _key("Other income (e)+(f)") == "other_income"

    def test_a_line_opening_mid_phrase_names_nothing_alone(self):
        """ICICI, page 1: "a) Basic EPS before and after extraordinary items,
        net" over "of tax expense (not annualised) (in ₹)", the figures on the
        second line. Read alone it was the tax charge."""
        def line(i, y, items):
            els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=1,
                              text=t, confidence=0.95,
                              bbox=BBoxModel(x=x, y=y, width=w, height=14.0))
                   for j, (t, x, w) in enumerate(items)]
            box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                                      height=e.bbox.height) for e in els)
            return TextLine(id=f"l{i}", page_number=1, bbox=box, elements=els)

        centres = [700, 900]
        cal = FiscalCalendar(year_end_month=3)
        lines = [line(0, 40, [("Q1-2027", 660.0, 80.0), ("Q1-2026", 860.0, 80.0)]),
                 line(1, 65, [("Tax expense (g)+(h)", 20.0, 300.0),
                              ("4,321.12", 660.0, 80.0), ("4,079.48", 860.0, 80.0)]),
                 line(2, 90, [("a) Basic EPS before and after extraordinary items, net",
                               20.0, 420.0)]),
                 line(3, 108, [("of tax expense (not annualised) (in R)", 20.0, 380.0),
                               ("20.65", 660.0, 80.0), ("17.91", 860.0, 80.0)])]
        table = build_table(document_id="d", page_number=1, region_id="r",
                            lines=lines, v_lines=[], page_width=1100,
                            page_height=900, table_index=0, calendar=cal,
                            ruled=False, base_confidence=0.7)
        assert table is not None
        ctx = TableFactContext(
            document_id="d", entity="Standalone", entity_id="ent_sa",
            entity_type=EntityType.STANDALONE, calendar=cal,
            default_currency=Currency.INR, default_unit=MagnitudeUnit.CRORE)
        by_value = {f.value: f.canonical_metric for f in extract_from_table(table, ctx)}
        assert by_value.get(20.65) != "tax_expense"
        assert by_value.get(20.65) in (None, "eps")

    def test_a_bracketed_qualifier_after_the_formula_still_is(self):
        assert _key("Operating Profit (3-6) (Profit before Provisions and "
                    "Contingencies)") == "pre_provision_operating_profit"

    def test_an_attribution_with_no_one_after_it_qualifies_nothing(self):
        assert _key("Profit for the quarter / year attributable to :",
                    stock=False) == "pat"


class TestAReserveIsNotTheCharge:
    def test_the_reserve(self):
        assert _key("Share-based payment reserve") != "share_based_payment_expense"

    def test_the_charge(self):
        assert _key("Share-based payment expense") == "share_based_payment_expense"

    @pytest.mark.parametrize("label,key", [
        ("Cash and balances with Reserve Bank of India", "cash_and_rbi_balances"),
        ("(c) Interest on balances with Reserve Bank of India and other "
         "inter-bank funds", "interest_on_rbi_balances"),
        ("Reserves and surplus", "other_equity"),
    ])
    def test_the_reserve_bank_is_a_name(self, label, key):
        assert _key(label) == key


class TestTotalProvisionsDependsOnTheColumns:
    def test_on_a_slide_of_periods_it_is_the_charge(self):
        assert _key("Total Provisions", stock=False) == "provisions_and_contingencies"

    def test_on_a_balance_sheet_it_is_the_balance(self):
        assert _key("Total Provisions", stock=True) == "provisions"

    def test_where_the_columns_do_not_say_it_is_not_guessed(self):
        assert _key("Total Provisions") is None


class TestAShareOfTheRowAbove:
    @pytest.mark.parametrize("label", ["Attributable to owners of parent",
                                       "Attributable to non-controlling interests"])
    def test_alone_it_names_no_measure(self, label):
        assert _key(label, stock=False) is None

    @pytest.mark.parametrize("label,key", [
        ("Attributable to owners of parent", "pat_attributable_to_owners"),
        ("Attributable to non-controlling interests", "nci_share_of_profit"),
    ])
    def test_read_with_the_row_above_it_does(self, label, key):
        m = _compose("Profit (loss) after tax", label, False)
        assert m is not None and m.key == key

    def test_in_a_table_each_share_is_read_with_the_profit(self):
        """SAP's quarter, page 8: profit after tax 2,210, owners 2,180,
        minority 30. The second share must be read with the profit, not with
        the first share."""
        def line(i, y, items):
            els = [OCRElement(id=f"e{i}_{j}", document_id="d", page_number=8,
                              text=t, confidence=0.95,
                              bbox=BBoxModel(x=x, y=y, width=w, height=14.0))
                   for j, (t, x, w) in enumerate(items)]
            box = BBox.union_all(BBox(x=e.bbox.x, y=e.bbox.y, width=e.bbox.width,
                                      height=e.bbox.height) for e in els)
            return TextLine(id=f"l{i}", page_number=8, bbox=box, elements=els)

        centres = [700, 900]
        rows = [("Income tax expense", ["(778)", "(640)"]),
                ("Profit (loss) after tax", ["2,210", "1,749"]),
                ("Attributable to owners of parent", ["2,180", "1,697"]),
                ("Attributable to non-controlling interests", ["30", "52"])]
        lines = [line(0, 40, [("Q2 2026", 660.0, 80.0), ("Q2 2025", 860.0, 80.0)])]
        for i, (label, values) in enumerate(rows, start=1):
            lines.append(line(i, 40 + 25 * i, [(label, 20.0, 380.0)]
                              + [(v, c - 40.0, 80.0) for v, c in zip(values, centres)]))
        cal = FiscalCalendar(year_end_month=12)
        table = build_table(document_id="d", page_number=8, region_id="r",
                            lines=lines, v_lines=[], page_width=1100,
                            page_height=900, table_index=0, calendar=cal,
                            ruled=False, base_confidence=0.7)
        assert table is not None
        ctx = TableFactContext(
            document_id="d", entity="Consolidated", entity_id="ent_cons",
            entity_type=EntityType.CONSOLIDATED, calendar=cal,
            default_currency=Currency.EUR, default_unit=MagnitudeUnit.MILLION)
        by_value = {f.value: f.canonical_metric for f in extract_from_table(table, ctx)}
        assert by_value.get(2180.0) == "pat_attributable_to_owners"
        assert by_value.get(30.0) == "nci_share_of_profit"
