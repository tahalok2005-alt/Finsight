# Production readiness pass — engineering report

Every number below was measured on this machine during this pass. Where a
measurement is not trustworthy, it says so and is not used.

The evidence document throughout is **IDFC_Q12027.pdf** — 75 pages, a real Q1
FY27 investor presentation. It matters that it is a *presentation* and not a
statutory filing: its financial content sits in slide graphics and KPI tiles,
its tables are laid out by business segment, and its columns are headed with
month-year abbreviations. It answers "is this a name", "is this a period" and
"is this figure doubtful" badly, which is what made it useful.

---

## 1. Baseline

Taken before any change.

| | Result |
|---|---|
| Backend suite | **399 passed, 1 skipped** in 10 m 50 s |
| Frontend typecheck | clean |
| Frontend production build | succeeds |
| Frontend lint | **6 warnings, 0 errors** (all pre-existing) |

IDFC as it stood:

| | Baseline |
|---|---|
| Pages processed | 75 / 75 |
| Processing time | **830.5 s** (13 m 50 s), cold |
| Figures extracted | 38 |
| With a reporting period | 3 (8%) |
| With exact dates | 0 |
| Marked REQUIRES REVIEW | **35 (92%)** |
| Entities discovered | `Retail Deposits Retail Deposits 9%`, `Mortgages Vehicles MSME Consumer Loans Credit Cards Microfin`, `of Ethical, Digital & Social Good Banking`, … |

Stage split of the baseline run: page loop 564.5 s, charts 176.6 s,
extraction 87.5 s.

---

## 2. Root causes

### 2.1 The keyword soup — entity discovery

**Layer:** `app/document/assembly.py`, section-heading → entity attribution.

A heading became a business unit if it *mentioned* anything entity-like, and
that word list contains `retail`, `digital`, `services`, `business`. Those
words appear on nearly every slide of a bank's presentation, so chart legends
passed the gate. Figures were then attributed to units the document never
named, and the interface presented that soup to the reader as though the
source had said it.

**Why it was not caught before:** the word list was tuned on a filing whose
segments really are called Retail and Digital. On that document it is a good
rule. It had never met a document where those words are ambient.

**Fix:** the gate now also asks whether the candidate *reads as a name*. Four
properties of the text disqualify it, none specific to any filer: it carries a
digit or `%`; it repeats a content word; it begins with a connective; or it is
four or more capitalised words with nothing binding them together.

### 2.2 Month-year column heads were unreadable

**Layer:** `app/normalization/periods.py`.

`Jun-26`, `Mar-26`, `Dec-18` — the ordinary column head of an Asian or
European presentation — matched no pattern. Every date pattern required an
alphabetic month.

**Why it was not caught before:** the benchmark documents are Indian and US
filings that write `Quarter Ended 30th Jun'26` and `Three Months Ended June
28, 2025`. Neither uses the abbreviated form.

**Fix:** a month-year head resolves to that month's end. An existing test
caught a regression during the work — `"June 28,"` with its year on the next
header row was being read as June 2028 — so the rule requires an explicit
separator (`Jun-26`) or a four-digit year (`Jun 2026`). A bare `Jun 26` is
genuinely ambiguous and stays unresolved.

### 2.3 REQUIRES REVIEW at 92% — a missing period marked the value suspect

**Layer:** `app/validation/engine.py` → `app/services/pipeline.py`.

`check_period_coverage` raised a single `REQUIRES_REVIEW` validation and
attached all 35 undated facts to it. Any validation touching a fact raises its
review flag, so a missing *column header* marked 35 well-read *values* as
doubtful.

**Why it was not caught before:** on the benchmark filings almost every column
has a period, so the rule fired on a handful of narrative figures and the
conflation never showed.

**Fix:** period coverage is now an observation about the document, reported at
warning level and attached to no facts. Review flags carry a typed
`ReviewReason` and a sentence saying what to look at.

