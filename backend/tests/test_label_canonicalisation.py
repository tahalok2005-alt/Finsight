"""Row labels carry annotation; the taxonomy must see through it -- and only it.

Found in the Phase 21 corpus (fifteen companies): most rows the taxonomy did
not recognise were standard statement lines whose labels carried a serial
number, a note reference, a subtotal formula or a face-value clause, or had
lost their spaces to OCR. Every label below is quoted as the corpus printed it
or as the recogniser read it.

The other half of the rule is held here too: a label that adds a substantive
word to a known measure names a DIFFERENT measure, and must not be matched to
the shorter one by similarity.
"""
from __future__ import annotations

import pytest

from app.extraction.taxonomy import match_metric, normalize_label


@pytest.mark.parametrize("label,key", [
    # serial numbers, enumerators and bullets
    ("(c) Other income (Refer Note 4)", "other_income"),
    ("(b)Premium on Insurance Business", "insurance_premium_income"),
    ("e) Premium and other operating income from insurance business",
     "insurance_premium_income"),
    ("16Minority Interest", None),              # bare: needs its statement's kind
    ("XV Paid-up share capital (face value of INR 1 per share)", "share_capital"),
    ("- Trade payables", "payables"),
    ("---of which provisions for non-performing assets", "provisions_for_npa"),
    ("Less: Share of profit/(loss) of minority shareholders", "nci_share_of_profit"),
    # subtotal formulas
    ("TOTALINCOME(1)+(2)", "total_income"),
    ("Interest earned (a)+(b)+(c)+(d)", "interest_earned"),
    ("Profitfrom ordinary activities beforetax (7-8-9)", "pbt"),
    ("PROFIT/(LOSS)FROMORDINARYACTIVITIESBEFORETAX(7)-(8)+(9)", "pbt"),
    ("NETPROFIT/(LOSS)FROMORDINARYACTIVITIESAFTERTAX(10)(11)",
     "pat_ordinary_activities"),
    ("Net Profit / (Loss) for the period [7 - 8]", "pat"),
    ("Total revenue from operations [ 1(a) + 1(b)]", "revenue_from_operations"),
    ("Operating Profit (3-6) (Profit before Provisions and Contingencies)",
     "pre_provision_operating_profit"),
    # note references, face value, annualisation, currency brackets
    ("Provisions (other than tax) and contingencies (Refer Note 7)",
     "provisions_and_contingencies"),
    ("Provisions (otherthantax)and contingencies (netofwriteback) 8",
     "provisions_and_contingencies"),
    ("Paid Up Equity Capital (Face yalue of 1 pershare) (Refer Nate9)",
     "share_capital"),
    ("Paid-up equity share capital [Face value ? 1 per share]", "share_capital"),
    ("Paid-up equity share capital (Face Value of each share-Rs.10/-)",
     "share_capital"),
    ("Earning per share - Basic () for the period", "eps"),
    ("(a) Basic and diluted EPS before Extraordinary items (net of tax expense)",
     "eps"),
    ("Exceptional items [net (charge) / credit]", "exceptional_items"),
    # lost spaces
    ("Interestreceived", "interest_received"),
    ("Cash generated from/(used in)operations", "cash_generated_from_operations"),
    ("Income taxes refund /(paid)(net)", "income_taxes_paid"),
    ("Purchaseofpropertyplantandequipmentandotherintangibleassets"
     "(includingcapital work-in-progress)", "capex"),
    # standard lines the taxonomy did not have before Phase 21
    ("Interest expended", "interest_expended"),
    ("Gross profit", "gross_profit"),
    ("Results from operating activities", "ebit"),
    ("Total Capital and Liabilities", "total_equity_and_liabilities"),
    ("Balances with banks and money at call and short notice", "balances_with_banks"),
    ("Cash and cash equivalents at the end of the period", "cash_at_end"),
    ("Cash and cash equivalents as at the beginning of the period / year",
     "cash_at_beginning"),
    ("Items that will not be reclassified to profit or loss",
     "oci_items_not_reclassified"),
    ("Items that will be reclassified to profit or loss", "oci_items_reclassified"),
    ("Deferred tax assets", "deferred_tax_assets"),
    ("Network operating expenses", "network_operating_expenses"),
    ("Common Equity Tier (CET) 1 Ratio", "cet1_ratio"),
])
def test_corpus_labels(label, key):
    m = match_metric(label)
    assert (m.key if m else None) == key, (label, m)


class TestAQualifiedLabelIsADifferentMeasure:
    @pytest.mark.parametrize("label,not_key", [
        ("Network operating expenses", "operating_expenses"),
        ("Deferred tax assets", "deferred_tax"),
        ("Current tax assets", "current_tax"),
        ("Finance costs on lease liabilities", "finance_costs"),
        ("Results from operating activities", "operating_cash_flow"),
        ("Profit before exceptional items and tax", "pbt"),
        ("Common Equity Tier 1", "tier_1_ratio"),
        # each side names something the other does not
        ("customer assets", "subscribers"),
        ("Interest expanded", "interest_earned"),
    ])
    def test_never_the_shorter_measure(self, label, not_key):
        m = match_metric(label)
        assert m is None or m.key != not_key, (label, m)

    def test_a_scope_in_brackets_is_kept(self):
        # A bank's domestic margin and its whole-bank margin are two figures.
        assert match_metric("NIM (Domestic) (%)") is None
        assert match_metric("NIM (Whole Bank)") is None
        # A unit in brackets is still dropped.
        assert match_metric("ARPU (Rs/Sub/month)").key == "arpu"


class TestTheKindOfStatementDecidesABareName:
    """ "Non-controlling interests" is an equity line in a balance sheet and the
    minority's share of profit in a statement of profit; "Capital" in a bank's
    balance sheet is its share capital. The words alone do not say which, the
    column heads do -- "As at" or "... ended" -- and without them there is no
    answer."""

    def test_in_a_balance_sheet(self):
        assert match_metric("Non-controlling interests", stock=True).key == \
            "non_controlling_interests"
        assert match_metric("Minority interest", stock=True).key == \
            "non_controlling_interests"
        assert match_metric("Capital", stock=True).key == "share_capital"

    def test_in_a_statement_of_profit(self):
        assert match_metric("Non-controlling interests", stock=False).key == \
            "nci_share_of_profit"
        assert match_metric("Minority Interest", stock=False).key == \
            "nci_share_of_profit"
        assert match_metric("Capital", stock=False) is None

    def test_unknown_kind_decides_nothing(self):
        for label in ("Non-controlling interests", "Minority interest",
                      "Capital", "Investments", "Provisions", "Other assets"):
            assert match_metric(label) is None, label


class TestNormalisationRemovesAnnotationOnly:
    @pytest.mark.parametrize("raw,norm", [
        ("(d) Others", "others"),
        ("Others (d)", "others"),
        ("(d)others", "others"),
        ("A Items that will not be reclassified to profit or loss (i)",
         "items that will not be reclassified to profit or loss"),
        ("Net Profit from Ordinary Activities aftertax 12",
         "net profit from ordinary activities aftertax"),
        ("Common Equity Tier 1", "common equity tier 1"),
        ("Tier 2", "tier 2"),
        ("Profit / (Loss) for the period", "profit/loss"),
        ("D&A", "d and a"),
    ])
    def test_normalise(self, raw, norm):
        assert normalize_label(raw) == norm

    def test_enumerated_siblings_that_differ_stay_apart(self):
        a = normalize_label("(i) Items that will not be reclassified to profit or loss")
        b = normalize_label("(ii) Income tax relating to items that will not be "
                            "reclassified to profit or loss")
        assert a != b
