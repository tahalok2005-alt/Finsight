# FinSight failure knowledge base

Every significant extraction failure found in real filings, recorded once:
the pattern, a source example, the root cause, the general fix, the
regression test that holds it, and the documents it affected. Institutional
memory: before fixing a "new" failure, look here — it is usually a known
pattern in a new layout.

Conventions: *Doc* keys are those of `data/corpus/manifest.json`. A fix is
general when it is stated in terms of layout, text or accounting structure and
names no company. Phase numbers say when the pattern was found.

---

## Values

### V1. Stops read as thousands separators
- **Example**: "1.053.69" for 1,053.69 (a bank's total income), parsed as 1.053.
- **Root cause**: the number pattern stopped at the second stop.
- **Fix**: two or more stops in one token cannot be a decimal; each group of
  three digits after the first is a thousands group (`repair_misread_separators`).
- **Test**: `tests/test_normalization.py`, `test_p22_fact_safety.py::TestEveryPrintedForm`.
- **Docs**: Phase 19 corpus (bank results).

### V2. An unseparated figure truncated
- **Example**: "14272.21" parsed as 142.
- **Root cause**: the grouped alternative of the number pattern allowed zero
  separators and matched the first three digits.
- **Fix**: the grouped form requires at least one separator.
- **Test**: `test_p22_fact_safety.py::TestEveryPrintedForm`.

### V3. Two columns' figures in one cell
- **Example**: "20,211 17,292" read as revenue 2,021,117,292; "228 88" as 22,888.
- **Root cause**: a missed column boundary put two figures in one cell.
- **Fix**: a cell is one figure only if it obeys one grouping convention
  (`holds_more_than_one_number`); otherwise refused, never concatenated.
- **Test**: `tests/test_merged_cells.py`.
- **Docs**: ETERNAL, ICICI (Phase 21).

### V4. Fullwidth brackets and signs
- **Example**: Airtel's loss from associates "(844)" read "（844)", reported +844, unflagged.
- **Root cause**: only fullwidth commas were mapped; "（" is not "(".
- **Fix**: fullwidth brackets, minus, percent and digits mapped one for one.
  NFKC is not used: it turns a superscript footnote marker into a digit.
- **Test**: `test_p22_fact_safety.py::TestFullwidthPunctuation`.
- **Docs**: AIRTEL (Phase 22).

### V5. European figures
- **Example**: "1.234,56" parsed as 1.234.
- **Root cause**: only Anglo/Indian conventions were recognised.
- **Fix**: a comma after grouped digits is determinate as a decimal comma
  (`european_figure`); the form is recorded in `number_format`.
- **Test**: `test_p22_fact_safety.py::TestEveryPrintedForm`.

### V6. Digit misreads on image statements
- **Example**: Eternal's image cash flow: "(711)" read "(71)", "(1,168)" read
  "(168)", "(161)" read "(167)" — a font whose thin "1" merges with its bracket.
- **Root cause**: one OCR pass over a whole page at 200 dpi.
- **Fix**: every table figure's cell is read a second time, alone, from a
  400 dpi clip (`app/ocr/second_read.py`); disagreement about digits or sign
  sends the figure to review as `value_disputed`. Never repaired. Measured: 1
  false dispute in 1,681 correct golden figures; every page-read misread caught.
- **Test**: `test_p22_fact_safety.py`, `test_fact_state.py::TestPhase22Dimensions`.
- **Docs**: ETERNAL (Phase 21–22).

### V7. A lost decimal point
- **Example**: SBI's "1,35,341.56" read as "13534156".
- **Root cause**: OCR dropped every separator.
- **Fix**: caught by the bank total-income identity (flagged) and by the second
  read (disputed).
- **Docs**: SBI.

### V8. A printed dash extracted as zero
- **Example**: an OCI tax line's "-" became a fact of 0.0 (flagged, but a dash
  is not a figure).
- **Fix**: a cell that prints only a dash produces no fact.
- **Test**: `test_p22_generalization.py::TestADashAndAPollutedHead`.

### V9. A column head that swallowed data rows
- **Example**: a release's balance table reconstructed with its first data
  rows in the column heads ("As on 30th June'26 13,72,936 ... 38%"); the "%"
  made every crore figure in those columns a percentage.
- **Fix**: what a head says about its column's kind is what it says before
  the first grouped figure in it (`_head`).
- **Test**: `test_p22_generalization.py::TestADashAndAPollutedHead`.
- **Docs**: AXIS_PR (the header reconstruction itself still swallows the
  rows; the figures are then missing, not mis-typed).