### 2.4 "Not applicable" and "not identified" were the same state

**Layer:** `app/extraction/facts.py`, `app/models/enums.py`, `utils/period.ts`.

A column that is a business segment never offered a period. A column that
looked like a date and would not resolve is a real gap. Both rendered as the
same warning, which makes an ordinary segment table look broken.

**Fix:** `PeriodBasis.NOT_APPLICABLE`, decided from the column head's own
shape. The interface reads "Not period-specific" in muted text for the first
and keeps the warning for the second.

### 2.5 Export was unreachable, not broken

**Layer:** `frontend/src/views/DocumentView.tsx`.

The backend always served all three formats correctly — verified: HTTP 200 for
CSV, XLSX and JSON before any change. The menu was a CSS `group-hover`
dropdown positioned `mt-1` below its button. Moving the pointer toward the menu
crossed a 4-pixel gap where neither element was hovered, so the menu closed
before it could be clicked.

**Fix:** a click menu that stays open, closes on Escape or outside click, and
fetches the file as a blob so a failure appears as a message rather than a
blank tab. The CSV also gained the four period fields, which the model had and
the export had never carried.

### 2.6 The Statements grid was 88% empty

**Layer:** `app/api/routes_intelligence.py` → `views/doc/SummaryTab.tsx`.

The matrix keys its columns by period. With almost no facts carrying one, the
income statement came out 13 rows × 2 columns with **3 of 26 cells filled**. A
reader cannot tell that from failed extraction.

**Fix:** the backend judges whether a comparative statement can honestly be
built; below that, the UI explains why and lists the figures with their
context.

### 2.7 Runs appeared to hang — worker count ignored memory

**Layer:** `app/config/settings.py`.

`resolved_page_workers` chose from CPU cores alone: two thirds of logical
cores, capped at eight. That says what a machine can compute and nothing about
what it can hold. On this 16 GB machine with **2.2 GB free** it still asked for
eight, and eight page bitmaps plus eight ONNX arenas do not fit in 2.2 GB. The
run did not fail — it thrashed, which is the worst way for this to go wrong
because nothing errors and the pipeline simply looks frozen.

**How it was found:** the new per-page instrumentation. Pages 37–44 — exactly
one batch of eight workers — each blocked for ~2 100 s against a 59 s median,
with the time inside OCR.

**Fix:** worker count is capped by free memory as well as cores, with a floor
of two and a fallback to the core-count answer when memory cannot be probed.
The chosen count and the memory reading behind it are logged, so a slow run is
explainable rather than mysterious.

### 2.8 Column headings that describe nothing — found in the browser

**Layer:** `app/extraction/facts.py`.

Reading the finished Financials screen as a user, figures like this appeared:

```
Total Assets    As at 30 June 2026    16.40%     HIGH 84
Total Assets    Not period-specific   ₹38,161m   HIGH 85
```

A balance-sheet total shown as a percentage, confidently dated. On that slide
the detector reconstructs the headings `Jun-25`, `Mar-26` and `Jun-26` at
column positions holding **zero cells**, while the figures sit three positions
to the right — so `Jun-26` lands above the year-on-year percentage column.

The misalignment is pre-existing. **The fix in §2.2 made this case worse**: it
turned "no period" into a *wrong* period, which is the one outcome the rules
forbid.

**Fix:** if a table has three or more dated columns and more than half of them
contain no data, the heading row does not describe the columns beneath it and
no period is taken from it at all. The figures keep their values and report no
period, which is visible and true.

---

## 3. Changes

