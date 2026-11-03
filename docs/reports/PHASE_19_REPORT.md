# Phase 19 — Financial fact integrity

*Period-correct table extraction, source-grounded validation, and a golden
corpus read from the documents themselves.*

---

## 0. The numbers, in the terms the brief asks for

Not "how many facts", but how many are correctly attributable.

| | Before | After |
|---|---|---|
| Facts with a **wrong period** | unmeasurable — periods were absent, not wrong | **0** of 72 checked against source |
| Facts with a **wrong unit** | — | **0** |
| Facts with a **wrong entity** | — | **0** |
| **Correct-period accuracy** (golden corpus) | — | **100%** (72/72) |
| **Correct-unit accuracy** | — | **100%** (72/72) |
| **Correct-entity accuracy** | — | **100%** (72/72) |
| Golden facts extracted | — | 72 of 151 |
| Facts shown as HIGH with no period, unflagged | **260** (20% of 1,306) | **0** |
| Facts labelled "not period-specific" | 505 | **0** on periodized tables |
| **False conflicts** (Groww) | **89** | **0** |
| Facts requiring review (Groww) | 29 | 1 |
| Cross-foot checks passing (Groww) | 0 of 8 — could not run | **8 of 8** |
| Keyed accuracy | RIL 0.9882 · IMAGE_ONLY 0.9877 · DEGRADED 0.6950 | RIL 0.9882 · IMAGE_ONLY **1.0000** · DEGRADED **0.7092** |
| Backend tests | 644 | **778** |

Groww produces 114 facts where it produced 118. The four fewer are the point:
every one that remains carries a period, a scale, a currency and the right
entity, and eight independent arithmetic checks now confirm them.

Two defects found along the way were worse than the one reported: a period of
**2031** invented from concatenated column heads, and a number-parsing fault
that read `14272.21` as **142.0**. Both are described below; both are fixed;
both have tests.

---

## 1. The reported bug, reproduced exactly

Groww's unaudited consolidated results, page 6. The source prints:

```
                              (All amounts are in INR Crores, unless otherwise stated)
                                    Quarter ended                        Year ended
                          30 June      31 March      30 June      |    31 March
Particulars                  2026          2026         2025      |       2026
                        (Unaudited)    (Audited)    (Audited)     |    (Audited)
...
Current tax                  260.40       268.55       133.06     |      770.24
```

What FinSight produced:

| metric | value | period | period_basis | confidence | review |
|---|---|---|---|---|---|
| Current tax | 770.24 | `None` | `not_applicable` | 0.89 **HIGH** | no |
| Current tax | 268.55 | `None` | `not_applicable` | 0.89 **HIGH** | no |
| Current tax | 133.06 | `None` | `not_applicable` | 0.89 **HIGH** | no |
| Current tax | 260.40 | `None` | `not_applicable` | 0.89 **HIGH** | no |

Four figures from four different columns, presented as authoritative, with no
temporal context and no invitation to check.

**What it produces now:**

| metric | value | period | period end | unit | review |
|---|---|---|---|---|---|
| Current tax | 260.40 | **Q1 FY27** | 2026-06-30 | crore | no |
| Current tax | 268.55 | **Q4 FY26** | 2026-03-31 | crore | no |
| Current tax | 133.06 | **Q1 FY26** | 2025-06-30 | crore | no |
| Current tax | 770.24 | **FY26** | 2026-03-31 | crore | no |

Four distinct facts, four distinct periods, the quarter and the year that share
an end date kept apart. This is asserted by
`tests/test_table_period_integrity.py::TestTheReportedDefect`, built from the
source rather than from output.

### 1.1 A second defect found while reproducing: a fabricated period

The orphaned header fragment had resolved a period that appears nowhere in the
document:

```json
{ "label": "As at 2031-06-30",
  "raw_text": "(All amounts...) / Quarterended / 30 June 31 March 30 June",
  "end_date": "2031-06-30", "confidence": 0.75 }
```

**2031**, produced by concatenating three columns' header text and parsing the
result. A missing period is a gap; an invented one is a lie. Two independent
changes now prevent it: the header block never merges text across columns, and
the date parser refuses a bare two-digit year that is not introduced by an
apostrophe (`30 June 31` no longer resolves at all).

---

## 2. Root cause

Four links. Each is individually defensible; together they turned a lost header
into a confident claim.

**1. Layout splits one statement into several table regions.** Page 6's single
statement became four, cut at the horizontal rules between sections:

