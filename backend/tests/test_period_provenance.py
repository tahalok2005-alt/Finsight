"""The period a figure belongs to, and how honestly it is reported.

A financial figure without its period is a number, not information. These tests
pin the two things that make the period trustworthy: that the dates the source
supports survive every boundary unchanged, and that a period nobody established
stays empty rather than being filled in from the shape of a label.

The recurring example is Apple's 28 June 2025. Apple runs a 52/53-week
calendar, so its third quarter does not end on the last day of June -- which
makes it the ideal witness for the rule that a date must never be recomputed
from a fiscal label.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import repository as repo
from app.database.models import FinancialFactRow
from app.database.session import session_scope
from app.models.enums import PeriodBasis, PeriodType
from app.models.schemas import NormalizedPeriod
from app.normalization.periods import (
    FiscalCalendar, parse_period, parse_period_levels,
)

SEPT = FiscalCalendar(year_end_month=9)      # a September year end, as Apple has
MARCH = FiscalCalendar(year_end_month=3)     # the Indian convention
DECEMBER = FiscalCalendar(year_end_month=12)  # the UK and European convention


@pytest.fixture
def registered(synthetic_pdf):
    """A document row to hang test facts on. No processing required."""
    from app.core.ids import sha256_file
    doc_id = "perioddoc01"
    with session_scope() as s:
        if repo.get_document(s, doc_id) is None:
            repo.create_document(
                s, document_id=doc_id, filename=synthetic_pdf.name,
                original_filename=synthetic_pdf.name,
                stored_path=str(synthetic_pdf),
                content_hash=sha256_file(synthetic_pdf),
                size_bytes=synthetic_pdf.stat().st_size)
    return doc_id


# --------------------------------------------------------------- resolution
class TestPeriodResolution:
    """What the resolver establishes, and what it declines to."""

    def test_a_source_header_gives_the_exact_day_not_the_month_end(self):
        p = parse_period_levels(["Three Months Ended", "June 28,", "2025"],
                                calendar=SEPT)
        assert p.label == "Q3 FY25"
        assert p.end_date == "2025-06-28"        # the day the document printed
        assert p.period_type == PeriodType.QUARTER

    def test_the_label_alone_cannot_reproduce_that_day(self):
        """The reason the resolved period must be carried rather than rebuilt.

        This is not a defect being pinned -- it is arithmetic. "Q3 FY25" under a
        September year end *is* the June quarter, and without the header there
        is nothing to say it closed on the 28th rather than the 30th. The lesson
        is that the answer has to travel, not be recomputed.
        """
        from_header = parse_period_levels(["Three Months Ended", "June 28,",
                                           "2025"], calendar=SEPT)
        from_label = parse_period(from_header.label, calendar=SEPT)
        assert from_header.label == from_label.label == "Q3 FY25"
        assert from_header.end_date == "2025-06-28"
        assert from_label.end_date == "2025-06-30"
        assert from_header.end_date != from_label.end_date

    @pytest.mark.parametrize("levels,expected_type,expected_end", [
        (["Three Months Ended", "June 28,", "2025"], PeriodType.QUARTER, "2025-06-28"),
        (["Six Months Ended", "June 30,", "2026"], PeriodType.HALF_YEAR, "2026-06-30"),
        (["Nine Months Ended", "June 28,", "2025"], PeriodType.NINE_MONTH, "2025-06-28"),
        (["Year Ended", "March 31,", "2026"], PeriodType.FISCAL_YEAR, "2026-03-31"),
        (["June 28,", "2025"], PeriodType.POINT_IN_TIME, "2025-06-28"),
    ])
    def test_each_span_length_stays_distinguishable(self, levels, expected_type,
                                                    expected_end):
        """A half-yearly report contains all of these at once.

        Three-month, six-month, nine-month, annual and balance-sheet columns
        sit side by side in one filing, and collapsing them would make every
        figure in it ambiguous.
        """
        p = parse_period_levels(levels, calendar=MARCH)
        assert p.period_type == expected_type
        assert p.end_date == expected_end

    def test_a_balance_sheet_instant_has_no_span(self):
        p = parse_period_levels(["September 28,", "2024"], calendar=SEPT)
        assert p.period_type == PeriodType.POINT_IN_TIME
        assert p.end_date == "2024-09-28"
        assert p.start_date is None, "a moment in time has no start"

    def test_nothing_resolvable_yields_nothing(self):
        assert parse_period_levels(["Particulars"], calendar=MARCH) is None
        assert parse_period("Gross Revenue", calendar=MARCH) is None


# -------------------------------------------------------------------- basis
class TestPeriodBasis:
    """How the period was arrived at, recorded where it is decided."""

    def test_text_the_document_wrote_is_stated(self):
        for text in ("Q1 FY27", "Three Months Ended June 28, 2025",
                     "As at 31st Mar'26"):
            p = parse_period(text, calendar=MARCH)
            assert p.basis == PeriodBasis.STATED, text

    def test_a_period_assembled_across_header_rows_is_derived(self):
        """No single line said "Q3 FY25"; the span and the date were separate."""
        p = parse_period_levels(["Three Months Ended", "June 28,", "2025"],
                                calendar=SEPT)
        assert p.basis == PeriodBasis.DERIVED

    def test_a_level_that_states_its_own_period_keeps_stated(self):
        """A banner above it does not downgrade what the column itself says."""
        p = parse_period_levels(
            ["RESULTS FOR THE QUARTER ENDED 30TH JUNE, 2026", "1Q FY27"],
            calendar=MARCH)
        assert p.label == "Q1 FY27"
        assert p.basis == PeriodBasis.STATED

    def test_basis_is_never_invented_for_an_unresolved_period(self):
        p = NormalizedPeriod(label="x", raw_text="x")
        assert p.basis == PeriodBasis.UNKNOWN


# -------------------------------------------------------------- persistence
class TestPeriodPersistence:
    """The resolved period must survive the database unchanged."""

    def _fact_with_period(self, document_id: str, period: NormalizedPeriod):
        from app.models.enums import FactKind, StatementNature, ValueType
        from app.models.schemas import FinancialFact
        return FinancialFact(
            fact_id=f"pf_{period.label.replace(' ', '_')}",
            document_id=document_id, metric="Profit After Tax",
            canonical_metric="pat", metric_display="Profit After Tax",
            kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
            value=3671.0, raw_value="3,671", value_type=ValueType.MONETARY,
            period=period.label, period_normalized=period, source_page=7)

    def test_a_resolved_period_round_trips_through_the_database(self, registered):
        source = NormalizedPeriod(
            label="Q3 FY25", raw_text="Three Months Ended / June 28, / 2025",
            period_type=PeriodType.QUARTER, fiscal_year=2025, quarter=3,
            start_date="2025-04-01", end_date="2025-06-28",
            confidence=0.92, basis=PeriodBasis.DERIVED)

        with session_scope() as s:
            repo.save_facts(s, [self._fact_with_period(registered, source)])

        with session_scope() as s:
            row = s.scalars(select(FinancialFactRow).where(
                FinancialFactRow.document_id == registered)).first()
            assert row is not None
            assert row.period == "Q3 FY25"
            assert row.period_start == "2025-04-01"
            assert row.period_end == "2025-06-28"
            assert row.period_confidence == pytest.approx(0.92)
            assert row.period_basis == "derived"

            back = repo.row_to_fact(row, None)

        np_ = back.period_normalized
        assert np_.end_date == "2025-06-28", "the source date did not survive"
        assert np_.end_date != "2025-06-30", "the date was recomputed from the label"
        assert np_.start_date == "2025-04-01"
        assert np_.confidence == pytest.approx(0.92)
        assert str(np_.basis) == "derived"

    def test_a_period_without_dates_stays_without_dates(self, registered):
        """NULL is the honest answer, and must not be replaced by arithmetic."""
        vague = NormalizedPeriod(
            label="FY26", raw_text="FY26", period_type=PeriodType.FISCAL_YEAR,
            fiscal_year=2026, start_date=None, end_date=None,
            confidence=0.8, basis=PeriodBasis.STATED)

        with session_scope() as s:
            repo.save_facts(s, [self._fact_with_period(registered, vague)])
        with session_scope() as s:
            row = s.scalars(select(FinancialFactRow).where(
                FinancialFactRow.document_id == registered)).first()
            back = repo.row_to_fact(row, None)

        assert row.period_end is None
        assert back.period_normalized.end_date is None, \
            "a date was manufactured for a period that never had one"
        assert back.period_normalized.label == "FY26"   # the label still stands

    def test_a_legacy_row_keeps_its_label_and_gains_no_dates(self, registered):
        """Rows written before these columns existed must not be back-filled.

        The migration adds columns; it does not invent history. A label without
        dates is exactly what such a row should read back as.
        """
        with session_scope() as s:
            s.add(FinancialFactRow(
                id="legacy_fact_1", document_id=registered, metric="PAT",
                canonical_metric="pat", metric_display="PAT",
                kind="table_cell", nature="actual", value=100.0,
                raw_value="100", value_type="monetary", currency="INR",
                unit="crore", period="Q1 FY27", period_type="quarter",
                fiscal_year=2027, quarter=1, source_page=1))
        with session_scope() as s:
            row = s.get(FinancialFactRow, "legacy_fact_1")
            back = repo.row_to_fact(row, None)

        assert row.period_end is None and row.period_basis is None
        assert back.period_normalized.label == "Q1 FY27"
        assert back.period_normalized.end_date is None
        assert str(back.period_normalized.basis) == "unknown"


# ---------------------------------------------------- the column carries it
class TestColumnCarriesTheResolvedPeriod:
    """Facts read the resolved period, not a re-parse of its label."""

    def test_a_fact_takes_the_column_s_exact_dates(self):
        from app.extraction.facts import _period_for
        from app.models.schemas import TableColumn

        resolved = parse_period_levels(["Three Months Ended", "June 28,",
                                        "2025"], calendar=SEPT)
        column = TableColumn(index=1, header="Three Months Ended June 28, 2025",
                             normalized_period=resolved.label,
                             period_resolved=resolved)

        got = _period_for(column, SEPT)
        assert got.end_date == "2025-06-28"
        assert got.end_date != "2025-06-30", \
            "the fact re-derived the date from the label"
        assert got.basis == PeriodBasis.DERIVED

    def test_a_column_with_only_a_label_still_resolves(self):
        """Backward compatibility: the repair pass works in labels alone."""
        from app.extraction.facts import _period_for
        from app.models.schemas import TableColumn

        column = TableColumn(index=1, header="1Q FY27",
                             normalized_period="Q1 FY27")
        got = _period_for(column, MARCH)
        assert got is not None and got.label == "Q1 FY27"


# ------------------------------------------------- a half-yearly filing
class TestHalfYearlyReport:
    """The document shape that made this work necessary.

    A half-yearly report is not "a report about H1". It sets the half-year
    beside the quarter inside it, beside the prior year's comparatives, beside
    balance-sheet columns dated to the half-year end. Assigning every figure in
    it to H1 would be wrong for most of them, and assigning none of them a
    period -- which is what happened before -- makes the whole document
    unreadable.
    """

    def _column(self, levels, calendar=MARCH):
        from app.models.schemas import TableColumn
        p = parse_period_levels(levels, calendar=calendar)
        return TableColumn(index=1, header=" ".join(levels),
                           normalized_period=p.label if p else None,
                           period_resolved=p), p

    def test_the_four_spans_of_a_half_yearly_filing_stay_distinct(self):
        """Quarter, half, nine-month and year, all dated to their own day."""
        from app.extraction.facts import _period_for

        shapes = [
            (["Three Months Ended", "September 30,", "2025"],
             PeriodType.QUARTER, "2025-09-30"),
            (["Six Months Ended", "September 30,", "2025"],
             PeriodType.HALF_YEAR, "2025-09-30"),
            (["Nine Months Ended", "December 31,", "2025"],
             PeriodType.NINE_MONTH, "2025-12-31"),
            (["Year Ended", "March 31,", "2025"],
             PeriodType.FISCAL_YEAR, "2025-03-31"),
        ]
        seen = []
        for levels, ptype, end in shapes:
            column, _ = self._column(levels)
            got = _period_for(column, MARCH)
            assert got is not None, levels
            assert got.period_type == ptype, levels
            assert got.end_date == end, levels
            seen.append((got.period_type, got.end_date))

        assert len(set(seen)) == 4, "two spans collapsed into one period"

    def test_a_quarter_and_a_half_year_ending_the_same_day_stay_apart(self):
        """Both close on 30 September; only the span tells them apart."""
        q, _ = self._column(["Three Months Ended", "September 30,", "2025"])
        h, _ = self._column(["Six Months Ended", "September 30,", "2025"])
        assert q.period_resolved.end_date == h.period_resolved.end_date
        assert q.period_resolved.period_type != h.period_resolved.period_type
        assert q.period_resolved.start_date != h.period_resolved.start_date
        assert q.normalized_period != h.normalized_period

    def test_a_balance_sheet_column_is_not_given_the_report_s_span(self):
        """The report is half-yearly; this column is still a moment in time."""
        column, p = self._column(["September 30,", "2025"])
        assert p.period_type == PeriodType.POINT_IN_TIME
        assert p.start_date is None
        assert "As at" in p.label

    def test_no_quarter_is_invented_for_an_undated_column(self):
        from app.extraction.facts import _period_for
        from app.models.schemas import TableColumn
        bare = TableColumn(index=1, header="Particulars")
        assert _period_for(bare, MARCH) is None

    # -- the shapes a real UK half-yearly report actually prints -----------

    def test_a_numeric_dotted_date_is_read(self):
        """"6 months ended / 30.06.26" is how UK and European filings head a
        column. Before this was handled, a 165-page half-yearly report came
        back with 308 of its 335 figures carrying no period at all.
        """
        column, p = self._column(["6 months ended", "30.06.26"], DECEMBER)
        assert p.period_type == PeriodType.HALF_YEAR
        assert p.start_date == "2026-01-01"
        assert p.end_date == "2026-06-30"

    def test_the_second_half_is_not_called_the_first(self):
        """The comparative column of a half-yearly report is often H2.

        "6 months ended 31.12.25" is the *second* half of 2025 for a December
        filer. Labelling every six-month column H1 would put the comparative
        in the wrong half of the wrong year.
        """
        _, first = self._column(["6 months ended", "30.06.25"], DECEMBER)
        _, second = self._column(["6 months ended", "31.12.25"], DECEMBER)
        assert first.label == "H1 FY25"
        assert second.label == "H2 FY25"
        assert first.start_date == "2025-01-01"
        assert second.start_date == "2025-07-01"
        assert second.end_date == "2025-12-31"

    def test_a_bare_numeric_date_heads_a_balance_sheet_column(self):
        _, p = self._column(["31.12.25"], DECEMBER)
        assert p.period_type == PeriodType.POINT_IN_TIME
        assert p.end_date == "2025-12-31"
        assert p.start_date is None

    def test_an_ambiguous_numeric_date_is_refused(self):
        """"06.07.26" is 6 July or 7 June depending on the convention.

        Nothing in the string settles it, so nothing is returned. A month-end
        date — which is what a reporting period actually closes on — always
        settles itself, so this costs nothing real.
        """
        assert self._column(["6 months ended", "06.07.26"], DECEMBER)[1] is None
        assert self._column(["06.07.26"], DECEMBER)[1] is None

    def test_a_digit_span_says_the_same_as_a_spelt_one(self):
        _, digits = self._column(["6 months ended", "30.06.26"], DECEMBER)
        _, words = self._column(["Six Months Ended", "June 30,", "2026"],
                                DECEMBER)
        assert digits.period_type == words.period_type
        assert digits.end_date == words.end_date == "2026-06-30"

    def test_a_bare_half_year_label_is_read(self):
        """Half-yearly reporters write "H1 2026" in their own narrative."""
        p = parse_period("H1 2026", calendar=DECEMBER)
        assert p is not None
        assert p.period_type == PeriodType.HALF_YEAR
        assert p.start_date == "2026-01-01"
        assert p.end_date == "2026-06-30"


# ------------------------------------------------------- across the API
class TestPeriodReachesTheApi:
    """Every field a reader needs must survive the HTTP boundary too.

    A field that exists in the model and is dropped by the serializer is a
    field the product does not have, so this asserts against the JSON the
    browser actually receives.
    """

    def _seed(self, document_id: str):
        from app.models.enums import FactKind, StatementNature, ValueType
        from app.models.schemas import FinancialFact
        source = NormalizedPeriod(
            label="Q3 FY25", raw_text="Three Months Ended / June 28, / 2025",
            period_type=PeriodType.QUARTER, fiscal_year=2025, quarter=3,
            start_date="2025-04-01", end_date="2025-06-28",
            confidence=0.92, basis=PeriodBasis.DERIVED)
        undated = NormalizedPeriod(
            label="FY26", raw_text="FY26", period_type=PeriodType.FISCAL_YEAR,
            fiscal_year=2026, confidence=0.8, basis=PeriodBasis.STATED)

        def fact(fid, period, value):
            return FinancialFact(
                fact_id=fid, document_id=document_id, metric="Profit After Tax",
                canonical_metric="pat", metric_display="Profit After Tax",
                kind=FactKind.TABLE_CELL, nature=StatementNature.ACTUAL,
                value=value, raw_value=str(value),
                value_type=ValueType.MONETARY,
                period=period.label if period else None,
                period_normalized=period, source_page=7)

        with session_scope() as s:
            repo.save_facts(s, [fact("api_dated", source, 3671.0),
                                fact("api_undated", undated, 4784.0),
                                fact("api_noperiod", None, 100.0)])

    def test_the_api_serves_the_source_date_not_a_recomputed_one(
            self, client, registered):
        self._seed(registered)
        r = client.get(f"/api/v1/documents/{registered}/financials?limit=50")
        assert r.status_code == 200
        items = {f["fact_id"]: f for f in r.json()["items"]}

        dated = items["api_dated"]
        assert dated["period"] == "Q3 FY25"
        assert dated["period_end"] == "2025-06-28"
        assert dated["period_end"] != "2025-06-30",             "the API recomputed the date from the fiscal label"
        assert dated["period_start"] == "2025-04-01"
        assert dated["period_type"] == "quarter"
        assert dated["period_basis"] == "derived"
        assert dated["period_confidence"] == pytest.approx(0.92)

    def test_the_api_leaves_an_unestablished_date_null(self, client, registered):
        self._seed(registered)
        items = {f["fact_id"]: f for f in client.get(
            f"/api/v1/documents/{registered}/financials?limit=50").json()["items"]}

        undated = items["api_undated"]
        assert undated["period"] == "FY26"          # the label still stands
        assert undated["period_end"] is None        # but no date was invented
        assert undated["period_start"] is None
        assert undated["period_basis"] == "stated"

    def test_a_figure_with_no_period_says_nothing_about_one(
            self, client, registered):
        self._seed(registered)
        items = {f["fact_id"]: f for f in client.get(
            f"/api/v1/documents/{registered}/financials?limit=50").json()["items"]}

        none = items["api_noperiod"]
        assert none["period"] is None
        assert none["period_end"] is None
        assert none["period_type"] is None

    def test_the_review_queue_carries_the_period_too(self, client, registered):
        """A reviewer judging a figure needs to know which period it is."""
        self._seed(registered)
        with session_scope() as s:
            from app.database.models import FinancialFactRow
            row = s.get(FinancialFactRow, "api_dated")
            row.requires_review = True
            row.review_state = "pending"

        r = client.get(f"/api/v1/documents/{registered}/reviews")
        assert r.status_code == 200
        items = r.json().get("items", [])
        item = next((i for i in items if i["fact_id"] == "api_dated"), None)
        assert item is not None, "the seeded figure never reached the queue"
        assert item["period_end"] == "2025-06-28"
        assert item["period_type"] == "quarter"
        assert item["period_basis"] == "derived"
