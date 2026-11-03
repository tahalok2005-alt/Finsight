"""Which headings name a reporting unit, and which merely mention one.

Every case below is a real heading from a real filing, and the wrong answers
were all found the same way: by processing a document the system had not seen
and looking at what the figures were attributed to.

The rule these defend is structural rather than lexical. A results deck
mentions retail, digital, services and business on nearly every page; what
separates a heading that *is* a reporting unit is either a legal suffix
somewhere in it, or a head noun saying so.
"""
from __future__ import annotations

import pytest

from app.document.assembly import _entity_from_heading

RIL = "Reliance Industries Limited"
HDFC = "HDFC Bank Limited"


class TestRealSegmentHeadings:
    """Headings that do name a unit. These worked and must keep working."""

    @pytest.mark.parametrize("heading,expected", [
        ('CONSOLIDATED JIO PLATFORMS LIMITED ("JPL")', "JPL"),
        ('CONSOLIDATED RELIANCE RETAIL VENTURES LIMITED ("RRVL")', "RRVL"),
        ('OILTO CHEMICALS("O2C")SEGMENT', "O2C"),
        ("JIOSTAR BUSINESS", "JIOSTAR"),
    ])
    def test_a_named_unit_is_recognised(self, heading, expected):
        assert _entity_from_heading(heading, company_name=RIL) == expected

    def test_a_segment_heading_without_an_abbreviation_still_names_it(self):
        name = _entity_from_heading(
            "OIL AND GAS (EXPLORATION AND PRODUCTION) SEGMENT",
            company_name=RIL)
        assert name and "OIL AND GAS" in name


class TestReportingBasis:
    """A basis declaration governs what follows it, however it is worded."""

    def test_a_bare_declaration_is_the_group(self):
        assert _entity_from_heading(
            "CONSOLIDATED RESULTS FOR QUARTER ENDED 30TH JUNE 2026",
            company_name=RIL) == "Consolidated"

    @pytest.mark.parametrize("heading,expected", [
        ("Standalone Segment information in accordance with the RBI "
         "guidelines and Accounting Standard 17 - Segment Reporting",
         "Standalone"),
        ("Consolidated Segment information for the quarter ended 30 June 2026",
         "Consolidated"),
    ])
    def test_a_declaration_survives_the_description_after_it(self, heading,
                                                             expected):
        """A bank's filing carries both bases and says so in a long heading.

        Reading only short headings put standalone and consolidated results
        under one entity, and the reconciler then reported twelve
        contradictions between figures that were never meant to agree.
        """
        assert _entity_from_heading(heading, company_name=HDFC) == expected

    def test_a_subsidiary_s_own_consolidation_is_not_the_group(self):
        """"Consolidated Jio Platforms Limited" is JPL, not the filer."""
        assert _entity_from_heading(
            'CONSOLIDATED JIO PLATFORMS LIMITED ("JPL")',
            company_name=RIL) == "JPL"


class TestHeadingsThatNameNothing:
    """The wrong answers, each found on a document the system had not seen."""

    def test_a_fragment_of_a_legal_name_is_refused(self):
        """The line grouper cut one filer's own name in half.

        "Billionbrains Garage Ventures Limited (Formerly known as Billionbrains
        Garage Ventures Private Limited)" became the tail alone -- carrying a
        closing bracket it never opened -- and that fragment was attributed 58
        of the document's 116 figures as a subsidiary.
        """
        assert _entity_from_heading("Garage Ventures Private Limited)",
                                    company_name="Groww") is None

    @pytest.mark.parametrize("heading", [
        "Quality Financial Services",
        "DISTRIBUTION OF FINANCIAL SERVICES",
        "Paytm for Business App",
        "Consumer Payments",
    ])
    def test_a_slide_heading_is_not_a_reporting_unit(self, heading):
        """An earnings release mentions services and business everywhere.

        One of these governed 23 of that release's 25 figures.
        """
        assert _entity_from_heading(heading, company_name="Paytm") is None

    @pytest.mark.parametrize("heading", [
        "RETAIL BANKING",
        "Retail, Agri and MSME (RAM) Book",
    ])
    def test_a_diagram_label_is_not_a_reporting_unit(self, heading):
        """Both are labels inside illustrations, not sections of the deck.

        "RETAIL BANKING" is one spoke of a wheel diagram; the bank never
        reports it as a unit, and it owned two dozen figures.
        """
        assert _entity_from_heading(heading,
                                    company_name="IDFC FIRST Bank") is None

    def test_a_bare_noun_is_not_a_unit(self):
        assert _entity_from_heading("Risk", company_name=RIL) is None

    @pytest.mark.parametrize("heading", [
        "Risk-weighted assets by business",
        "Non-performing assets by business segment",
    ])
    def test_a_hyphenated_word_is_not_cut_into_a_name(self, heading):
        """A UK bank's "Risk-weighted assets by business" named a segment "Risk".

        The hyphen was read as the dash that ends a name, and the word
        "business" further along let the fragment through; sixteen of the
        group's own figures were attributed to it.
        """
        assert _entity_from_heading(
            heading, company_name="Standard Chartered PLC") is None

    @pytest.mark.parametrize("heading,expected", [
        ("NON-BANKING FINANCE BUSINESS", "NON-BANKING FINANCE"),
        ("E-COMMERCE SEGMENT", "E-COMMERCE"),
    ])
    def test_a_hyphenated_unit_keeps_its_whole_name(self, heading, expected):
        # Cut at the hyphen these were "NON" and "E": refused as too short,
        # so the unit's figures went to the group.
        assert _entity_from_heading(heading, company_name=RIL) == expected

    def test_a_spaced_dash_still_ends_the_name(self):
        assert _entity_from_heading("Consolidated - Financial summary",
                                    company_name=RIL) == "Consolidated"