| Region | y range | x range |
|---|---|---|
| `region_c4507e8` | 385–484 | 183–1383 |
| `region_8c095f0` | 450–1042 | 181–1392 |
| `region_afd1a77` | 1009–1521 | 181–1391 |
| `region_96a4558` | 1485–1625 | 181–1395 |

**2. The table builder trusted the region boundary.** One table per region, so
the header block became a 4×3 table and the data became three headerless ones.

**3. The orphaned header's own columns were wrong.** With no numeric columns to
align to, column detection gave it a single band three columns wide; three
headers collapsed into `"30 June 31 March 30 June"`, and that produced 2031.

**4. An empty header was read as "this column has no period".**

```python
c.index: (PeriodBasis.UNKNOWN if looks_like_period_header(c.header or "")
          else PeriodBasis.NOT_APPLICABLE)
```

The UNKNOWN/NOT_APPLICABLE distinction is real and worth keeping — a segment
column genuinely has no period, and flagging it would turn every segment table
into a screen of warnings. But it was decided by *reading the header text*, and
an empty header is not a document saying "no period here". It is a header we
lost. `NOT_APPLICABLE` then meant no review flag, and confidence never required
a period, so the fact reached the user at 0.89 HIGH.

**In one sentence:** *absence of evidence was treated as evidence of absence,
at a point where the evidence had been destroyed two stages earlier.*

### 2.1 Scope before the fix

Across the seven corpus documents as they stood:

| | Count |
|---|---|
| Facts | 1,306 |
| No period | 588 (45%) |
| Labelled "not period-specific" | 505 |
| **…and HIGH confidence with no review flag** | **260 (20%)** |

One fact in five was presented as an authoritative figure with no temporal
context and no warning.

The full audit, written before any code changed, is
[docs/TABLE_PERIOD_INTEGRITY.md](../TABLE_PERIOD_INTEGRITY.md).

---

## 3. Table architecture changes

Columns are now first-class evidence, and a period travels
**table → header cell → column → cell → fact** rather than by proximity.

### 3.1 Fragments are rejoined before anything reads them
`backend/app/tables/fragments.py` (new)

Adjacent table regions whose horizontal extents agree to within a tenth,
separated by less than a few per cent of page height, with no heading or
paragraph between them, are offered to the table builder as one band. The test
is purely geometric and carries no notion of what any filer's tables look like.
Rejoining is a smaller and safer change than re-tuning layout, which would move
every document in the corpus; and the builder still applies its usual structural
tests and may reject the merged band.

### 3.2 The header block is a structure, not a string
`backend/app/tables/header_block.py` (new)

One `ColumnHeader` per data column, carrying its own head lines, its group
heads, its audit status and its note reference. Three distinctions, each
geometric:

* **A banner is not a column head.** Text crossing every data column describes
  the table. It is still consulted for the table's *unit*, because spanning the
  whole table is exactly what a table-wide declaration does — which is how
  "(All amounts are in INR Crores, unless otherwise stated)" is now read, a
  wording the bracketed unit pattern never matched.
* **A group head spans a set of columns and is placed by partition.** "Quarter
  ended" can sit wholly inside column 2 while governing columns 1–3, so
  containment cannot express it; group heads cut the table at their own left
  edges. A header row containing any spanning cell is a group row, and every
  cell on it is a group head however narrow — which keeps a "Year ended" over a
  single column distinct from the "Quarter ended" beside it.
* **A column head is assembled down its own column.** "30 June" and "2026" are
  separate elements on separate lines.

Two refusals were added from real documents:

* **A group head naming a specific date is a title, not a group.** A group head
  names the *kind* of period its columns are, never which one, because the
  columns differ. One bank's statement is headed "UNAUDITED STANDALONE
  FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026" across the whole
  table; read as a group it was prepended to every column and its date won over
  each column's own, so four columns covering three quarters and a full year
  all resolved to the first quarter.
* **A head naming two different dates cannot date its column.** A comparative
  column headed "Jun-25" had absorbed the sentence above the table and read
  "…discontinued in Dec'25 end Jun-25". The parser took the first date and
  dated the column six months wrong, at high confidence, unflagged. It now
  refuses and the figures are held for review.

### 3.3 The table's shape decides what it owes
`backend/app/tables/shape.py` (new)