| Area | Change |
|---|---|
| `document/assembly.py` | `_reads_as_a_name` — a name must look like a name |
| `normalization/periods.py` | `_MONTH_YEAR` / `_month_year_end`; separator-or-four-digit-year disambiguation |
| `models/enums.py` | `PeriodBasis.NOT_APPLICABLE`; `ReviewReason` + `REVIEW_REASON_TEXT` |
| `extraction/facts.py` | period-absence classification; `_header_row_is_misaligned` guard |
| `extraction/confidence.py` | `review_reason_for` — the value, not its metadata |
| `validation/engine.py` | period coverage is an observation, not a verdict on figures |
| `database/models.py` | `financial_facts.review_reason`; `pages.duration_ms` |
| `config/settings.py` | memory-aware `resolved_page_workers`; `_available_memory_mb`; `slow_page_ms` |
| `services/pipeline.py` | per-page timing, slow-page logging, concurrency logging |
| `api/routes_documents.py` | `GET /diagnostics`; `POST /bulk-delete` reusing the single-delete lifecycle |
| `api/routes_intelligence.py` | `_comparative_verdict` on each statement |
| `api/routes_data.py` | serves `review_reason` and its explanation |
| `export/exporters.py` | period fields, review reason, analyst-ordered `SPREADSHEET_COLUMNS` |
| `components/ExportMenu.tsx` | click menu replacing the unreachable hover dropdown |
| `views/DocumentsView.tsx` | multi-select, select-all-visible, shift-range, bulk delete |
| `views/doc/FactsTab.tsx` | review reason with its sentence; muted "Not period-specific" |
| `views/doc/SummaryTab.tsx` | statement fallback list instead of a grid of dashes |
| `utils/period.ts` | `not-applicable` certainty |

---

## 4. IDFC results

| | Before | After |
|---|---|---|
| Figures extracted | 38 | 38 |
| With a reporting period | 3 (8%) | 3 (8%) |
| With exact dates | 0 | 0 |
| Period **not applicable** | — (state did not exist) | **24** |
| Period genuinely unknown | — | 11 |
| Marked REQUIRES REVIEW | **35 (92%)** | **11 (29%)** |
| Review flags with a stated reason | 0 | **11 of 11** |
| Entities | keyword soup | `Wholesale`, `RETAIL BANKING`, `Retail Liabilities` |
| Statement reconstruction | 3 of 26 cells, rendered as a grid | refused, with the reason stated |

**The period count did not improve, and that is the correct outcome.** An
intermediate build reached 14 periods and 11 exact dates — and inspection in
the browser showed those dates were being taken from headings that sit above
empty columns (§2.8). They were wrong. With the alignment guard the figures
report no period rather than a fabricated one.