### V10. A space-grouped statement
- **Example**: a Swiss half-year report prints "43 109" and "(23 118)" -- a
  space as the thousands separator, no decimals in the thousands. Every such
  figure was refused as two figures in one cell (V3), and nothing was read.
- **Fix**: a table with no comma-grouped figure at all and at least three --
  and 30% or more -- of its figures space-grouped in threes has space grouping
  as its convention (`table_grouping`); runs that are two figures by any
  convention ("228 88", "3 551 4 579") are still refused. CHF added as a
  currency.
- **Test**: `test_p22_generalization.py::TestASpaceGroupedStatement`, `TestSwissFrancs`.
- **Docs**: NESTLE.

### V11. A column of note numbers read as figures
- **Example**: the "Notes" column of an IFRS statement: "5" beside "Other
  operating income" filed as a figure of 5.
- **Fix**: a column headed "Note(s)", "Note No.", "Schedule" or "Ref." whose
  cells are all references ("3", "12", "3.2", "5, 6") is not a data column.
- **Test**: `test_p22_generalization.py::TestANotesColumnIsNotFigures`.
- **Docs**: NESTLE.

### V12. Text with a number in it (section-40 audit)
- **Example**: a "PUNE-35" stamp beside a statement read as the figure 35; a
  slide's "Improving versus 2025" as 2025.
- **Fix**: a cell carrying a word other than a scale, currency, nil or rate
  mark is not a figure.
- **Docs**: BAJAJAUTO_2023, NESTLE_PPT.

### S2. S1 took calendar-quarter heads for data (a Phase 22 regression)
- **Example**: "€ millions, unless otherwise stated | Q2 2026 | Q2 2025 | △ in
  %": "Q2 2026" was not recognised as a period head, the row counted as data
  under S1's "plain figures beside a named line", and the statement lost its
  column heads -- the change column's "19" became basic EPS of 19 (unflagged)
  and 39 change figures became flagged amounts. Found by the section-40 audit,
  not by the golden scorer (the golden SAP facts were already incomplete).
- **Fix**: "Q2 2026", "Q1-Q2 2025" and month ranges are period heads; a
  grouped figure is never read inside "Q2 2026".
- **Test**: `test_p22_final.py::TestAYearHeadOverItsQuarter::test_calendar_quarter_heads_are_heads`.
- **Docs**: SAP.
- **And the fix's own regression (`p22e`)**: with the quarter statement's heads
  restored, the year-to-date statement on the next page -- its head split into
  "Q1-Q2" / "2026" words beside "millions, unless otherwise stated", still read
  as data -- was linked as the quarter's continuation and inherited its heads:
  41 half-year figures dated to the quarter, 2 unflagged. The golden gate
  failed and caught it. Now a stub that declares the unit makes its row head, a
  bare "Q1-Q2" is a head fragment, and two statements titled differently in
  their stubs are never linked as one.
  `test_p22_final.py::TestAStatementAfterAnotherIsNotItsContinuation`.

### S1. The head swallowing the first data rows
- **Example**: the same statement's first six lines ("Sales 43 109 44 228",
  "Cost of goods sold (23 118) ...") read as column-head lines -- the heads
  became "January-June 2026 43 109 191 (23 118) ..." and the lines were lost;
  a bank release's first rows likewise.
- **Root cause**: a row counted as data only if a figure in it had "." or ",".
- **Fix**: bracketed negatives and space-grouped figures are measurements;
  plain figures beside a named line (three or more letters in the label
  column) are data; a row of column serial numbers ("1 2 3 4 5") stays head.