`POINT_IN_TIME` / `PERIOD_FLOW` / `PERIODIZED_KPI` / `NON_PERIODIC` /
`UNCLASSIFIED`, decided from the resolved column periods first, then the head
text, then the title — column evidence outranking the title, because the title
describes the section and the columns describe the figures.

This is what makes "period missing" answerable. The same `period = None` means
opposite things in a statement and in a segment table, and nothing downstream
could previously tell them apart. `UNCLASSIFIED` is a real answer and is
treated as "not mandatory": demanding a period from a table we could not read
would flag every unusual layout, and a warning that fires everywhere is read
nowhere.

### 3.4 The reconstruction is checked against itself
`backend/app/tables/validate.py` (new)

Before facts are persisted: cells sitting outside the column they were filed
under, a dated column nearly empty beside full ones, two columns resolving to
the same period, a column holding figures with no readable head. A failure
means the reconstruction is internally inconsistent, so the column a figure
came from — and therefore its period — is not established, and its facts are
held for review rather than published as settled.

Cross-footing is deliberately **not** checked here. Component rows routinely
exclude items the total includes, so an arithmetic check against the visible
rows fires on perfectly good tables. That belongs to the validation engine,
which has the taxonomy to know which rows are components of which total — and
section 9 shows it now working.

---

## 4. Period model changes

`backend/app/normalization/periods.py`

Five changes, each from a document that got a wrong answer.

| Change | Why |
|---|---|
| `quarter\s*ended` — the space before "ended" is optional | OCR closes the gap in a tightly set head. One statement is headed "Quarterended" and "Yearended"; requiring the space left every column of that filing dated but with no span, so four quarterly figures were typed as instants |
| A two-digit year must be introduced by an apostrophe | `30 June 31` resolved to 30 June **2031**. Every dated head in the corpus prints a four-digit year or an apostrophe, so nothing real is lost |
| The day may not be taken from the year | `Mar 2026` was read as the **20th** of March in '26 — the day group ate the first half of the year and the year group took what was left |
| Numeric dates resolve through `parse_period`, not only through `parse_period_levels` | The same head meant a period as a list of header lines and nothing as one string. A bank's dated columns all fell back to the title's period |
| A comma is a date separator alongside the stop | OCR reads `31.03.2026` as `31,03.2026` constantly in small type. Three groups with a two-or-four digit tail is not a figure in any convention, and the day/month validity check rejects anything that is not a real date |

**A period is now mandatory where the source provides one.** A fact from a
periodized table with no period is `UNKNOWN` + `REVIEW_REQUIRED`, held below
the review threshold, never `not_applicable`, and never HIGH.

---

## 5. Column geometry changes

* **Column membership is by x-geometry, never by order.** Groww's own statement
  proves why: three rows have nil cells printed as a dash that OCR drops, so
  "Remeasurement gains on investment" arrives as two values that belong to
  columns 2 and 4. Assigning by order would put 11.31 in column 1 — a full year
  wrong. The golden fixture records those rows for exactly this reason.
* **The header block's extent is found, not assumed.** `mark_header_rows`
  stopped after four rows and at the first row carrying numbers; Groww's head is
  six rows and its fourth is nothing but years. The block now ends at the first
  row containing a *measurement* — excluding bare years, all-digit dates, and
  any row carrying the word that names the label column.
* **A serial column is recognised by what follows it.** Requiring most of its
  rows to be numbered missed a bank's results, where only top-level lines carry
  a number: the ratio came to 0.46, too high to read as a label column and too
  low to pass a 0.7 threshold, so the label column beside it went unread and the
  page produced 8 facts instead of 28. The signature is a first column of small
  integers with a text column beside it — both halves needed, since the size
  test alone would swallow a leading column of genuine small figures.

---

## 6. Unit and currency changes

* **A banner is a unit declaration.** Spanning the whole table is what a
  table-wide declaration does. "(All amounts are in INR Crores, unless
  otherwise stated)" opens with three words of prose before the currency, so the
  bracketed pattern never matched it and Groww's entire statement came through
  with no scale.
* **Row evidence still overrides the table declaration.** The same statement
  declares crore and prints earnings per share under "Earnings per share in INR
  (Face Value : INR 2/- per share)". Basic EPS is `per_share` with no magnitude
  — not ₹1.19 crore. Asserted in
  `TestUnitScopeVsRowOverride::test_earnings_per_share_is_not_in_crore`.
* **A per-share figure is not missing a scale.** The safety gate exempts
  per-share, percentage, ratio, bps and count from the magnitude requirement,
  and percentage/ratio/bps/count from the currency requirement.