The review count is the real change: 92% → 29%, and every remaining flag names
its reason — 10 `ambiguous_period` ("the table sets out several reporting
periods and this value could not be safely assigned to one of them") and 1
`validation_failed`.

---

## 5. Performance

### Valid completed measurements

| Run | Configuration | Wall time |
|---|---|---|
| Baseline, cold | 8 workers, memory free at the time | **830.5 s** |
| After, cold, clean | 3 workers (memory-capped), nothing else running | **843.7 s** |

**No material change in wall time** — +1.6%, within run-to-run noise. The
memory cap does not cost throughput on this document.

What did change is stability. Per-page distribution on the clean run:

| | mean | median | p95 | max | pages over 45 s |
|---|---|---|---|---|---|
| Clean run, 3 workers | 23.1 s | 23.4 s | 36.6 s | 44.4 s | **0** |

Stage split: page loop 583.1 s, charts 167.0 s, extraction 91.7 s.

### Discarded measurement

A cold run timed **2 893.7 s** with a p95 page of 2 105 s. It is discarded: a
test suite was running concurrently at its start, so its slowness cannot be
attributed cleanly between memory pressure and that load. It is the run that
*revealed* the stall (§2.7) and is reported here for that reason only. **No
before/after improvement is claimed from it.**

### Not measured

The memory cap's benefit is not isolated. Its cost is measured (none, above);
its benefit is inferred from the stall signature — one batch of eight workers
blocking together with 2.2 GB free — rather than from a controlled comparison.

### Bottleneck

OCR dominates at roughly 69% of wall time. The second cost is chart detection
at 167 s (20%) for 205 detections on a 75-page deck; that has not been
investigated and is a candidate for the next pass.

---

## 6. Validation

| | Result |
|---|---|
| Backend suite | **452 passed, 1 skipped** in 12 m 51 s (baseline: 399 passed, 1 skipped) |
| New regression tests | **53** in `tests/test_production_readiness.py` |
| Frontend typecheck | clean |
| Frontend production build | succeeds (420.4 kB, 122.4 kB gzip) |
| Frontend lint | **6 warnings — identical to baseline, none new** |
| Bulk delete, end to end in the browser | 38 → 32 documents, exactly the 6 selected; zero orphaned facts, tables or pages |
| Export, end to end | CSV / XLSX / JSON all HTTP 200, named from the document, 38 analyst-facing columns |

Benchmarks re-run with the final extraction code:

| Document | F1 before | F1 after | Facts | Provenance |
|---|---|---|---|---|
| RIL media release | 0.9882 | **0.9882** | 432 / 431 | 100% |
| RIL image-only | 0.9877 | **0.9877** | 255 | 100% |
| RIL scanner-degraded | 0.695 | **0.695** | 251 | 100% |
| Apple FY25 Q3 | no answer key | no answer key | 56 → **52** | 100% |

Apple's four fewer facts are four *wrong* facts removed: `Total non-current
assets` (209 004 / 211 993) and `Total non-current liabilities` (124 545 /
131 638) were being reported under the `current_assets` and
`current_liabilities` metrics.

### Regressions I introduced and then fixed

Both were found by real documents, not by unit tests:

1. **The connective rule.** My repeated-word test counted connectives, so
   `OIL AND GAS (EXPLORATION AND PRODUCTION)` was rejected as a name and its
   figures fell onto the consolidated group, where they contradicted the
   consolidated reading of the same filing. Caught by
   `TestCrossDocumentComparison`.
2. **Wrong dates from misaligned headings** (§2.8). Caught by reading the
   finished screen in the browser. No test I had written would have found it.

---

## 7. Remaining limitations

This is not finished work, and these are the things a demonstration would run
into.

* **Only 38 figures come out of a 75-page deck.** 61 tables are detected and
  53 of them produce nothing. The financial content is largely in KPI tiles —
  `Profit After Tax` / `(Q1-FY27)` / `US$ 114 Mn.` laid out *across* columns —
  and the extractor reads metrics down a label column. Reading transposed and
  tile layouts is a genuine capability gap, not a bug, and it is the single
  largest thing standing between this and a usable reading of a presentation.
* **Table reconstruction on slide graphics is unreliable.** Grids of 16×44 are
  being built from infographics at 18% cell density. The alignment guard stops
  that producing *wrong* dates; it does not make the grids right.
* **Metric matching still over-reaches.** On this deck `Operating Expense` maps
  to `Revenue from Operations` and `Leverage ratio (%)` to `Net Debt to
  EBITDA`. The figures table now leads with the line item as printed, so a
  reader can always see what the document actually said, but the canonical
  mapping beside it is sometimes wrong.
* **Units and currency are wrong in places.** `Basic EPS` reads as
  `$1.00 million` — an EPS of about ₹1 typed as millions, and USD on an Indian
  bank whose deck quotes US$ conversions throughout.
* **Chart detection is unexamined.** 205 detections on 75 pages costing 167 s.
  Whether those are real charts has not been checked.
* **The memory cap is defensive, not tuned.** 400 MB per worker is a measured
  estimate with headroom, not a profiled figure.
* **`period_confidence` is persisted but not shown** anywhere in the figures
  table.

Nothing here is described as industry-grade. The trustworthiness problems in
the brief — invented context, invented periods, undifferentiated warnings, a
grid of dashes, an export that could not be clicked — are fixed and tested. The
extraction yield on presentation-style documents is not.
