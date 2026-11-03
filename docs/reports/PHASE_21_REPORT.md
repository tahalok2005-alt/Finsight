# Phase 21 — Real-world corpus expansion and generalization hardening

Every number below is measured, from recorded runs, and reproducible with the
commands in the appendix. Golden facts were read from the source PDFs by eye —
never from FinSight output. The final state is tag **`after14`**: one code
state, every gate run on it. Where an acceptance criterion was not met, this
report says so.

---

## 0. The short version

Phase 20 closed at 137/151 golden facts (90.7%) on **two** documents from **two**
companies. That figure could not say whether anything generalised. Phase 21
built a corpus of **20 documents from 15 companies** (17 new official filings,
353 new pages), transcribed **2,616 golden facts** across **28 tables** from the
source PDFs, processed every document *before* changing any code, classified
every failure into the brief's categories A–Q, and fixed the causes generally.

**On the 14 new documents that carry golden tables (2,465 golden facts), like for like:**

| | Before (baseline) | After (`after14`) |
|---|---|---|
| Golden facts found | 529 (21.5%) | 1,524 (61.8%) |
| **Correct** — every stated dimension right | **69 (2.8%)** | **767 (31.1%)** |
| Incomplete — nothing wrong, something unknown | 165 | 749 |
| **Wrong** — a dimension asserted wrongly | **295** | **8** |
| — of which carried no review flag | 113 | 5 |
| Misread/fabricated figures under a known row name | 57 | 18 |
| Figures attached to another row's name | 0 | 4 * |
| Entity/basis asserted wrongly | 124 | 0 |
| Currency asserted wrongly | 147 | 0 |
| Duration asserted wrongly | 12 | 1 |
| Unit asserted wrongly | 10 | 0 |
| Value wrong | 51 | 8 |

\* All four are one SBI row, "16Minority Interest": the row number is run into
the label, so the scorer cannot key it. FinSight's figure and measure (the
minority's share of profit) are both right; I checked each by eye.

**Whole corpus (16 golden documents, 2,616 facts):** **912 correct** (34.9%),
1,669 found (63.8%), **8 wrong** (5 unflagged). 98.2% of what FinSight asserts is
fully right. Period end, entity, unit and currency are 100% right wherever
FinSight states them.