### 6.1 A number-parsing defect the corpus exposed

Not a period problem, and the most serious single finding of the phase.

`NUMBER_RE`'s separated form used `*` rather than `+`, so it also matched the
leading three digits of an unseparated figure — and alternation is ordered, so
it won:

| printed | parsed as | error |
|---|---|---|
| `14272.21` | **142.0** | ÷100 |
| `79362.78` | **793.0** | ÷100 |
| `307522.08` | **307.0** | ÷1000 |
| `92184.38` | **921.0** | ÷100 |

Silent, plausible, and wrong by two to three orders of magnitude. Only filings
that print figures without thousands separators were affected, which is why it
survived: every earlier benchmark document prints "1,501.42", and the first
statement to print "14272.21" entered the corpus in this phase. Fixed, with 28
cases in `tests/test_number_parsing.py` covering both directions.

A second, smaller repair: `1.053.69` — OCR reading a comma as a stop — used to
parse to **1.053**, truncated at the second stop. Two or more stops in one token
is not a number in any convention, and every group after the first being exactly
three digits makes the correction determinate. The repair deliberately requires
*two* stops: firing on one would rewrite every genuine three-decimal figure by a
factor of a thousand, which is the same error in the other direction and on far
more figures.

---

## 7. Entity and basis changes

Two changes, both from Groww's filing, which carries a consolidated statement
and a standalone one in the same document.

**A basis declaration is found by position, not by region count.** The rank
limit made the reach of the rule depend on how much letterhead a filer prints:
Groww opens each results page with a logo, its registered name, a former name,
a postal address, a CIN and a contact line — six regions — before "Statement of
the unaudited standalone financial results". None of it repeats closely enough
for chrome detection to match, because OCR renders the former name differently
on each page. The declaration is at 16% and 20% of page height on the two pages
concerned, while sentences mentioning "consolidated" in passing are below 60%,
so depth is the direct measure and region count was a poor proxy for it.

**A heading naming the filer does not override a basis declared on the same
page.** "Billionbrains Garage Ventures Limited" correctly resolves to the
consolidated group — it *is* the filer — and it appears in the letterhead of
every page, so it overrode the standalone declaration below it. The two are not
competing claims: one says who is reporting, the other on what basis, and the
second refines the first.

**Effect on Groww:**

| | before | after |
|---|---|---|
| Page 6 (consolidated statement) | `Consolidated` 58 | `Consolidated` 56 |
| Page 10 (standalone statement) | **`Consolidated` 57** | **`Standalone` 55** |
| Facts flagged `conflicting_sources` | **89** | **0** |
| Facts requiring review | 99 of 118 | **1 of 114** |

The 89 conflicts were the reconciler doing its job on a wrong entity: it
compared standalone revenue with consolidated revenue and correctly concluded
they disagreed, about figures that were never comparable. Standalone and
consolidated are now separate scopes and are never reconciled against each
other (brief §17).

---

## 8. Safety-gate changes

`backend/app/extraction/safety_gate.py` (new)

One place decides whether a fact is fit to be shown as a finished answer. It
does not delete facts or alter values; it decides whether a fact may present
itself as confident, and what a reader is told is missing.

Checked, in the order a reader needs them:

1. **Period**, when the table's shape says the columns are periods
2. **Column established** — the reconstruction validated, and headings matched
3. **Value type**, then **unit** and **currency**, each only where the type
   requires them
4. **Provenance** — page, and a recorded region

A fact that fails is kept, marked `REVIEW_REQUIRED`, given a plain-English
reason, and **capped just below the review threshold**. Confidence is capped
rather than rewritten: whatever the scorer concluded from the evidence stands,
but a fact that failed the gate may not read as settled. The pre-gate value is
kept in `meta.confidence_before_gate`, so nothing is lost.

The messages are sentences, not enum names:

> "This figure comes from a statement whose columns are reporting periods, but
> its own period could not be established. The value is shown without one rather
> than guessed."

A test asserts every finding carries a sentence over forty characters that ends
in a full stop and contains no `None`.

---

## 9. Extraction-quality changes

### 9.1 Arithmetic now confirms the extraction independently

Groww's cross-footing check — Revenue from Operations + Other Income = Total
Income — could not run before, because the facts had no periods to group by.
It now runs and **passes for all four periods of both entities, eight of
eight**:

