"""A recognition repair corrects misread letters; it does not change the words.

Collapsing the letters a recogniser confuses (a/o, l/i, rn/m) and the spaces it
loses lets a damaged label meet its alias. It also let undamaged labels meet
aliases they do not name, each found by processing a real filing:

  * "Credit Cards" -- a bank's product line -- reduced to "creditcords" and
    scored 0.90 against "creditors": card figures reported as payables;
  * "As a % of Revenue" became cost of revenue (two results releases);
  * a bank's "Asset Management" segment became assets under management,
    eighteen dated segment figures under the wrong measure;
  * "Operating expenses to NTI" -- a ratio -- became operating expenses;
  * "Operating EBIDTA margin" became the EBIT margin.

Every case below is a label as OCR read it from a page in the corpus.
"""
from __future__ import annotations

import pytest

from app.extraction.taxonomy import _repair_accounts_for, match_metric


class TestARepairThatNamesSomethingElse:
    @pytest.mark.parametrize("label", [
        "Credit Cards",
        "Credit Card",
        "As a % of Revenue",
        "as a % of Revenue",
        "AssetManagement",
        "Operating expenses to NTI",
        "Operating expenses to NTl",
        "OperatingEBIDTAmargin (%)",
        "Outstanding",
        "Totaloutstanding' (in crore)",
        "Ratio %",
    ])
    def test_is_refused(self, label):
        assert match_metric(label) is None

    @pytest.mark.parametrize("label,alias", [
        ("Credit Cards", "creditors"),                        # a word replaced
        ("AssetManagement", "assets under management"),       # a word missing
        ("Outstanding", "outstanding debt"),                  # a word missing
        ("OperatingEBIDTAmargin (%)", "operating margin"),    # a word extra
        ("Operating expenses to NTI", "operating expense"),   # a ratio
    ])
    def test_the_reason_is_the_words(self, label, alias):
        assert not _repair_accounts_for(label, alias)


class TestARepairOfMisreadLetters:
    @pytest.mark.parametrize("label,key", [
        # Kotak's scanned statement
        ("(a)Interest/dlscountanadvances/bils", "interest_on_advances"),
        ("(viilTotalDebtstoTotatAssets (%) (Refer Note 7.a)", "debt_to_assets"),
        ("NetProfit(12-13) 14", "pat"),
        # Tata Steel: an alternative name before a slash is not an extra word.
        # Line 1(a) of a SEBI results statement is SALES, a component of
        # revenue from operations (Phase 22, FAILURE_KB M6) -- the damaged
        # spelling must still reach that line, not the total.
        ("Sales/incomefromoperations a）", "sales"),
        ("VIH Tax expense:", "tax_expense"),
        ("Profit before tax 3.843", "pbt"),
    ])
    def test_still_reaches_its_measure(self, label, key):
        m = match_metric(label)
        assert m is not None and m.key == key

    def test_a_lost_space_and_a_misread_letter_together(self):
        # "Totat" for "Total", no spaces, a damaged enumerator before it
        assert _repair_accounts_for(
            "(viilTotalDebtstoTotatAssets (%)", "total debts to total assets")

    def test_a_plural_is_the_same_word(self):
        assert _repair_accounts_for("VIH Tax expense:", "tax expenses")

    def test_a_percent_sign_says_ratio(self):
        # Standard Chartered's capital table: "Total capital" is an amount,
        # "Total capital (%)" is the total capital ratio.
        m = match_metric("Total capital (%)")
        assert m is not None and m.key == "capital_adequacy_ratio"
        assert match_metric("Total capital") is None