**The measure itself.** The scorer pairs a golden row with a fact by label and
cell. It never checked the canonical metric FinSight files a figure under,
because the golden rows record printed labels, not taxonomy keys. So I read all
label→metric pairings among found golden facts by eye (377 at `after12`, 371 at
`after14`). 28 found facts were under the wrong measure, and the scorer's
wrong-row list held 4 more (ICICI's basic EPS filed as the tax charge). The
causes were fixed (§4.11), and 4 remain (SBI's deck "Operating Profit" filed as
EBIT ×3, one SAP basic-EPS figure under the diluted row). None of the four is
among the 912 correct.

**Existing corpus (the Phase 20 checker, unchanged):** **145/151 = 96.0%**
coverage, **100%** accuracy (Phase 20: 137/151 = 90.7%). Mid-phase this read
149/151. The 4 facts it lost were Groww's "Income tax relating to above", which
FinSight had been filing as tax on items that *will* be reclassified. The source
says *will not*: the next section's heading had been run into the label. They
are now refused (§4.11). Four of the gained facts are one HDFC row I renamed in
the golden file to carry its parent ("d) Others" → "Interest earned: d)
Others", values untouched); without them the figure is 141/151 = 93.4%.

**Benchmarks:** RIL image-only 1.0000 (held), RIL scanner-degraded
0.7143 → **0.8511**, Apple 44 → **85** facts — and **RIL digital 0.9882 → 0.9765:
a regression of one answer-key fact**, explained in §4.1.

**Documents with no golden table** (smoke-checked at the end, §4.10): Standard
Chartered HY 2026 (165 pp.) and IDFC FIRST (75 pp.). Reading their output found
five more wrong-fact classes, all older than Phase 21 and all general. All five
were fixed. StanChart dropped from 675 facts to 478, and 170-odd of the removed
facts had been wrong (118 were "Credit cards" filed as payables). Its currencies
are intact (424 USD, 54 unknown, no rupees).

**Accounting validation:** 400 checks, 168 alarms, **0 false alarms** (9 at
`after8`), 2 true catches.

**Speed:** the extraction stage across the 17 new documents fell from **1,354 s
to 142 s** (9.5×).

**Tests:** backend **1,102 passed, 1 skipped, 0 failed**; **253 new Phase-21 test
cases**. Frontend: typecheck clean, build succeeds, lint at the 6-warning baseline.

### Acceptance criteria — stated plainly

| | Criterion | Met? |
|---|---|---|
| A | No benchmark regression | **No.** RIL digital lost one answer-key fact (0.9882 → 0.9765). Both figures involved are extracted correctly; the release prints two different PBT figures for Q1 FY26 under one label (§4.1). Every other benchmark held or improved. |
| B | No known wrong period / entity / unit facts | **Not fully.** On the golden corpus: 0 wrong period ends, 0 wrong entities, 0 wrong units, 0 wrong currencies. But **one duration is wrong**: an ICICI diluted-EPS cell carries the fiscal-year figure (75.89) where the quarter's (20.36) belongs, unflagged. So one known wrong period fact remains. Also known and not fixed: 8 wrong values (digit misreads, 5 unflagged, §15) and 4 facts under the wrong measure. |
| C | Generalization demonstrated | **Partly.** Fifteen companies, eleven layouts that failed differently. Correct facts on the new documents rose from 2.8% to 31.1%, and wrong assertions fell from 295 to 8. But seven of the fourteen new golden documents still have **zero** fully-correct facts: their figures are found and honestly incomplete (currency glyph unreadable, period head not recovered). That generalises *not being wrong*; it does not yet generalise *coverage*. |
| D | Coverage improves over 90.7% on the existing corpus | **Met**: 96.0% (93.4% excluding the renamed golden row), accuracy 100%. |
| E | New corpus measured and documented | **Met**: manifest, 28 golden tables, dashboards per run, this report, `docs/EVALUATION.md`. |
| F | Every systematic failure fixed generally or documented | **Met.** Each is fixed in §4 or listed with its cause in §15. |
| G | Zero company-specific hacks | **Met.** §16 says how that was checked. |

---

## 1. Corpus (sections 2–5)

| Company | Sector | Document(s) | Pages | Notable shape |
|---|---|---|---|---|
| ICICI Bank | private bank | Q1 FY27 results | 13 | standalone + consolidated results and balance sheets |
| Wipro | IT services (IFRS) | Q1 FY27 press release | 12 | ₹ statements with a US$ convenience-translation column |
| Bharti Airtel | telecom | Q1 FY27 results | 15 | ₹ millions, unlabelled subtotals |
| Tata Steel | steel | SEBI results, press release, presentation | 44 | "Quarter ended" over each column; year head over three lines |
| Hindustan Unilever | FMCG | results | 30 | **labels in the middle**, figures on both sides |
| SAP SE | software (IFRS) | Q2 2026 statement | 23 | € (read "E"), "Q2 2026", balance-sheet dates only in the title |
| Eternal | consumer internet | Q1 FY27 results | 31 | statements as images |
| State Bank of India | public bank | results, analyst presentation | 59 | standalone and consolidated **side by side** in one table |
| Indian Bank | public bank | results | 21 | space thousands separator; head above the cut band |
| Kotak Mahindra Bank | private bank | board outcome (**scanned**), press table (typeset), press release | 32 | three durations in six columns; scanned and typeset copies of one statement |
| MapmyIndia | tech | newspaper extract | 3 | lakh, other companies' adverts on the page |
| Bajaj Finance | NBFC | investor presentation | 70 | 71-column slide tables; consolidated and parent slides |
| Groww, HDFC Bank, Paytm | (Phase 20) | results | 48 | kept, re-measured |
| Standard Chartered, IDFC FIRST | (earlier phases) | HY 2026 report, Q1 FY27 deck | 240 | no golden table; smoke-checked (§4.10) |

Every file came from the filer's or the exchange's official site. The manifest
records the URL, SHA-256, page count and the shape/period/unit/entity matrices,
all written before processing. Infosys was dropped (bot-protected download).

## 2. Golden corpus (sections 6–8)

28 tables, **2,616 facts**, from 16 documents. By statement: income statement
1,684 (+151 Phase 20), balance sheet 340, cash flow 193, KPI 248. By basis:
consolidated 1,875, standalone 644, basis not stated 97. By period: quarter
1,490, fiscal year 522, balance 358, nine months 110, half year 136. Every printed
subtotal was checked as an identity against its transcribed components before a
file was written, which caught two of my own misreads (Eternal cash flow
806→906, (387)→(367)). Rows that mean nothing without their parent carry it
("Tax expense: Current tax"). At the end of the phase four SAP rows were brought
into line with that rule ("Attributable to owners of parent" → "Profit (loss)
after tax: Attributable to owners of parent"), with the printed words kept in
`printed_label` and the values untouched.

## 3. Method (sections 9–10)

The corpus was processed once, **before any code change** (`baseline`). Every
missing, wrong or incomplete golden fact was then classified by walking the
stored evidence (OCR cache → table → cell → label → fact):

| Category | Baseline | Final |
|---|---|---|
| A. OCR | 169 | 262 |
| C. table fragmentation | — | 1 |
| D. column geometry | 201 | 98 |
| E. header recognition | 165 | 603 |
| G. duration parsing | 12 | 0 |
| H. entity/basis | 92 | 0 |
| I. unit/currency | 140 | 146 |
| J. number parsing | 125 | 14 |
| K. metric taxonomy | 1,043 | 391 |
| L. row segmentation | 214 | 157 |
| O. safety gate | 235 | 32 |

Baseline counts are over 17 documents, final over 20. E and A rose because
facts that used to be missing are now found and honestly incomplete, or have
reached the stage where OCR is the limit. Tagged runs `after1` … `after14`
followed, each scored from its recorded run without reprocessing. `after2` was
stopped (too slow before the taxonomy cache) and `after11` discarded (§18).

## 4. What was fixed, and the defect that showed it

Every fix is general and has a regression test built from the real page's
geometry or text. Each was measured on the whole corpus before the next.

### 4.1 Currency (I) — 147 wrong → 0

* The document vote counted **amounts** in prose ("$480 million") and phrases
  naming two currencies, so a filing whose ₹ glyph OCR cannot emit became
  dollars. A declaration now states a unit, never an amount, and a symbol counts
  only with a magnitude; a telecom's footnote markers ("$ Passive
  infrastructure…") had been voting dollars.
* After the vote declined, a fallback took **the first currency symbol on the
  opening pages**. It is removed: the currency is what the document declares, or
  nothing. Crore/lakh still imply rupees, flagged `currency_inferred`.
* A table that declares its unit with an illegible glyph stays currency-unknown;
  it may not borrow the document's.
* A currency named in some columns' own heads (Wipro's US$ translation column)
  belongs to those columns and cannot become the table's.
* **RIL digital, 0.9882 → 0.9765.** Page 1 prints Q1 FY26 PBT 28,222 and PAT
  21,757 with a footnote: "1Q FY26 … excludes ₹8,924 crore … profit from sale
  of listed investments". The statutory statement on page 25 prints 37,146 and
  30,681 under the same labels. FinSight extracts all four correctly and flags
  both pairs `conflicting_sources`. The benchmark keeps the highest-confidence
  candidate per metric/period, and with both at 0.64 the tie now falls to page
  25 for PBT. I did not change the evaluator to recover the number. Resolving it
  properly needs footnote-scoped measures ("excluding exceptional items"), which
  FinSight does not yet model.

### 4.2 Entity and basis (H) — 124 wrong → 0

* Column **group heads** name a basis: SBI prints standalone and consolidated
  side by side, and each column now carries the basis of the head above it.
* Slide titles name the basis after a dash ("Financial summary – Consolidated").
* A page whose title band names **something else** where its sibling names a
  basis ("Financial summary – Bajaj Finance") is recorded as *basis not stated*
  instead of falling to the default consolidated group (57 wrong → 0, now
  honestly incomplete).

### 4.3 Column geometry (D) — 201 → 98

* **Rulings that cut through figures are not columns.** SBI's line finder
  returned 24 strokes, several through the consolidated figures, giving
  seventeen columns for eight. Rulings are also accepted only when they give
  every column of figures a band of its own; serial numbers no longer count as
  figures.
* **Group heads are placed by partition.** Overlap is binding, and the rest are
  assigned by a DP over contiguous groups under both a centred and a
  left-aligned model, keeping whichever fits the printed heads. No fixed rule
  assigned SBI's eight columns correctly; this does.
* A head is over a column only when a meaningful part of it is (15% of its
  width, capped at about half a line). Tata Steel's column boundary fell 2.5 px
  into "Financial year", and the year's figures became balances.
* **Labels in the middle** (HUL): 4 facts → 203.
* **Serial numbers split over two columns** (Kotak scanned): 0 facts → 102.

### 4.4 Header recognition (E)

* Several printed lines in one header band are read as separate levels.
* A head printed **above the cut band** is recovered: audit status counts as
  head text, label-column words are set aside instead of disqualifying the line,
  and the recovered lines go through the header block (Indian Bank: 0 dated →
  108).
* A dateless span phrase ("Quarter ended") is a group head by what it says,
  unless it repeats once per column (Tata Steel's case).
* Masthead lines (bank name, address) no longer reach a column's period text. A
  title whose spaces OCR lost ("…QUARTERENDEDJUNE30,2026") is still a title.
* The two-date guard judges the text the period was read from. The column's
  header path is the header block's evidence, so a later calendar re-read cannot
  undo it.
* A change column whose head carries a footnote marker ("Change¹ %", read
  "Changel %") is still a change column. StanChart's changes in its capital
  ratios ("47bps") had been reported as the ratios.

### 4.5 Period and duration (F, G) — 12 wrong durations → 1

* A span phrase governs every date form ("Nine months ended 31-Dec-24").
* **The head's own span beats a span borrowed from the table.**
* A span printed over two lines ("Financial year" / "ended on") is read together.
* "Q2 2026", "Q1–Q2 2026" (dated only under a December year end) and month
  ranges are read ("Jan–Jun 2026" had been a *balance* on 30 June), as is the
  year end an interim balance sheet states ("as at 6/30/2026 and 12/31/2025").
* A fiscal label printed beside its own date keeps the date ("Year ended March
  31, 2026 (FY2026)" had no end date under an unknown calendar).

### 4.6 Numbers (J) — 125 → 14

* A cell holding two figures is refused, not concatenated (Eternal "revenue"
  2,021,117,292).
* A thousands comma read as a point is repaired in whole-number tables, and a
  decimal point read as a colon in two-decimal tables ("12,351:26").
* A period head ("1Q FY27") is not a figure.

### 4.7 Taxonomy (K) — 1,043 → 391

* Label canonicalisation applied to both sides: enumerators, note references,
  subtotal formulas, face-value clauses, currency brackets, bullets, "Less:".
* Exact lookups that survive lost spaces and recognition confusions. A key
  naming two metrics is dropped, never guessed.
* Standard-format vocabulary: Ind AS Schedule III, the SEBI results format for
  banks and Form A, IAS 1/IAS 7, plus industry lines every filer in an industry
  prints (telecom access charges, advertising and promotion). No company's own
  segment names.
* Precision guards:
  * A label that adds a substantive word to an alias names a different measure
    ("Network operating expenses" had been operating expenses; "Deferred tax
    assets" the tax charge).
  * Two labels that each name something the other lacks are different
    ("customer assets" had been subscribers).
  * "cash", "will not be" and "reserve" are contrast markers.
  * CET1 is split from Tier 1, and PBT from PBT before exceptional items.
* Names whose meaning depends on the statement resolve by the columns' "As
  at"/"… ended": "Minority interest" by the columns, and "Total provisions" (a
  balance, or a bank's charge for the period) the same way. Names that mean an
  amount or its rate ("Gross NPA") resolve by the figure's own marks.

### 4.8 Rows (L) and the safety gate (O) — 214 → 157, 235 → 32

* A row that means nothing alone is read with its heading (no colon needed),
  its parent total ("(d) Others" → other interest earned), or the label line it
  wraps onto — always by exact taxonomy match only.
* A rate in a row of amounts, or in a column headed as a rate, is not the row's
  value ("Profit after tax = 28%").
* A component of a same-named total ("(c) Other income" under "Other income
  (a+b+c)") is marked, even when a wrapped line breaks the enumeration.

### 4.9 Fragmentation (C)

* A band of column heads over a table's value columns is that table's head,
  even though it is narrower than the table (Wipro's income statement).

### 4.10 Found by the smoke runs of documents with no golden table

Standard Chartered and IDFC were re-run at the end to check that nothing had
broken shapes no fix targeted. Their currencies and periods were intact. Their
output showed five older defects, all general:

| Defect | Seen on | Fix |
|---|---|---|
| A hyphen inside a word ended an entity name: "Risk-weighted assets by business" registered a segment "Risk" | StanChart (16 facts) | a dash or colon ends a name; a hyphen between letters does not |
| A recognition repair (collapsing a/o, l/i, lost spaces) made undamaged labels meet aliases they do not name: "Credit Cards" → *creditors* (payables), "As a % of Revenue" → cost of revenue, "Asset Management" (a segment) → AUM, "Operating expenses to NTI" (a ratio) → operating expenses | StanChart (118 "Credit cards" facts), IDFC, Kotak (18), Bajaj (12), Eternal, Paytm | a repair must account for every word of the alias and leave at most three letters over; it corrects letters and never supplies, removes or swaps a word. Across all 5,689 corpus labels this changed 13, 11 of them wrong matches refused |
| "Leverage ratio" was an alias of net debt/EBITDA; for a bank it is Basel III Tier 1 over exposure, for an NBFC assets over equity | Bajaj (27 facts), StanChart | not guessed |
| A segment column's figures were given to the group | StanChart (37 facts) | a column with no period whose head names something other than scale, time, basis or a measure word ("Retail Banking", "Level 1") leaves the entity unstated and records the head as `column_qualifier` |
| Change column with a footnote marker | StanChart | §4.4 |

### 4.11 Found by auditing the measure

The measure audit (§0) found these. Each case had a figure read correctly and
filed under the wrong measure:

| Defect | Seen on | Fix |
|---|---|---|
| Two label lines run together: "Other income (e)+(f) Premium and other operating income from insurance" filed as the premium. An image statement's "Profit for the period (VII-VIII) Other comprehensive income … Income tax relating to above items" filed as profit. Groww's "Income tax relating to above Items that will be reclassified…" filed as tax on the opposite OCI bucket | ICICI, Eternal, Groww | refused. Nothing in the text says which line owns the figures: ICICI's were the first line's, Eternal's the last. A mid-phase attempt to take the first line created 19 wrong Eternal facts and was reverted (§18) |
| A line opening mid-phrase read alone: "of tax expense (not annualised)", the second line of a basic-EPS label, filed the EPS figures (20.65…) as the **tax charge**, unflagged | ICICI (4 facts) | a line that opens with a connective (not "of which") names nothing alone |
| "Profit for the quarter / year attributable to :" (the total, with the heading of the split run in) filed as the owners' share | Airtel | "attributable to" with no one after it qualifies nothing |
| "Attributable to owners of parent" under "Profit (loss) after tax" filed as balance-sheet equity, and the NCI line as other comprehensive income | SAP | a subject-less line is read with the row above it (exact match only) and never fuzzy-matched to an alias that names a subject |
| "Share-based payment reserve" (equity) filed as the expense | Wipro | "reserve" is a contrast marker ("Reserve Bank" is a name) |
| "Total Provisions" on a P&L slide filed as the balance | SBI deck | resolved by the columns (§4.7); this slide's columns do not say, so it is now unmatched |

The measure is now read correctly on everything the audit covered, except the 4
facts listed in §15.

## 5. Per-document results (final, `after14`)

| Doc | Golden | Correct | Incomplete | Wrong | Missing |
|---|---|---|---|---|---|
| ICICI | 331 | 190 | 4 | 2 | 135 |
| Wipro | 223 | 14 | 159 | 0 | 50 |
| Airtel | 159 | 0 | 68 | 2 | 89 |
| Tata Steel | 145 | 117 | 0 | 0 | 28 |
| HUL | 178 | 0 | 76 | 0 | 102 |
| SAP | 272 | 0 | 184 | 0 | 88 |
| Eternal | 263 | 0 | 114 | 3 | 146 |
| SBI | 184 | 149 | 0 | 1 | 34 |
| SBI presentation | 97 | 0 | 27 | 0 | 70 |
| Indian Bank | 108 | 102 | 0 | 0 | 6 |
| Kotak (scanned) | 153 | 101 | 0 | 0 | 52 |
| Kotak (typeset) | 153 | 94 | 0 | 0 | 59 |
| MapmyIndia | 48 | 0 | 2 | 0 | 46 |
| Bajaj Finance | 151 | 0 | 115 | 0 | 36 |
| Groww | 99 | 93 | 0 | 0 | 6 |
| HDFC | 52 | 52 | 0 | 0 | 0 |

The per-table breakdown, with the top cause of every miss, is in
`data/corpus/DASHBOARD_after14.md`.

## 6. Accounting validation (section 19)

400 checks run (139 at baseline): 214 pass, 149 warnings, 33 failures, 4 for
review. Accounting equations are never used to rewrite a value; they find
extraction errors and flag them. Five changes made the engine's verdicts
trustworthy:

* **A total's own formula decides applicability.** "Total tax expense [8(a) to
  8(c)]" has three components, one of which the identity does not know, so it is
  not a failure.
* **Terms come from one table**, or across tables only when every term is
  uncontested. Page 1 of RIL (excluding a gain) had been tested against page
  21's tax.
* **A component of a same-named total is never that total** ("(c) Other income").
* **A result struck after further lines is not PBT less tax.** A bank's
  consolidated "Net profit for the period" comes after "Less: share of minority"
  and "Add: share of associates". Where such a line (non-nil) sits between the
  terms and the result in the same table, the identity uses another printing of
  the result that it does describe ("Net profit after tax before minority
  interest") or none.
* Expenses printed in brackets are the IFRS convention, not an implausible
  negative.

False alarms (every involved figure correct): 9 at `after8` → **0**. True
catches: 2 (Eternal's capex misread 71 for 711, SBI's total income with a lost
decimal).

## 7. Cross-source corroboration and restatements (sections 21–22)

See `data/corpus/CORROBORATION_after14.md`. The scanned and typeset Kotak
statements were processed independently and **agree on 181 of 184** shared
figures. The three disagreements are all errors in the scan, and corroboration
is how they showed: a decimal point lost (debt/assets 368 vs 3.68), a digit
misread (FY25 tax 5,334.03 vs 5,134.03), and separators lost (gross NPAs
6,266.25 read as 6.26625). The last one first surfaced as a taxonomy error, the
typeset amount filed under the NPA *ratio*, which was fixed. SBI's results and
presentation, and Tata Steel's three documents, share no dated keys: the
presentation and press-release figures have no period ends FinSight could
establish, so they cannot yet corroborate the statements. No restatement is
observable in this corpus, since no two documents report the same period twice
with dated figures.

## 8. Banking and non-banking (sections 23–24)

The corpus has seven banks (counting IDFC and StanChart), one NBFC and nine
other companies. Bank identities are the bank format's own (interest
earned + other income = total income; interest expended + operating expenses =
total expenditure). The corporate identity "revenue + other income = total
income" is blocked for any statement that reports interest earned. Bank
vocabulary comes from the prescribed formats: interest expended, provisions and
contingencies, provisions for NPA, NPA amounts and ratios, CET1/AT1/Tier 2,
Form A balance-sheet lines, and the insurance-business lines of consolidated
banks. Best-covered: Indian Bank 102/108, SBI 149/184, HDFC 52/52, ICICI
190/331, Kotak 101/153 and 94/153. Non-banking: Tata Steel 117/145 and Groww
93/99 are the strongest. IFRS filers (SAP, Wipro) and image or slide tables are
the open front (§15). One bank-specific meaning is still unmodelled: in a
bank's deck "Operating profit" is profit before provisions, not EBIT (§15).

## 9. Metrics tracked separately (section 25)

Coverage, period end, duration, entity, unit, currency and value accuracy,
accounting pass rate, review rate and false-alarm count are reported
separately in every dashboard (§0 table). The measure is audited by eye (§0),
because the golden files cannot score it. Review rate over found golden facts
fell from 46% to 24% as wrong assertions disappeared.

## 10. Performance (sections 26–27)

| | Baseline | Final |
|---|---|---|
| Extraction stage, 17 new documents | 1,354 s | 142 s |
| `match_metric`, uncached label | 175 ms (57 ms before the vocabulary grew) | 4 ms |
| Full 20-document replay, OCR cached | — | 7–10 min |

OCR was not "optimised": its cost is unchanged and cached per page. The
extraction-stage cost came from taxonomy lookups re-normalising every alias and
recomputing recognition keys on every call. Those are pure functions and are now
cached; results were identical before and after.

## 11. Frontend review and dashboard (sections 29–30)

The figure panel now shows when a currency or scale was **carried** to a figure
rather than printed with it ("INR — inferred, not printed with the figure").
The backend has always recorded this, but the UI never showed it, so a reader
could not tell a stated currency from a borrowed one. Verified in the running
app on the RIL release, where all app requests returned 200. Entities with an
unstated basis display as "Basis not stated", and a figure from a segment column
as "Entity not attributed". Both paths already existed in the UI and were
checked for null entities. The developer dashboard is the generated
`data/corpus/DASHBOARD_<tag>.md`: corpus, quality, failure taxonomy, per
document with timings, per golden table, and a comparison with a prior tag.

## 12. Tests (sections 31–33)

**253 new test cases**: 248 in 16 new files, and 5 added to
`test_entity_headings.py`. Each is built from a real page's geometry or text.
They cover currency evidence, column basis, merged cells, multi-line heads,
label canonicalisation (63), side-by-side bases, row composition, the
translation column, heads above the band, middle labels, accounting
applicability, unstated basis, scanned statements, recognition repair, the
smoke-run findings and metric identity. 225 test cases exercise period and
duration handling by name (the brief asks for 50+). The adversarial cases are
well over the 100 asked for: figures run together, rules through figures,
repeated vs grouped heads, a rate in a row of amounts, a component alone, a name
for an amount and its rate, two lines run into one label, a reserve against its
charge, and a repair that would swap a word. Three existing tests were updated
because a label now reaches its own new metric rather than none; each still
asserts the opposite measure is never matched.

## 13. Final gate (section 36), all on `after14`

| Gate | Result |
|---|---|
| Backend suite | **1,102 passed, 1 skipped, 0 failed** |
| Golden, Phase 20 checker | 145/151, 100% accuracy |
| Golden, full corpus | 912/2,616 correct, 8 wrong |
| Period / table / merge / number / unit / currency / entity / accounting / reconciliation / anti-fabrication / adversarial | in the suite, all passing |
| HDFC, Groww | 52/52, 93/99 |
| Paytm | processed (33 facts), no golden table |
| RIL / image-only / degraded / Apple | 0.9765 (−1 fact) / 1.0000 / 0.8511 / 85 facts |
| Standard Chartered | 478 facts, 222 dated; USD 424, unknown 54, no rupees; 352 group, 126 not attributed (segment and qualifier columns) |
| IDFC FIRST | 103 facts, 46 dated; 8 US$ (printed as US$ on the slide), 2 INR, 93 unknown (₹ glyph) |
| All new companies | measured, §5 |
| Frontend | typecheck clean, build succeeds, lint 6 warnings (baseline 6) |

## 14. Source-to-FinSight comparisons (section 35)

See `data/corpus/COMPARISON_after14.md`: 20 golden facts per company for ten
companies, evenly spread, each beside FinSight's reading of the same cell. Of
the 200, **78 correct, 55 incomplete, 66 missing, 1 wrong**. The wrong one is
Airtel's share of associates, printed "(828)" and read without its sign. By
company: Indian Bank 19/20 correct, SBI 17, ICICI 14, Tata Steel 14, Kotak 13;
Wipro, SAP, Eternal, HUL and Airtel are mostly incomplete or missing.

## 15. Known limitations — what is still missing or wrong

* **Wrong values (8, 5 unflagged)**, all OCR on image, scanned or dense pages:
  * ICICI's diluted-EPS row takes neighbouring figures (21.54 for 21.29, and the
    year's 75.89 in the quarter's column — the one wrong duration).
  * Airtel's share of associates lost its sign twice.
  * Eternal's cash-flow image: −71 for −711, −168 for −1,168, −167 for −161.
  * SBI's total income lost its decimal point (flagged).
* **Wrong measure (4)**:
  * SBI's analyst deck "Operating Profit" filed as EBIT (×3). In a bank's deck
    it is profit before provisions; the taxonomy has no industry context yet.
  * One SAP basic-EPS figure sits under the diluted row (row alignment).
* **Currency glyphs** (I, 146 unknown): the OCR model cannot emit ₹ or €. SAP's
  "€ millions" reads "E millions", and Airtel's "(₹ in Millions)" reads "(Kin
  Milions)". Those figures are honestly currency-unknown. Mapping a substitute
  letter back to a glyph would be a guess about the recogniser, and was not done.
* **Period heads not recovered** (E, 603 incomplete): Wipro's balance sheet,
  SAP's balance sheet (dates only in a title line), HUL's June columns,
  Eternal's image tables, Bajaj's 71-column slides, SBI's presentation, and
  StanChart's "H1'25"/"Q1'26" heads, read without their apostrophes as
  "H125"/"Q126".
* **Rows** (L, 157): label lines run together are now refused rather than
  guessed (ICICI's other income and basic EPS, Groww's OCI tax, Eternal's image
  statement). Recovering them needs a row-segmentation fix that keeps each
  label line's position.
* **Vocabulary** (K, 391): company-specific lines (SAP's cloud segments, Tata
  Steel's exceptional items), working-capital movements, and OCI sub-lines.
* **Column geometry** (D, 98): ICICI's consolidated results and MapmyIndia's
  newspaper page.
* **Segments by column** are now unattributed, not modelled. A "Retail Banking"
  column's figures are no longer given to the group, but FinSight cannot yet say
  they are the segment's.
* **Figures without a period are not flagged for review.** This is a deliberate
  earlier policy (Phase 17): the period shows as unknown where it is true. On
  StanChart, 143 such facts sit at high recognition confidence, mostly in
  reported/adjusted reconciliation tables whose period is in the title.
* **The scorer** cannot score the measure (the golden rows have no taxonomy
  keys), and cannot key a label with a row number run in ("16Minority
  Interest"). Both are covered by the by-eye audit.
* RIL footnote-scoped measures (§4.1).

## 16. No company-specific code (section 38, criterion G)

Every rule is stated in terms of layout, text or accounting structure; none
names a company, a document or a page. The vocabulary added comes from the
prescribed formats (Schedule III, the SEBI bank format, IAS 1/7) or from lines
every filer in an industry prints. No segment or product name was added (SAP's
"Cloud" stays unrecognised). Each fix was re-measured on all twenty documents,
and a fix that helped one document while hurting another was revised before
the next run: the kind-head rule and Tata Steel, the overlap threshold and
Groww's banner, and the run-together-label rule and Eternal (§18).

## 17. Files changed

**Backend:**
* `normalization/numbers.py`, `normalization/periods.py`
* `tables/structure.py`, `tables/header_block.py`, `tables/fragment_identity.py`
* `extraction/facts.py`, `extraction/taxonomy.py`, `extraction/narrative.py`
* `spatial/extract.py`, `services/pipeline.py`
* `document/assembly.py`, `document/entities.py`
* `validation/engine.py`
* `evaluation/golden.py`, `evaluation/scoring.py` (new), `evaluation/corpus_runs.py` (new)
* `api/routes_data.py`
* Scripts `corpus_run.py`, `corpus_score.py`, `corpus_compare.py`,
  `corpus_corroborate.py` (all new)
* 16 new test files, one extended

**Frontend:** `types/index.ts`, `components/FigureDetail.tsx`.

**Docs:** `docs/EVALUATION.md`.

**Data:**
* `data/corpus/`: manifest, sources, runs, scores, dashboards
* 26 new golden tables
* 12 golden rows renamed to carry their parent (8 bank rows, 4 SAP rows),
  values untouched

## 18. How the end of the phase went

Honest accounting of the last cycles, because two of them were not clean:

* **`after11` was discarded.** `corpus_run.py` skips any document whose record
  already exists for the tag. I stopped two cycles mid-run to add fixes and
  relaunched under the same tag, so `after11` mixed records from three code
  versions. That showed up as a fix that did not take effect on ICICI. The
  records were deleted, and every later cycle used a fresh tag with all gates
  on one code state.
* **A rule I added was wrong and was reverted.** To fix ICICI's run-together
  label, `after13` took the line before a subtotal formula as the row. On
  Eternal's image statement the figures belonged to the *last* of the merged
  lines, so that rule filed OCI tax figures (34, −21) as profit: 19 wrong
  facts, outside any golden table. They were caught by reading the output, and
  `after14` refuses such labels outright.
* `after12` and `after13` are consistent intermediate states and are kept.
  `after14` is final.

## Appendix — reproducing

```
cd backend
python scripts/corpus_run.py --tag T          # process each document once (skips existing records for T)
python scripts/corpus_score.py --tag T --compare baseline
python scripts/corpus_compare.py --tag T      # 200 comparisons
python scripts/corpus_corroborate.py --tag T  # cross-source
python ../scratch/p16_benchmarks.py           # RIL x3, Apple
python -m pytest -q tests
```