```
PASSED  ... holds for ent_1ab80e826c01 Q1 FY27   (consolidated)
PASSED  ... holds for ent_1ab80e826c01 Q4 FY26
PASSED  ... holds for ent_1ab80e826c01 Q1 FY26
PASSED  ... holds for ent_1ab80e826c01 FY26
PASSED  ... holds for ent_06c13e13a6cb Q1 FY27   (standalone)
PASSED  ... holds for ent_06c13e13a6cb Q4 FY26
PASSED  ... holds for ent_06c13e13a6cb Q1 FY26
PASSED  ... holds for ent_06c13e13a6cb FY26
```

This is the strongest evidence in the phase that the attribution is right: the
figures were extracted independently of one another and they add up, per
period, per entity. It is also how the number-parsing defect in section 6.1 was
found — three of these failed until it was fixed.

### 9.2 Groww, before and after

| | before | after |
|---|---|---|
| Facts | 118 | 114 |
| Facts from periodized tables with no period | **118** | **0** |
| Labelled "not period-specific" | 89 | **0** |
| HIGH confidence, no period, unflagged | **77** | **0** |
| `conflicting_sources` | 89 | **0** |
| Requiring review | 29 | **1** |
| Cross-foot checks passing | 0 of 8 (could not run) | **8 of 8** |

Four fewer facts, and every one that remains carries a period, a scale, a
currency and the right entity.

### 9.3 Paytm

| | after |
|---|---|
| Facts | 26 |
| Period coverage on periodized tables | **100%** |
| Labelled "not period-specific" | 0 |
| HIGH confidence, no period, unflagged | 0 |
| Requiring review | 2 (was 13) |

The two review flags are the comparative column described in section 3.2 —
correctly refused rather than dated six months wrong.

### 9.4 HDFC

Page 1 went from 8 facts to 28, and its four columns now resolve to
Q1 FY27 / Q4 FY26 / Q1 FY26 / FY26 — matching the golden fixture, including the
column whose head OCR mangled to `31,03.2026`.

---

## 10. Golden corpus

`data/golden/*.json`, loaded by `backend/app/evaluation/golden.py`.

Golden facts are transcribed **from the rendered page by eye**, never from
FinSight's output. Each file records the table's title, its reporting basis and
the evidence for it, its unit declaration and where that declaration sits, every
column with its group, its head lines, its resolved period, its audit status and
its note reference, and every row's values aligned to those columns — with a
printed dash recorded as `"nil"` and a blank cell as `null`, because neither is
a fact and a system producing `0.0` for them would be inventing a figure.

| File | Document | Page | Golden facts | Why it is in the corpus |
|---|---|---|---|---|
| `GROWW_p6_consolidated_pl.json` | Groww consolidated P&L | 6 | 99 | The reported defect. Written-out dates, a full-width unit banner, a four-level head, nil cells, per-share rows |
| `HDFC_p1_standalone_results.json` | HDFC Bank standalone results | 1 | 52 | Every dimension different: dotted numeric dates, basis in the title, unit in a corner note, a serial column, a note reference on its own head line, figures printed without separators |

Two files, **151 golden facts**. They were chosen to disagree with each other:
a rule that works on Groww and fails on HDFC is a rule fitted to one filer, and
three of this phase's defects were found precisely because HDFC broke a rule
that Groww had confirmed.

Each file also carries a `must_never` list, in plain words:

```
"any fact from this page attributed to the consolidated entity -- the title says standalone",
"any fact from this table with period_basis = not_applicable",
"any fact from this table with no period and HIGH confidence",
"a period outside 2025-04-01 .. 2026-06-30",
"the quarter column and the year column resolving to the same period, though they share an end date"
```

### 10.1 Results

| | GROWW p6 | HDFC p1 | Total |
|---|---|---|---|
| Golden facts | 99 | 52 | 151 |
| Matched | 56 | 16 | **72** |
| **Correct period** | 56/56 | 16/16 | **72/72 — 100%** |
| **Correct unit** | 56/56 | 16/16 | **72/72 — 100%** |
| **Correct entity** | 56/56 | 16/16 | **72/72 — 100%** |
| Wrong period | 0 | 0 | **0** |
| Wrong unit | 0 | 0 | **0** |
| Wrong entity | 0 | 0 | **0** |
| Not extracted | 43 | 36 | 79 |

**Every fact the system extracted from these two statements is correctly
attributed.** The 79 not extracted are a coverage gap, not an attribution
error, and section 13 says what they are.