- **Test**: `test_p22_generalization.py::TestANotesColumnIsNotFigures` (the
  fixture's first line is kept), `tests/test_multiline_header.py`.
- **Docs**: NESTLE, AXIS_PR.

## Metrics

### M14. A gross line matched to its net
- **Example**: "Other operating income" (a gross line above "Other operating
  expenses") scored 0.83 against "other operating income/(loss), net".
- **Fix**: a fuzzy match may not net a pair ("income/(loss), net") the label
  names one side of. ("Cash generated by operating activities" still reaches
  operating cash flow: there "net" is a convention, not a pair.)
- **Test**: `test_p22_generalization.py::TestAGrossLineIsNotItsNet`.

### M1. A correct number filed under the wrong measure — unscored
- **Example**: "Share-based payment reserve" (equity) filed as the period's
  share-based payment expense; the scorer called it correct.
- **Root cause**: the golden scorer paired by printed label and never checked
  the canonical metric.
- **Fix**: every golden row states its canonical key; the scorer reports
  WRONG_METRIC and gates on it (Phase 22).
- **Test**: `test_scorer_v2.py::TestTheMetricIsScored`.

### M2. Two label lines run together
- **Example**: "Other income (e)+(f) Premium and other operating income from
  insurance" filed as the premium; an image statement's "Profit for the period
  (VII-VIII) Other comprehensive income … tax relating to above items" filed as
  profit with the tax line's figures.
- **Root cause**: the row grouper merged label lines; the figures may belong
  to either.
- **Fix**: a label that continues past a subtotal formula, a unit or
  annualisation bracket, or "relating to above" names two lines and is not
  matched (`_FORMULA_THEN_TEXT`, `_UNIT_THEN_TEXT`, `_ABOVE_THEN_TEXT`).
  An attempt to take the first line was reverted: on the image statement the
  figures were the last line's.
- **Test**: `test_metric_identity.py::TestTwoLinesRunIntoOneLabel`,
  `test_p22_fact_safety.py::TestRunTogetherLines`.
- **Docs**: ICICI, ETERNAL, GROWW.

### M3. A line that begins mid-phrase
- **Example**: "of tax expense (not annualised) (in ₹)" — the second line of a
  basic-EPS label — filed as the tax charge with the EPS figures.
- **Fix**: a line opening with a connective (not "of which") names nothing alone.
- **Test**: `test_metric_identity.py`.
- **Docs**: ICICI.

### M4. A subject-less share of the row above
- **Example**: "Attributable to owners of parent" under "Profit (loss) after
  tax" filed as balance-sheet equity.
- **Fix**: read with the row above (exact match only); never fuzzy-matched to
  an alias that names a subject.
- **Test**: `test_metric_identity.py::TestAShareOfTheRowAbove`.
- **Docs**: SAP.

### M5. A movement filed as the balance
- **Example**: "- Trade receivables (126)" under "Movements in working
  capital:"; Apple's "Accounts receivable, net 5,685" under "Changes in
  operating assets and liabilities:".
- **Root cause**: a balance-sheet name read without its heading.
- **Fix**: under a heading (or in a label) that announces movements, a stock
  metric is refused; a flow total ends the block; a heading run into the
  row above still governs (`_MOVEMENT_HEADING`, `_MOVEMENT_TRAILER`).
- **Test**: `test_p22_fact_safety.py::TestMovementsAreNotBalances`.
- **Docs**: ETERNAL, APPLE.
- **Extended (Phase 22, new documents)**: a label-only heading ending "Net
  change in"; the row "... before working capital changes" opens a movement
  block even when the heading below it is as plain as "Adjustment for:"; and in
  a statement recognised as a cash-flow statement (two or more lines only a
  cash-flow statement prints) no balance-sheet measure is extracted except the
  cash at either end ("Bank balances other than cash and cash equivalents" in
  the investing section is a movement). `TestBalancesInsideACashFlowStatement`.
  Docs: HCLTECH_FY26, NTPC_FY26.

### M6. A component filed as its total
- **Example**: Tata Steel's "a) Sales / income from operations" (line 1(a)) as
  total revenue from operations; "(c) Other income" under "Other income (a+b+c)".
- **Fix**: SEBI line 1(a) is sales; components of same-named totals are marked
  and never used as the total in identities.
- **Test**: `test_p22_fact_safety.py::TestNamesThatMeanAComponent`,
  `test_accounting_applicability.py`.

### M7. A name that means different measures to different filers
- **Example**: "Leverage ratio" (bank: Tier 1 / exposure; NBFC: assets /
  equity) filed as net debt / EBITDA; a bank deck's "Operating Profit" (profit
  before provisions) filed as EBIT.
- **Fix**: "leverage ratio" not guessed; in a table printing two or more
  lender lines, the bare "Operating Profit" is pre-provision operating profit
  (`_bank_meanings`).
- **Test**: `test_smoke_findings.py`, `test_p22_fact_safety.py::TestALendersOperatingProfit`.
- **Docs**: BAJFIN_PPT, STANCHART, SBI_PPT.

### M9. A holder row read without its heading
- **Example**: "Non-controlling interest" two rows under "Other comprehensive
  income (loss) for the period / year attributable to" filed as the minority's
  share of PROFIT (its reading alone), on two filers' statements.
- **Root cause**: only the row directly under a heading was composed with it.
- **Fix**: a row naming only a holder ("Owners of the Company", "(b) Non-
  controlling interest") within three rows of a heading ending "attributable
  to" takes the heading's measure; if the two together name nothing the
  taxonomy knows, the row names nothing. A heading run into a row with figures
  still attributes (`_ATTRIBUTION_CLAUSE`).
- **Test**: `test_p22_generalization.py::TestAHolderRowTakesTheHeadingsMeasure`.
- **Docs**: HCLTECH_FY26, LT (Phase 22, found on documents never seen before).

### M10. A per-share context read as money
- **Example**: "Total dividend paid (in ₹)" under "Dividend per equity share of
  ₹ 2 each" filed as dividends paid (a cash-flow amount) in crore, unflagged.
- **Fix**: under a per-share heading (not an earnings heading, not "face
  value"), or in a label saying "per share", no monetary measure is read.
- **Test**: `test_p22_generalization.py::TestAPerShareContextIsNotMoney`.
- **Docs**: HCLTECH, HCLTECH_FY26.

### M11. Fuzzy matching across subjects
- **Example**: "Equity attributable to owners of the Company" (balance sheet)
  scored 0.91 against "profit attributable to owners of the company";
  "Profit on sale of property, plant and equipment" against "proceeds from
  sale of property, plant and equipment".
- **Fix**: subject classes (result / equity / cash movement / position); a
  fuzzy match whose two sides name subjects in different classes is refused.
  The standard wordings of equity attributable to owners added as aliases.
- **Test**: `test_p22_generalization.py::TestFuzzyMatchingDoesNotCrossSubjects`.

### M12. Two lines of one table read as one measure
- **Example**: "Construction materials consumed" matched cost of materials by
  similarity beside "Cost of raw materials and components consumed"; a
  heading "Other comprehensive income ... attributable to:" merged with the
  profit subtotal's figures beside the line that IS other comprehensive income.
- **Fix**: within a table and column, where two rows carry one key with
  different figures, the most direct reading (exact > prefix > fuzzy >
  dangling "attributable to") keeps it; weaker readings are not extracted;
  equally weak readings go to review (`metric_ambiguous`). Two exact readings
  (current and non-current "Borrowings") are two genuine lines.
- **Test**: `test_p22_generalization.py::TestTwoLinesOfOneTableReadAsOneMeasure`.
- **Docs**: LT, MARUTI.
- **Extended (final run `p22c`)**: where the raw-materials line matched nothing,
  there was no collision and "Construction materials consumed" stood alone as
  the cost of materials, unflagged (4 facts). A label that ends with an
  alias's core -- its words after a generic head such as "cost of" -- and puts
  a substantive word of its own where the head was names a narrower measure
  and is not fuzzy-matched; a word the measure's own aliases use ("raw" in
  "raw material cost") does not narrow it (`_qualifies_the_core`). Replayed on
  every recorded label of `p22c`: it changes the 4 wrong facts and nothing
  else. `test_p22_final.py::TestANarrowerKindOfTheSameCore`.

### M15. A figure on another printed line than its label
- **Example**: a scanned statement's heading "Other comprehensive income for
  the period/year attributable to:" was clustered into one row with the
  figures of the unlabelled total line 5 pt above it (34,469 -- the profit
  attributable), and filed as other comprehensive income; likewise the
  total-comprehensive-income heading with the OCI total (8 facts, 4 unflagged).
- **Fix**: where a one-line label and a figure in its row stand more than half
  a line apart vertically, the figure is marked `row_misaligned` and goes to
  review (`metric_ambiguous`). On the corpus it marks 9 figures: the 8 wrong
  ones and one already flagged; no correct figure.
  `test_p22_final.py::TestAFigureOnAnotherLine`.
- **Docs**: MARUTI.

### M17. A maturity bucket filed as the measure (section-40 audit)
- **Example**: an asset-liability maturity table's "Advances >2-3 M 21,389"
  filed as the lender's advances (36 facts, unflagged).
- **Fix**: a column headed as a maturity or ageing bucket ("1-7 D", ">6 M-1
  Y", "Over 5 years") is not a data column; the total column is.
- **Test**: `test_p22_final.py::TestPartsOfAMeasureAreNotTheMeasure`.
- **Docs**: BAJFIN_PPT.

### M18. People read as money (section-40 audit)
- **Example**: "Employees by Region and Functional Areas" (full-time
  equivalents) filed as general and administrative expense, € millions (24).
- **Fix**: a table whose title or stub head says its figures are people
  yields no monetary measure.
- **Docs**: SAP.

### M16. Both holders in one label
- **Example**: "Owners of the Company Non controlling interest" -- two lines
  read as one row -- filed as the minority's share of total comprehensive
  income, carrying the owners' figures (35,631).
- **Fix**: a holder row naming both an owners-type and a minority-type holder
  names nothing. `test_p22_final.py::TestTwoHoldersInOneLabel`.
- **Docs**: MARUTI.

### M13. A name a lender uses in its own sense (continued from M7)
- **Example**: a bank's "Operating Revenue" (NII + other income) filed as
  revenue from operations; a finance company's "Net Income" (income less
  finance cost, printed above "Profit After Tax") filed as net income meaning
  earnings.
- **Fix**: in a lender's table the bare name is net total income; "Net
  Income" beside a printed profit after tax is not the profit -- net total
  income for a lender, review elsewhere.
- **Test**: `test_p22_generalization.py::TestWhatALenderMeansByAName`.
- **Docs**: AXIS_PR, CHOLA_PR.

### M8. OCR damage to an acronym
- **Example**: "EB/TDA", "EB1TDA" for EBITDA; the latter matched the EBITDA
  *margin* by recognition repair.
- **Fix**: an "I" read as "/", "1", "l" between capitals is corrected only
  when the corrected spelling is a declared label; a rate alias needs a rate sign.
- **Test**: `test_p22_fact_safety.py::TestAcronymConfusions`.
- **Docs**: RIL (footnote).

## Prose and highlight tiles

Found by the section-40 human audit (Phase 22), not by the golden scorer:
the golden corpus holds tables only, so no prose figure had ever been scored.
Reading every prose and tile figure of `p22b` against its sentence found
about half of them wrong in some dimension, and none flagged.

### N1. The day of a date read as the amount
- **Example**: "Consolidated Networth as at December 31, 2025 was ₹175,251
  crore" filed as net worth **31**; "Customers as on December 31, 2025" as 31
  crore customers; a tile's "30 Jun 26" as AUM 30.
- **Root cause**: the first number with "crore" within 24 characters was taken;
  the date's "31, 2025" came first.
- **Fix**: digits inside a date or period label ("December 31, 2025",
  "31.03.2026", "Q3FY26", "JQ 25") are never a figure (`date_like_spans`); a
  tile whose value is a date is refused (`value_is_a_date`).
- **Test**: `tests/test_p22_narrative.py::TestDates`, `TestTiles`.
- **Docs**: KOTAK_PR, BAJFIN_PPT, HUL.

### N2. A rate read as an amount
- **Example**: "Net Payment Revenue ... reduced from 8.8bps to 8.4bps of GMV"
  filed as revenue ₹8.8 crore.
- **Root cause**: the basis-point test used `\bbps\b`, which never matches
  "8.8bps" (no word boundary between a digit and a letter).
- **Fix**: a figure whose unit, or the words after it, is a rate (%, per cent,
  bps, basis points, x, times) is never an amount.
- **Test**: `test_p22_narrative.py::test_a_basis_point_rate_is_not_an_amount`.
- **Docs**: PAYTM.

### N3. The earlier side of a comparison, or the change, read as the level
- **Example**: "losses increased from INR 45 crore to INR 94 crore" → 45;
  "Cash balance increased by INR 316 crore QoQ to INR 18,288 crore" → cash
  316; "AUM grew by 1.21 Lakh Crore" → AUM 1.21 lakh.
- **Fix**: a figure after "from", "against", "compared to", "vs" is the
  comparison's other side; after "grew by", "an increase of" it is a change;
  figures in brackets (comparatives, conversions, breakdowns) are skipped; a
  compound scale ("Lakh Crore") the parser cannot carry is refused.
- **Test**: `test_p22_narrative.py::TestFigureSelection`.
- **Docs**: ETERNAL, SBI_PPT, HUL.

### N4. A conversion's currency given to the figure beside it
- **Example**: "Gross revenue at ₹244.8 billion ($2,585.9 million)" — the ₹
  glyph read as "R" — filed as **USD** 244.8 billion.
- **Root cause**: the currency was read from a 24-character window that
  reached the bracketed dollar conversion.
- **Fix**: the currency and scale come from the figure's own neighbourhood,
  cut at the next bracket.
- **Docs**: WIPRO (five facts).

### N5. The measure named far from its figure
- **Example**: an offer-for-sale sentence filed its consideration (₹10,000
  crore) as net worth; "Earnings before interest, tax, depreciation and
  amortization (EBITDA)" filed as EBIT; "Capex per store ₹2.5 crore" as capex;
  the auditor's sentence about subsidiaries' revenues "before consolidation
  adjustments" as the group's revenue.
- **Fix**: the figure must follow its measure's name within eight words; a
  parenthesised abbreviation defines the measure; "per <unit>" between name and
  figure is a rate; an amount is never a ratio; the auditor's component
  sentence is not read.
- **Docs**: HDFC, AIRTEL, KOTAK_PT, HUL, ETERNAL, GROWW, ICICI.

### N7. A sentence cut at "Rs." (section-40 audit)
- **Example**: an auditor's paragraph about "one subsidiary, whose ...
  financial result ... includes total revenues of Rs. Nil, total net loss ...
  of Rs. 0.18 crore" was split after "Rs." -- the fragment lost the words
  that say whose figure it is, and 0.18 was filed as the group's
  comprehensive income.
- **Fix**: no sentence break after "Rs.", "No.", "Ltd." and like; the auditor's
  component paragraph is recognised in any wording and not read.
- **Test**: `test_p22_narrative.py::test_the_auditors_component_paragraph_in_other_words`.
- **Docs**: BAJAJAUTO_2023.

### N6. Prose confidence that the evidence does not support
- **Pattern**: every rule above removes one reading, and the audit showed how
  many kinds there are. A sentence has no column head to fix the period and no
  caption to fix the scale.
- **Fix (policy)**: a prose, tile or chart figure that no table in the
  document repeats carries `narrative_unconfirmed` and REVIEW_REQUIRED. One a
  table states merges into the table's fact (reconciliation) and is carried by
  it.
- **Test**: `test_p22_narrative.py::TestReviewPolicy`.

## Periods and columns

### P1. Growth columns read as quarters
- **Example**: SBI deck's "Q1FY27 / over / Q4FY26" growth heads kept only
  "Q4FY26"; interest income of 3.90 (a growth %) reported for Q4 FY26.
- **Root cause**: head recovery above the table band stopped at "over".
- **Fix**: comparison connectors and change heads are head text (`_HEAD_COMPARISON`).
- **Test**: `test_p22_fact_safety.py::TestAChangeColumnHeadOverThreeLines`.
- **Docs**: SBI_PPT (18 wrong facts).

### P2. A footnote marker on a change head
- **Example**: "Change¹ %" read "Changel %", missed as a change column.
- **Fix**: `GROWTH_HEADER_RE` tolerates a marker on the word.
- **Test**: `test_smoke_findings.py`.

### P3. Quarter and year sharing an end date
- **Example**: a bank's "Year ended 31.03.2026" typed as the quarter to 31 March.
- **Fix**: the span phrase governs every date form; the head's own span beats one
  borrowed from the table.
- **Test**: `tests/test_multiline_header.py`, `test_table_period_integrity.py`.

### P5. A year's head over its quarter (section-40 audit)
- **Example**: a deck's columns "FY 26" over "Q1 | Q4 | FY": the Q4 column
  read "FY 26 Q4" and was typed as the fiscal year, unflagged.
- **Fix**: "FY 26 Q4", "FY26 H1" are read as "Q4 FY26", "H1 FY26".
- **Test**: `test_p22_final.py::TestAYearHeadOverItsQuarter`.
- **Docs**: NTPC_PPT.

### P4. A flow figure under a bare date
- **Example**: a scanned results page whose "Quarter ended" group head was
  lost: the first column read "June 30, 2026 / Unaudited" and 19 lines of a
  quarter's P&L were reported as balances at June 30, unflagged.
- **Root cause**: a date with no span word is typed as a point in time.
- **Fix**: a FLOW measure under a head with neither a span phrase nor "as
  at/on" keeps the end date and gets an unknown span, and goes to review
  (`ambiguous_period`); a balance under the same head is still a balance.
- **Test**: `test_p22_generalization.py::TestAFlowUnderABareDate`.
- **Docs**: MARUTI.

## Entities and bases

### E3. A basis nobody stated
- **Example**: a bank's press release states no basis on any table; a
  footnote mentions "consolidated financial statements of the Group" for its
  subsidiaries' figures. All 41 figures were filed as consolidated.
- **Root cause**: the entity registry defaulted every region to the
  consolidated basis until something said otherwise.
- **Fix**: a region no heading, page header or column head reached has an
  unstated basis (`EntityType.UNKNOWN`); a heading naming only the filer says
  who reports, not on what basis.
- **Test**: `test_p22_generalization.py::TestNoSilentConsolidatedDefault`.
- **Docs**: AXIS_PR.

### E4. A basis stated only in the sentence that introduces a table
- **Example**: a consolidated results page's note (vi): "The specified items
  of the standalone financial results ... are given below:" and an untitled
  table. The page and section are consolidated; the table is standalone.
- **Fix**: the last sentence of a table's caption, if it introduces the table
  and names exactly one basis, is the basis of every column
  (`apply_introduced_basis`).
- **Test**: `test_p22_generalization.py::TestABasisStatedWhereATableIsIntroduced`.
- **Docs**: LT.

### E5. A basis the row's own label states (section-40 audit)
- **Example**: "Consolidated Profit" rows on a "Profit bridge - Standalone to
  Consolidated" slide filed as standalone (4 facts); "CRAR (standalone)" on a
  consolidated snapshot filed as consolidated (9).
- **Fix**: a row label naming exactly one basis gives its figures that basis,
  over the page's and the column's (`_one_basis`, `_rebase`).
- **Test**: `test_p22_final.py::TestTheRowSaysWhoseFigureItIs`.
- **Docs**: NTPC_PPT, BAJFIN_PPT.

### E6. Two tables stacked under one caption (section-40 audit)
- **Example**: a subsidiaries list ("The consolidated financial results include
  results of the following companies:") and, below it in the same region, "Key
  standalone financial information is given below:" and its figures. The
  caption's sentence made every column consolidated.
- **Fix**: a label-only line inside a table that introduces the rows below it
  with one basis gives those rows that basis.
- **Test**: `TestTheRowSaysWhoseFigureItIs::test_a_line_introducing_the_rows_below`.
- **Docs**: BAJAJAUTO_2023.

### E7. A title band's number made it another band (section-40 audit)
- **Example**: "19-years financial snapshot - Consolidated" then "9-years
  financial snapshot - BHFL": the subsidiary's slide carried the consolidated
  basis (108 facts, unflagged). The E3-era rule compared title prefixes, and a
  prefix could not start with a digit.
- **Fix**: a title prefix may begin with "N-years"/"N quarters", and prefixes
  are compared with their numbers removed (`_prefix_key`).
- **Test**: `test_p22_final.py::TestAPageTitleOfTheSameBand`.
- **Docs**: BAJFIN_PPT.

### E8. A damaged word hid a statement's basis (section-40 audit)
- **Example**: "Statement of Audited Standalone Financial Resuits" -- the page
  kept the consolidated basis of the page before, and 49 standalone figures
  were filed as consolidated (16 unflagged).
- **Fix**: the title pattern accepts the recogniser's damage to "results".
- **Test**: `TestAPageTitleOfTheSameBand::test_a_damaged_results_title_still_states_its_basis`.
- **Docs**: AIRTEL.

### E1. Standalone and consolidated side by side
- **Example**: SBI prints both in one table under two group heads.
- **Fix**: a group head naming a basis governs the columns beneath it.
- **Test**: `tests/test_column_basis.py`.

### E2. A segment column given to the group
- **Example**: StanChart's "Retail Banking" column's figures attributed to the group.
- **Fix**: a period-less column naming another dimension leaves the entity
  unstated (`column_qualifier`).
- **Test**: `test_smoke_findings.py::TestAColumnThatNamesASegment`.

## Qualified figures

### Q1. One measure and period, two stated bases
- **Example**: RIL's page 1 prints 1Q FY26 PBT 28,222 "excluding" an 8,924
  crore gain (footnote); the statement prints 37,146. They were one fact in
  conflict with itself.
- **Fix**: the qualification model (`app/extraction/qualification.py`): column
  heads and footnotes state a basis; it is carried only through the page's own
  arithmetic; differently qualified figures are distinct facts.
- **Test**: `test_p22_qualification_and_checks.py`.
- **Docs**: RIL benchmark.

## Accounting checks

### A1. An identity for the wrong kind of statement
- **Example**: the bank total-income identity failed four times on a conglomerate.
- **Fix**: statement categories (BANKING / NBFC / GENERAL_CORPORATE) from the
  lines a table prints; identities declare where they apply.
- **Test**: `test_p22_qualification_and_checks.py::TestWhichStatementAnIdentityIsFor`.

### A2. A margin on the other revenue line
- **Example**: EBITDA margin 15.9% (on gross revenue) warned against net revenue.
- **Fix**: a margin passes if it matches any revenue line the statement prints.

### A3. Subsidiaries summed to the group
- **Example**: listed subsidiaries' profit exceeding consolidated profit.
- **Fix**: only segments are summed; an excess is inconclusive (eliminations).

### A4. Identities that never ran
- **Example**: the bank expenditure identity named `finance_costs`; the
  cash-flow identity targeted `cash` (a balance).
- **Fix**: corrected keys; undated table figures are checked within their column.

### A5. A total that names other lines than the identity's
- **Example**: "Total comprehensive income (9+10)" -- line 9 is profit after
  the share of joint ventures -- tested as "Net profit after tax (5-6)" (line
  7) plus OCI; a correct statement "failed" by exactly the joint ventures'
  share (-184.47). The year's column passed only inside the tolerance.
- **Fix**: where a total's label names its lines ("(9+10)", "(IX+X)") and an
  operand prints a line number of the same kind not among them, the identity
  is not this statement's arithmetic and is not run (`_not_the_declared_lines`).
  `test_p22_final.py::TestATotalThatNamesItsLines`.
- **Docs**: LT.

### A7. A cash-flow statement's own layout (section-40 audit)
- **Example**: a statement printing the effect of exchange rates above its net
  increase in cash failed "operating + investing + financing = net change" by
  exactly that effect; every figure was correct.
- **Fix**: both layouts are tested and the one the statement obeys is kept.
- **Test**: `test_p22_final.py::TestTheStatementsOwnCashFlowLayout`.
- **Docs**: TOYOTA.

### A6. An outflow reported as a suspicious negative
- **Example**: "Purchase of property, plant and equipment (44,049.78)" --
  capital expenditure printed as the cash it used -- warned as "normally
  positive" (2 false alarms).
- **Fix**: in a cash-flow statement a bracketed or minus figure is the
  statement's direction, like a bracketed cost.
  `test_p22_final.py::TestAnOutflowIsNotASuspiciousNegative`.
- **Docs**: NTPC_FY26.

## Evaluation

### X1. A scorecard from three code versions
- **Example**: Phase 21's `after11`.
- **Root cause**: `corpus_run.py` skipped documents already recorded for a tag.
- **Fix**: a tag is one run with one code fingerprint and configuration
  (`app/evaluation/provenance.py`); mixed tags are refused; every scorecard
  writes `evaluation_manifest.json`.
- **Test**: `test_scorer_v2.py::TestAnEvaluationIsOneState`.

### X2. A scorer tolerance that paired the wrong figure
- **Example**: SAP diluted EPS 3.54 paired with basic EPS 3.55 (0.5% tolerance).
- **Fix**: values compared at their printed precision.

### X4. A timing taken while the machine slept
- **Example**: a 40-page scanned filing reported 7,955 s; exactly the six
  pages in flight (one per worker) each took ~7,600 s and finished at the same
  instant, every other page 40-70 s: the machine was suspended mid-run.
- **Fix**: timings come only from the controlled benchmark
  (`scripts/perf_benchmark.py`: one measurement at a time, fresh process,
  own cache); a corpus run's wall time is not a performance measurement.

### X5. A ratio unit the scorer could not score
- **Example**: a correct debt-equity ratio of 1.09 would have been scored as a
  ratio "read in the wrong unit": the unit check had no branch for multiples.
- **Fix**: `check_unit` knows "ratio". `test_scorer_v2.py::TestAMultipleIsNeitherAnAmountNorAPercent`.

## Performance

### Perf1. The second read rendered each cell on its own
- **Example**: a 29-page scanned filing spent 247 s re-reading 621 cells --
  more than its OCR -- because a clip render of a scanned page decodes the
  whole page image once per cell (610 ms a cell; recognition 314 ms).
- **Fix**: render the page once at the second-read resolution, crop every
  cell, recognise on a thread pool; a disagreement is confirmed by the cell's
  own clip before it stands. Compared cell by cell over 5,763 corpus cells
  before adoption (docs/PERFORMANCE.md).
- **Test**: `tests/test_second_read_strategy.py`.

### X3. A label the scorer could not key
- **Example**: "16Minority Interest" (row number run in) called a wrong-row figure.
- **Fix**: golden row positions pair facts by where they sit, where the
  table's positions are verified consistent.