### 10.2 What is not extracted, and why

The 79 unextracted golden facts are not attribution errors — no fact was
produced for them at all, which is the designed behaviour for a row whose label
the taxonomy does not recognise. Two distinct causes, and they need different
fixes:

**Taxonomy coverage (Groww 43, HDFC ~24).** Real line items with no entry in
the ~78-metric taxonomy:

| Row | Matches? |
|---|---|
| `Profit before tax for the period/year` | **no** |
| `Total tax expense for the period/year` | **no** |
| `Interest earned (a)+(b)+(c)+(d)` | **no** |
| `Operating expenses (i)+(ii)` | **no** |
| `Total Expenditure (4)+(5) (excluding provisions and contingencies)` | **no** |
| `Share of net loss of associate accounted for using equity method` | no |
| `Remeasurement gains/(losses) on defined employee benefit plans` | no |
| `Foreign currency translation reserve` | no |

The first two are striking: profit before tax and total tax expense are core
metrics, defeated by the trailing "for the period/year". That is an alias gap,
not a conceptual one, and it is the cheapest quality win available.

**OCR corruption of the row label (HDFC ~12).** These match when spelled as the
page prints them and fail as OCR read them:

| As printed | As OCR read | Printed matches | OCR matches |
|---|---|---|---|
| `i) Employees cost` | `i)Empioyees cost` | `employee_cost` | **no** |
| `Operating Profit before provisions and contingencies (3)-(6)` | `Operating Prafit befare provisians…` | `pre_provision_operating_profit` | **no** |

Nothing about the period model can recover these; they are recognition
failures on a small-type scanned page.

---

## 11. UI changes

**A misleading sentence is now unreachable, not merely unlikely.**
`describePeriod` will not say "Not period-specific" for a fact whose
`meta.table_shape` says its table is organised by period, whatever basis the
backend sends. The backend now sends the right basis; this makes the wrong
sentence unreachable from the UI even if it stops doing so. A periodized fact
with no period reads:

> **Period not identified** · needs review
>
> *This figure comes from a statement whose columns are reporting periods, but
> its own column could not be matched to one. The period has been left blank
> rather than guessed.*

**The safety gate's own words reach the reader.** Where the gate recorded a
message, the figure detail panel shows that sentence rather than an enum name
like `ambiguous_period`.

**Source-to-fact inspection (§22).** The figure panel now shows the chain that
answers *why does FinSight believe 260.40 is the quarter ended 30 June 2026?*:

```
From the column headed   "30 June 2026 (Unaudited)"
                         statement table · row 21, column 2 · page 6
```

Table, row, column, and the heading the period came from — checkable against
the page, rather than a resolved period asking to be believed.

---

## 12. Files changed

| File | Change |
|---|---|
| `backend/app/tables/fragments.py` | **new** — rejoin a statement layout split into several regions |
| `backend/app/tables/header_block.py` | **new** — the header block as per-column structure |
| `backend/app/tables/shape.py` | **new** — what kind of table this is, and whether a period is mandatory |
| `backend/app/tables/validate.py` | **new** — check the reconstruction against itself |
| `backend/app/extraction/safety_gate.py` | **new** — the last check before a fact looks authoritative |
| `backend/app/evaluation/golden.py` | **new** — golden-fact loading and scoring |
| `backend/app/tables/structure.py` | header block wired in; banners excluded from column heads and used for the unit; span patterns tolerate a lost space; multi-date heads refused; duplicate head parts collapsed |
| `backend/app/normalization/periods.py` | `_NUMBER`-adjacent date fixes: optional space before "ended", apostrophe required for a two-digit year, day may not come from the year, numeric dates reachable through `parse_period`, comma accepted as a date separator |
| `backend/app/normalization/numbers.py` | `_NUMBER_CORE` requires a real separator; `repair_misread_separators` |
| `backend/app/extraction/facts.py` | empty header → UNKNOWN not NOT_APPLICABLE; serial-column detection; table shape + validation + safety gate applied |
| `backend/app/document/assembly.py` | basis found by page depth; a filer-name heading does not override a basis declaration |
| `backend/app/services/pipeline.py` | table regions grouped and merged, with line de-duplication |
| `backend/app/models/schemas.py` | `ExtractedTable.meta` |
| `frontend/src/utils/period.ts` | "Not period-specific" unreachable for periodized facts; gate message surfaced |
| `frontend/src/components/FigureDetail.tsx` | source-to-fact chain; gate message in the review row |
| `data/golden/*.json` | **new** — 151 golden facts from two source documents |
| `docs/TABLE_PERIOD_INTEGRITY.md` | **new** — the audit written before the rebuild |
| `docs/ARCHITECTURE.md` | table reconstruction, header block, shape and validation documented |

**Tests added — 134:**

| File | Cases |
|---|---|
| `tests/test_table_period_integrity.py` | 18 — the Groww table, header block, unit scope, table shape, golden file |
| `tests/test_period_regression.py` | 62 — explicit dates, spans, point-in-time, fiscal labels, abbreviations, audit status, comparatives, and what must not resolve |
| `tests/test_anti_fabrication.py` | 26 — corrupted dates, misread numbers, ambiguous headers, the safety gate, table validation |
| `tests/test_number_parsing.py` | 28 — separated, unseparated, misread-separator and accounting forms |

---

## 12a. Corpus tested

| | |
|---|---|
| Companies | 6 — Groww, HDFC Bank, Paytm, Reliance, IDFC First Bank, Apple |
| Documents | 7 (four keyed, three unkeyed) |
| Pages processed in this phase | 76 across the three Indian filings, plus the four keyed benchmark documents |
| Golden tables transcribed from source | 2 |
| **Golden facts** | **151** |
| Statement kinds covered | consolidated P&L, standalone results, segment tables, KPI slides, balance-sheet extracts |

---

## 12b. Test results

```
778 passed, 1 skipped, 0 failed
```

Up from 644 entering the phase — **134 tests added, no regressions**. The skip
is `test_api.py:248` ("nothing needed review on this document"), a conditional
case.

**Keyed accuracy gate — no regressions, and two documents improved:**

| Case | Facts | F1 | Baseline | Matched | Baseline | Verdict |
|---|---|---|---|---|---|---|
| RIL | 415 | 0.9882 | 0.9882 | 84 | 84 | ok |
| RIL_IMAGE_ONLY | 242 | **1.0000** | 0.9877 | **81** | 80 | **improved (+0.0123)** |
| RIL_DEGRADED | 249 | **0.7092** | 0.6950 | **50** | 49 | **improved (+0.0142)** |
| APPLE | 44 | — | — | — | — | ok |

The image-only benchmark is now perfect — 81 of 81. The scanner-degraded
document moved for the first time in three phases; Phase 18 recorded it as
having "not moved for three phases", and the number-parsing and date fixes
account for the two additional facts it now matches.

This is the result that matters most: the correctness work did not cost
accuracy anywhere, and improved it on the two documents most sensitive to
recognition errors.

---

## 12c. Frontend results

| Check | Result |
|---|---|
| `tsc --noEmit` | 0 errors |
| `npm run build` | success |
| `npm run lint` (oxlint) | 6 warnings — unchanged baseline, none in changed files |

**Verified in the browser against the real Groww document**, not a fixture. The
Financials tab shows:

```
Current tax     Three months ended 30 June 2026        ₹260.4 crore   HIGH
Consolidated    Q1 FY27 · inherited from the surrounding structure    p6

Current tax     Three months ended 31 March 2026       ₹268.6 crore   HIGH
Consolidated    Q4 FY26 · stated in the document                      p6

Current tax     Three months ended 30 June 2025        ₹133.1 crore   HIGH
Consolidated    Q1 FY26 · inherited from the surrounding structure    p6

Current tax     Twelve months ended 31 March 2026      ₹770.2 crore   HIGH
Consolidated    FY26 · stated in the document                         p6
```

The quarter and the year that share an end date read as "Three months ended
31 March 2026" and "Twelve months ended 31 March 2026" — the case a reader is
most likely to conflate, and the one the old output could not express at all.

The document overview now reads **"3 carry no reporting period"** where it read
118, and **1 figure needs confirming** where it was 29.

The figure detail panel, opened on the first of those rows:

```
PERIOD                  Three months ended 30 June 2026
PERIOD END              2026-06-30
PERIOD BASIS            inherited
FROM THE COLUMN HEADED  "30 June 2026 (Unaudited)"
                        statement table · row 20, column 2 · page 6
ENTITY                  Consolidated
CONFIDENCE              HIGH · High confidence
REVIEW                  not required
```

HDFC's page 1 facts carry `Standalone` and `crore`, from the basis declared in
the statement title.

---

## 12d. Performance impact

The three new per-table stages, timed over Groww's 14 tables and 114 facts,
averaged over 20 repetitions:

| Stage | Cost per document |
|---|---|
| `classify_table` | 0.37 ms |
| `validate_table` | 0.99 ms |
| `safety_gate` (114 facts) | 0.23 ms |
| **Total** | **1.59 ms** |

Table reconstruction itself is 0.07 s (Groww) and 0.12 s (HDFC). Against warm
wall-clock runs of 17.4 s and 23.4 s, and cold runs an order of magnitude
longer, the correctness machinery added in this phase is not measurable.

The brief's rule — *a fast wrong answer is worse than a slow correct answer* —
did not have to be paid for here.

---

## 13. Known limitations

1. **Taxonomy coverage is now the binding constraint on quality**, not
   attribution. 79 of 151 golden facts are not extracted, and the two most
   valuable — profit before tax, total tax expense — fail on a trailing
   "for the period/year". See section 10.2.
2. **OCR corruption of a row label cannot be recovered.** `i)Empioyees cost`
   and `Operating Prafit befare provisians` match the taxonomy when spelled as
   printed and fail as read. This is a recognition limit on small-type scans.
3. **`Other equity` reads 8.403.55 where the page prints 8,403.55.** The
   separator repair recovers the value; the underlying misrecognition remains,
   and is recorded in the golden file as `known_ocr_error`.
4. **Only two golden tables exist.** They were chosen to disagree with each
   other, and that paid off — three defects came from HDFC breaking a rule
   Groww had confirmed — but two documents is a small sample for a corpus meant
   to prove generalisation. Section 14 says where to take it next.
5. **Paytm's slide tables are mostly `UNCLASSIFIED`**, so a period is not
   mandatory for them. That is the conservative reading, but it means the
   safety gate is not protecting those facts; the ones it does cover are 10 of
   26.
6. **Layout still splits statements**, and the fix is a repair downstream
   rather than a correction at source. It is measurably safe on this corpus,
   but a document whose two adjacent tables genuinely are separate — same
   width, no heading between them — would be merged wrongly. No such case
   appears in the corpus; the risk is real and unmeasured.
7. **The reconciliation layer was not revisited.** Groww's 89 false conflicts
   disappeared because the entity was fixed, not because the reconciler
   changed.

---

## 14. Recommended next phase

**First — taxonomy coverage, measured against the golden corpus.** This is now
the largest quality gap and the cheapest to close. The corpus already names the
missing rows, and the two most valuable are alias failures rather than
conceptual ones. Expanding the taxonomy is only safe with the attribution
guarantees this phase established: more facts are worth having *now* in a way
they were not before, because a new fact will carry a correct period or none.

**Second — grow the golden corpus to the shapes in brief §16.** Two tables
cover shapes A, E and I. Missing: half-year (`H1 FY27 | H1 FY26`), nine-month
(`Nine months ended 31 Dec`), a pure KPI table (`Jun-26 | Mar-26 | Dec-25`),
and a cash flow statement. Each is a real filing away, and each has a decent
chance of exposing something, on the evidence of how much HDFC exposed.

**Third — row-label OCR.** Every remaining HDFC miss is a corrupted label. A
fuzzy match against the taxonomy, gated so that a low-confidence match produces
a review flag rather than a silent attribution, would recover them; the guard
matters more than the matcher.

**Explicitly not recommended: chasing fact counts.** Groww produces 114 facts
where it produced 118, and the 114 are worth more. The metric to move is the
share of extracted facts that are correctly attributed — currently 100% on the
golden corpus — and the number of golden facts extracted, currently 72 of 151.

---

## Appendix — how to reproduce

```bash
# the reported bug, and everything guarding it
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_table_period_integrity.py -q

# period regression suite
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_period_regression.py -q

# deliberately ambiguous inputs, and the refusals they must produce
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_anti_fabrication.py -q

# the golden corpus: attribution accuracy against facts read from the sources
cd backend && ../.venv/Scripts/python.exe ../scratch/p19_golden_run.py

# quality scorecard
cd backend && ../.venv/Scripts/python.exe ../scratch/p19_corpus_scorecard.py

# keyed accuracy gate
cd backend && ../.venv/Scripts/python.exe ../scratch/p16_benchmarks.py

# full suite and frontend
cd backend && ../.venv/Scripts/python.exe -m pytest tests/ -q
cd frontend && npx tsc --noEmit && npm run build && npm run lint
```
