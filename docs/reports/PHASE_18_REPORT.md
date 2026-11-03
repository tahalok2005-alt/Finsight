# Phase 18 — Production speed, multi-document generalization, real-world benchmarking

*Visual-first financial document intelligence. CPU only, no external LLM API.*

---

## 0. How to read this report

Two rules governed what went into it.

**Every number here was measured on this machine, and the machine drifts.** The
same chart stage, on identical code, measured 34.8s, 38.9s, 43.2s and 49.9s
across four consecutive runs — a 43% spread from background load alone. Any
before/after taken from two separate runs would mostly be measuring that. So
every speed claim below either comes from an A/B that ran both versions in one
process, alternating, or is reported with the spread attached.

**The phase's largest piece of work produced a negative result.** The speed
investigation measured every lever that could reduce OCR cost and rejected all
of them. That is sections 7 and 8, and it is stated as plainly there as here.
The gains this phase actually shipped are correctness gains.

---

## 1. Phase 17 carry-over status

Carry-over was time-boxed as instructed. Rather than reprocess the 165-page
Standard Chartered document for another confidence number, the reconciliation
layer was re-run over the already-stored facts — a replay, in minutes instead of
22.

| | before | after |
|---|---|---|
| `genuine_conflict` on Standard Chartered | 45 | **28** |
| separated as scale disagreements | — | **4** |

The 17 that stopped being conflicts were not silenced. They were reclassified,
and the reason is a defect the replay exposed: when the same printed number
appears under two different unit declarations — "2,490" as crore on one page and
as millions on another — the reconciler was calling it a contradiction. It is
the same fact printed at two scales. `classify()` now returns `different_unit`
for that case, and the fact carries `meta["scale_disagreement"]`, which surfaces
in review as `UNIT_UNDETERMINED` rather than as a conflict.

**Not done, and deliberately:** the full Standard Chartered reprocess. The
replay validated the reconciliation change against real stored facts; a fresh
22-minute run would have re-measured OCR, which nothing in Phase 18 changed for
that document.

---

## 2. What was changed

Six changes. Four are correctness, one is speed, one is honesty in the UI.

### 2.1 Entity attribution — a heading must *declare* a unit (correctness)

The old test asked whether an entity-ish word appeared anywhere in a heading. In
a results deck almost every heading mentions retail, digital, services or
business, and three documents the system had never seen showed what that costs:

- a marketing line, "Quality Financial Services", governed **23 of one
  release's 25 figures**
- a slide heading, "Paytm for Business App", became a segment
- a wheel-diagram spoke, "RETAIL BANKING", owned two dozen figures of a bank
  whose deck never reports that unit separately

One shared cause, fixed once, in `_declares_a_reporting_unit`: a heading now
needs either a legal suffix anywhere in it, or a head noun at the end saying it
*is* a unit ("… Division", "… Segment", "… Business"). Position is what
separates the two uses of the same word — "JIOSTAR BUSINESS" is a reporting unit
and "Paytm for Business App" is a product.

This is a deliberate narrowing, and it costs something: "Digital Services" no
longer resolves, though it does name a real segment of at least one filer. That
trade is recorded in `test_a_bare_descriptor_no_longer_resolves` with the
evidence for it, because a bare two-word service phrase is the same shape as
every marketing line in every deck.

### 2.2 Reporting basis — read the top of the page, not only the chrome (correctness)

A statutory filing often declares its basis in ordinary body text at the top of
the page rather than in a running header — "Standalone Segment information in
accordance with the RBI guidelines and Accounting Standard 17". Where the header
band was OCR noise ("HHDFC BAN", "40C 063"), that line was the only place the
page said which basis it was on.

Reading only the chrome put one bank's standalone and consolidated results under
a single entity, and the evidence layer then reported **twelve contradictions
between figures that were never comparable**. `_basis_by_page` now sorts regions
by y and reads the top three non-chrome regions. Further down a page,
"consolidated" is a word in a sentence rather than a heading over the figures,
so the rank limit is doing real work.

### 2.3 A heading fragment is not a name (correctness)

The line grouper cut one filer's own legal name in half. "Billionbrains Garage
Ventures Limited (Formerly known as Billionbrains Garage Ventures Private
Limited)" became the tail alone — carrying a closing bracket it never opened —
and that fragment was attributed **58 of that document's 116 figures** as a
subsidiary. An unbalanced bracket now disqualifies a heading.

### 2.4 Same number, two scales, is not a conflict (correctness)

See section 1.

### 2.5 The radial test works at a bounded size (speed)

Chart detection's dominant cost was a full-resolution Hough circle search: 4.7s
of the 5.6s spent on one deck's six chart regions. A page-sized region at 200dpi
is ~1650x2340px, and the sweep ran over radii from 300 to 860px — an enormous
accumulator for a yes/no question about one big circle.

Whether a region contains a pie is a question about shape, and every parameter
is a fraction of the region, so the answer does not depend on the pixel count.
The search now runs on a copy bounded at 800px. Measured in section 8.

### 2.6 An honest processing view (UI)

Two measured dishonesties, described in section 12.

---

## 3. Files and modules changed

| File | Change |
|---|---|
| `backend/app/document/assembly.py` | `_declares_a_reporting_unit`, `_CORPORATE_SUFFIX`, `_STRUCTURAL_HEAD`, `_TRAILING_ABBREV`; unbalanced-bracket rejection; long-heading basis path; `_BASIS_DECLARATION_RANK` and y-ordered basis reading |
| `backend/app/charts/detector.py` | `_radial_score` + `RADIAL_WORKING_SIZE` |
| `backend/app/services/page_routing.py` | **new** — `PagePlan`, `plan_page` |
| `backend/app/services/pipeline.py` | routing gate; per-part timing for the chart stage |
| `backend/app/analysis/reconcile.py` | same printed number under different units → `different_unit`, not `genuine_conflict` |
| `backend/app/extraction/confidence.py` | `UNIT_UNDETERMINED` review reason for scale disagreement |
| `backend/app/evaluation/corpus.py` | **new** — corpus loader, reads Desktop documents in place |
| `data/benchmark/corpus.json` | **new** — 9-document corpus definition |
| `frontend/src/views/doc/ProcessingPanel.tsx` | page-phase grouping; estimate withdrawn after the last page |
| `backend/tests/test_entity_headings.py` | **new** — 17 cases |
| `backend/tests/test_page_routing.py` | **new** — 12 cases |
| `backend/tests/test_chart_radial.py` | **new** — 13 cases |

---

## 4. New benchmark documents

Three real earnings documents were found on the Desktop and added to the corpus.
**None were copied or modified**; `corpus.json` records absolute paths and the
loader reads them in place.

| Key | Document | Pages | Character |
|---|---|---|---|
| `HDFC` | HDFC results.pdf | 16 | Statutory bank filing; standalone *and* consolidated in one document; OCR-degraded header band |
| `PAYTM` | Paytm results.pdf | 18 | Earnings deck; KPI tiles, charts, marketing headings, unit declaration in a footnote |
| `GROWW` | groww Results.pdf | 14 | Statutory filing; long legal entity name; dense tables |

They were chosen because they break the system in three *different* ways, which
is what made the single shared cause in section 2.1 visible.

Corpus total: 9 documents — 4 with keyed ground truth (RIL, RIL_IMAGE_ONLY,
RIL_DEGRADED, APPLE), 5 without (IDFC, STANCHART, HDFC, PAYTM, GROWW).

---

## 5. Benchmark table

### 5.1 Keyed accuracy — the regression gate

These four have ground truth. They are the gate: a change that moves them is a
regression until proven otherwise.

| Case | Facts | F1 | Baseline F1 | Matched | Verdict |
|---|---|---|---|---|---|
| RIL | 418 | 0.9882 | 0.9882 | 84/84 | ok |
| RIL_IMAGE_ONLY | 243 | 0.9877 | 0.9877 | 80/80 | ok |
| RIL_DEGRADED | 249 | 0.6950 | 0.6950 | 49/49 | ok |
| APPLE | 44 | — | — | — | ok |

**No regressions.** Identical to the pre-Phase-18 baseline, which is the
required outcome: none of this phase's changes should have touched these
documents, and none did.

### 5.2 Quality and speed scorecard — the three new documents

Cold runs (OCR cache cleared), `VISUAL_ONLY`, after all Phase 18 changes.
Written by `scratch/p18_run_corpus.py` to `data/benchmark/scorecard.json`.

| | HDFC | PAYTM | GROWW |
|---|---|---|---|
| Pages | 16 | 18 | 14 |
| Wall (cold) | 174.4s | 144.6s | 133.0s |
| Pages/minute | 5.51 | 7.47 | 6.32 |
| **Facts** | **70** | **25** | **118** |
| High confidence | 39 | 6 | 102 |
| Requiring review | 15 | 13 | 29 |
| Unknown period | 12 | 11 | **118** |
| Unknown unit/currency | **0** | **0** | **0** |
| Conflicts | **0** | 2 | **0** |
| Charts detected | 11 | 6 | 1 |
| Facts from charts | 0 | 0 | 0 |
| Entities | `Consolidated 45`, `Standalone 25` | `Consumer Payments 23`, `Consolidated 2` | `Consolidated 118` |
| Strategies | `table_cell 66`, `narrative 4` | `spatial_grid 11`, `table_cell 10`, `narrative 4` | `table_cell 118` |

Reading this table honestly:

- **HDFC is the success.** Both bases separated, zero conflicts, zero unknown
  units. This document had 12 false conflicts before this phase.
- **GROWW's 118 facts are worth less than HDFC's 70**, because all 118 are
  undated (section 10.2). The fact count is the least informative column here.
- **PAYTM's entity column is the VISUAL_ONLY result**, and section 10.1 is
  about exactly that number.
- **Zero facts came from charts on any document**, which confirms the Phase 17
  triage-only finding on three documents it had not seen. Chart detection costs
  0.2s–8.2s per document and produces no financial facts; it exists to populate
  the Charts tab and to report what *could* be extractable.
- **No document produced an unknown unit or currency.** Unit evidence
  resolution is holding on unseen documents.

---

## 6. Clean before/after performance measurements

### 6.1 OCR cache reuse — the single largest speed effect in the system

Cold and warm runs of the same 18-page document, same code, same machine:

| | Wall | Page loop | Everything after | Facts |
|---|---|---|---|---|
| Cold (cache cleared) | **146.6s** | 107.6s | 39.0s | 25 |
| Warm (cache reused) | **42.4s** | 3.2s | 39.1s | 25 |
| | **3.46x** | **33.6x** | 1.00x | identical |

The cache is keyed on the PDF content hash plus the render signature plus the
OCR signature, so re-ingesting the same file under a new document id reuses the
work, and changing any render or OCR parameter invalidates it. Element ids are
re-stamped onto the receiving document by `rebind`, which is why the fact set
comes back identical rather than merely similar.

This mechanism is Phase 15/16 work. It was not rebuilt; it was measured, because
no measurement of it existed and it turns out to dominate everything else this
phase looked at.

### 6.2 Chart detection at a bounded radial size

Both versions in one process, alternating, over the real chart regions of every
corpus document — so machine drift falls on both equally:

| Document | Chart regions | Before | After | Speedup | Classifications |
|---|---|---|---|---|---|
| Paytm | 6 | 41.34s | 5.41s | **7.6x** | identical |
| IDFC | 205 | 14.35s | 12.12s | 1.2x | identical |
| RIL | 1 | 6.99s | 0.89s | **7.9x** | identical |
| StanChart | 50 | 2.35s | 1.35s | 1.7x | 1 changed |
| HDFC | 11 | 0.15s | 0.14s | 1.1x | identical |
| Groww | 1 | 0.02s | 0.02s | 0.9x | identical |
| **Total** | **274** | **65.19s** | **19.92s** | **3.3x** | **1 changed** |

The gain is concentrated exactly where it should be — on documents with
page-sized figures — and is absent on documents whose charts are already small.
That shape is the evidence the change is doing what it claims.

**The one changed classification is a false positive removed, not a detection
lost.** It is the back cover of the Standard Chartered annual report: contact
details set in plain text on a navy ground, no circle anywhere on the page. The
full-resolution pass reported a circle of radius 365px centred 91px from the top
edge — three quarters of it off the page. The crop was inspected directly before
this change was accepted. `INTER_AREA` is a proper low-pass filter, where
`medianBlur(5)` on a 1650px image is barely a filter at all, so the smaller
image is the *less* noisy input to a gradient-based search, not merely the
cheaper one.

End to end on the 18-page deck, warm, on a quiet machine: **42.4s → 20.8s**,
with the chart stage falling from 34.8–49.9s (that spread is the drift) to
**5.5s**, of which detection is 5.11s, triage 0.39s and page-furniture 0.01s.
Fact count unchanged at 25; chart types unchanged.

### 6.3 Page routing

**No measurable wall-clock gain on these documents, and the report says so.**

| Document | Before routing | After routing | Facts |
|---|---|---|---|
| HDFC | 139.8s | 138.0s | 66 → 66 |
| PAYTM | 147.0s | 153.4s | 25 → 25 |
| GROWW | 105.2s | 106.2s | 116 → 118 |

The differences are inside the run-to-run spread. The reason is arithmetic:
spatial extraction was only 5–8s of a 105–153s run, so skipping part of it
cannot show up against a 100s OCR loop. Routing was kept because it is free,
correct, and reports what it skipped in `diag["routing"]` — but it is not a
speed improvement and is not claimed as one.

---

## 7. Main bottlenecks discovered

Measured on a 12-core CPU machine, 200dpi renders, RapidOCR PP-OCR ONNX.

### 7.1 OCR inference is the run

Wall-clock split, deduplicating the three page phases that share one parallel
loop:

| Document | Pages | Wall | Page loop | Share | Tail |
|---|---|---|---|---|---|
| RIL_DEGRADED | 7 | 22.1s | 16.7s | 75.4% | 5.4s |
| RIL | 34 | 209.7s | 131.1s | 62.5% | 78.5s |
| APPLE | 3 | 26.7s | 23.1s | 86.6% | 3.6s |
| IDFC | 75 | 344.7s | 301.8s | 87.6% | 42.9s |
| StanChart | 165 | 1357.7s | 1267.7s | **93.4%** | 89.9s |

And inside the page loop, by thread time:

| Document | Rendering | OCR | Layout |
|---|---|---|---|
| IDFC | 2.7% | **93.1%** | 4.2% |
| APPLE | 2.2% | **96.3%** | 1.5% |
| StanChart | 1.1% | **96.8%** | 2.0% |

So on a real document, roughly **90% of wall clock is OCR inference**. Nothing
else is worth optimising until that is, which is what makes section 8 the
important section.

### 7.2 The chart stage had no internal timing

It reported one duration for three unrelated parts. On the 18-page deck the
stage cost 43s while chart detection inside it accounted for 6s, and there was
no way to see that from the outside — the first profile written for it measured
the wrong call set entirely (all 228 regions rather than the 6 the stage
actually analyses) and produced a confidently wrong attribution.

The stage now records `detail.seconds` as `{page_furniture, detection, triage}`.
That is a small change and it is listed as a bottleneck finding because the
absence of it *was* the bottleneck in understanding.

### 7.3 The machine itself

Four consecutive runs of the same stage on identical code: 34.8s, 38.9s, 43.2s,
49.9s. Any speed work here needs an A/B inside one process, or it is measuring
the weather.

### 7.4 A stale server can invalidate a whole afternoon

Three "before/after" reprocess runs in this phase were run against a uvicorn
process that had never died, so they all measured the old code. It was caught
because the numbers moved the wrong way. Every measurement in section 6 was
re-taken after confirming the listening PID.

---

## 8. Main speed improvements implemented — and the ones rejected

### 8.1 What shipped

1. **Bounded radial search** — 3.3x on chart detection corpus-wide (section 6.2).
2. **Memory-aware bounded concurrency** — see below.
3. **Page routing** — no measurable gain; kept for correctness reporting, not claimed as speed.

The OCR cache (3.46x end to end) is the largest effect in the system but is
existing Phase 15/16 machinery that this phase measured rather than built.

### 8.2 Memory-aware bounded concurrency

Worker count is two thirds of logical cores capped at eight, then capped again
by *available* memory at ~400 MB per worker, floored at two.

The core-count rule alone says what a machine can compute and nothing about
what it can hold. On a 16 GB machine with 2.2 GB free it still asked for eight
workers; eight page bitmaps plus eight ONNX arenas do not fit in 2.2 GB. The run
did not fail — it thrashed. One batch of eight pages took **35 minutes** where
the median page took 59 seconds, and a 75-page document that had taken 14
minutes took **48**. Nothing reported an error; the pipeline simply appeared to
hang. Sizing against free memory turns a stall into a slower but honest run.

Concurrency sweep, 24 pages, OCR cache bypassed, extraction verified identical
across every configuration (119 facts, 38 tables in all five):

| Workers | Intra-op threads | Wall | Per page |
|---|---|---|---|
| 2 | 4 | 363.3s | 15.14s |
| 4 | 3 | 291.9s | 12.16s |
| 4 | 4 | 281.0s | 11.71s |
| 6 | 2 | 276.0s | 11.50s |
| 8 | 2 | **255.1s** | **10.63s** |

29.8% from the baseline, and it flattens hard after four workers — the machine
is saturated, not under-scheduled.

### 8.3 Shared vs per-thread OCR engines — hypothesis disproved

The oversubscription hypothesis was that one shared ONNX session across threads
would contend, and per-thread engines would scale better. Measured, over 8 pages:

| Threads | Shared engine | Per-thread engines |
|---|---|---|
| 1 | 114.7s | 114.7s |
| 2 | **53.8s** | 161.5s |
| 4 | **48.8s** | 152.1s |
| 6 | **48.2s** | 196.9s |

Per-thread engines are **~3x worse** and get worse with more threads. ONNX
Runtime releases the GIL during inference and parallelises internally; adding
sessions multiplies the arenas and the contention. The shared engine was kept.

### 8.4 Detection resolution (`det_limit_side_len`) — rejected on correctness

`det_limit_side_len` controls the longest side the text detector sees, and
lowering it is the obvious OCR lever. It is genuinely faster on most pages:

| Page | side=1600 | side=1280 | side=960 |
|---|---|---|---|
| StanChart p25 (slowest) | 19.96s | 16.58s (0.83x) | 15.54s (0.78x) |
| StanChart p60 (typical) | 12.42s | 12.18s (0.98x) | 10.35s (0.83x) |
| IDFC p2 (KPI tiles) | 10.07s | 7.96s (0.79x) | 6.54s (0.65x) |

**Rejected**, because on the Paytm deck at side=1280 the page lost this string:

> `R1 Cr = R10 million; L Cr = Lakh Crore = 1 trllion equivalen`

That is the document's unit declaration. Losing it does not lose a fact — it
makes every figure on the page carry the wrong scale, or none. Thirteen strings
were lost on that page and nine gained, and the page was **1.06x slower**
anyway. A lever that trades a unit declaration for nothing is not a lever.

### 8.5 Render DPI — rejected, no effect

Lowering DPI barely moves OCR time, because the detector already downscales to
`det_limit_side_len` before it does anything:

| Page | dpi=200 | dpi=160 | dpi=130 |
|---|---|---|---|
| StanChart p25 (side=1600) | 17.20s | 17.51s (1.02x) | 17.68s (1.03x) |
| IDFC p18 (side=1600) | 5.50s | 5.41s (0.98x) | 5.47s (0.98x) |
| HDFC p3 (side=1600) | 14.99s | 16.09s (1.07x) | 15.32s (1.02x) |

Some combinations were *slower*. Meanwhile lower DPI degrades every downstream
consumer that works on the render — crops, table cell geometry, the page canvas.
All cost, no benefit. Not shipped.

### 8.6 Summary

**No safe OCR speed win was found.** Detection resolution costs a unit
declaration; DPI does nothing; per-thread engines are three times worse;
worker count is already at the point of diminishing returns and is now bounded
by memory rather than optimism. The honest position is that a ~90%-OCR workload
on this CPU runs at roughly 7 pages/minute, and making it materially faster
needs a different recogniser or a GPU, not another parameter.

---

## 9. Extraction-quality changes

Every change below is an attribution fix. **No change in this phase was made to
extract more figures**, and the fact counts move only where a fix legitimately
splits or merges attribution.

### 9.1 HDFC — twelve false conflicts eliminated

| | before | after |
|---|---|---|
| Facts | 66 | 70 |
| Entities | `Consolidated: 66` | `Consolidated: 45`, `Standalone: 25` |
| Conflicts | **12** | **0** |
| Requiring review | 27 | 15 |

The document reports both bases. Reading only the running header put them under
one entity, so the reconciler compared standalone revenue against consolidated
revenue and correctly concluded they disagreed — about facts that were never
comparable. Reading the basis declaration at the top of the page separates them,
and the twelve contradictions disappear because they were never contradictions.

This is the clearest result in the phase: the reconciliation layer was working
exactly as designed, and it was being fed a wrong entity.

### 9.2 Groww — 58 mis-attributed facts corrected

`Garage Ventures Private Limited)` — a fragment of the filer's own legal name,
cut by the line grouper — governed 58 of 116 facts as though it were a
subsidiary. All facts now carry the correct filing entity.

### 9.3 Paytm — improved in HYBRID, unchanged in VISUAL_ONLY

This one splits by processing mode, and the split is the most interesting result
in the phase.

| Entity | VISUAL_ONLY (before *and* after) | HYBRID (after) |
|---|---|---|
| `Consolidated` | 2 | 17 |
| `Consumer Payments` | **23** | 7 |
| `Consumer Businesses` | — | 1 |

Same code, same 25 facts, same document. The entity rule from section 2.1 is
not what separates them — see section 10.1, which is where this phase's most
actionable finding ended up.

### 9.4 Standard Chartered — 17 conflicts reclassified as scale disagreements

Section 1.

### 9.5 What did not change

Keyed accuracy is identical (section 5.1). Chart classifications are identical
across the corpus but for one removed false positive (section 6.2). Fact counts
on the routed pipeline are identical to the unrouted one (section 6.3).

---

## 10. New generalization gaps found

Both are stated without a fix, because both fixes available today would be
document-specific rules.

### 10.1 Entity attribution depends on region typing, and region typing is weaker in VISUAL_ONLY

This is the sharpest new finding of the phase, and it was found by accident:
the same document, same code, produced 23 mis-attributed facts through the
corpus runner and 7 through the API. The corpus runner runs `VISUAL_ONLY`; the
API ran `HYBRID`.

**OCR reads the deciding text correctly in both modes.** What differs is how the
layout detector types the region it lands in:

| Page 12 text | VISUAL_ONLY region type | HYBRID region type |
|---|---|---|
| "Summary of Consolidated Financial Performance" | `caption` | `title` |
| "Consumer Businesses" (p2) | `paragraph` | `section_heading` |

Entity resolution only consults `section_heading` and `title` regions. So in
VISUAL_ONLY the declaration that would *terminate* the carry-forward from
"Consumer Payments Business" is present in the OCR, correctly transcribed, and
never consulted — because it was filed as a caption. In HYBRID it is a title,
it terminates the carry-forward, and 16 facts move to `Consolidated`.

Two things follow, and they matter more than the Paytm numbers:

1. The gap is in **layout classification**, not in OCR recall, not in the
   entity rule, and not in period logic. That is a much smaller and better
   localised target than "Paytm mis-attributes facts".
2. **VISUAL_ONLY is the project's headline mode**, so this is the mode where
   accuracy matters most, and it is the weaker one here. Any future comparison
   of the two modes should be reported per-mode; a single number hides this.

Not fixed in this phase. Changing region typing touches every document
including the four keyed benchmarks, and doing it late in a phase without the
evidence to know what it costs is exactly the trade this project does not make.

### 10.1b A product heading and a segment heading can still be the same shape

Even in HYBRID, 7 facts remain under "Consumer Payments", from the heading
`Consumer Payments Business`. It ends in a structural head noun, which is
precisely the rule that correctly accepts Reliance's real segment
`JIOSTAR BUSINESS`. **There is no lexical or structural difference between
them.** Telling them apart needs evidence the system does not yet read —
whether the document declares a segment-reporting schedule at all, and whether
this heading appears inside it. A rule that excluded "Consumer Payments
Business" without that evidence would be a document-specific hack, so it was
not added.

### 10.2 Groww: table column under-segmentation leaves 118 facts undated

All 118 Groww facts carry **no period**. The cause is upstream of periods: the
table reader merges the header row into a single blob, so no column carries a
date, so no fact can be dated from its column. The facts are correct and
provenanced; they are simply undated, which is the designed behaviour — an
unknown period is preferable to an invented one — but it makes the document
much less useful than its fact count suggests.

This is a table-structure defect, not a period defect, and it is the strongest
candidate for the next phase.

### 10.3 A column header can swallow the paragraph above the table

Found while spot-checking Paytm's two remaining conflicts. Page 11 is a Summary
P&L with the columns `Jun-25 | Jun-26 | YoY`:

| Fact | Column | Period assigned | Basis | Confidence |
|---|---|---|---|---|
| Operating Revenue 2,448 | Jun-26 | `Q1 FY27` ✓ | inherited | 0.92 |
| Operating Revenue 1,918 | Jun-25 | **`As at 2025-12-31`** ✗ | derived | 0.75 |

The period parser is not the culprit. The stored `column_header` for the first
column is:

```
"comparable basis, excluding PIDF incentive which was discontinued in
 Dec'25 end  Jun-25"
```

The sentence introducing the table was absorbed into the header cell of the
first column. Given a header containing two dates, the parser took `Dec'25`
over `Jun-25` and produced a period six months wrong. The Jun-26 column's
header is clean (`"Jun-26"`) and dates correctly.

So there are two faults, and the order matters:

1. **The header row's extent is wrong** — a table-structure defect. This is the
   root cause.
2. **The period parser prefers the first date in a multi-date header** rather
   than refusing. Given a header that names two different dates, no period is
   the safer answer, and it would have caught this.

The second is the cheaper fix and the one that fails safe. It is also worth
noting the system *told* us: confidence on the polluted column was 0.75 against
0.92 on the clean one, so the evidence-weighted confidence was doing its job
even while the period was wrong.

**This is the same defect class as 10.2.** Groww merges several header cells
into one blob and dates nothing; Paytm merges a paragraph into a header cell
and dates it wrongly. Both are the header row's boundary being determined
incorrectly, seen from opposite sides.

### 10.4 The two Paytm conflicts are the reconciler working correctly on bad input

The same page carries two rows both printed `EBITDA Margin %` — one under
`EBITDA (Reported)`, one under `EBITDA (Comparable)` — whose Jun-25 values are
4% and 1%. The qualifier that distinguishes them sits on the EBITDA row above,
not on the margin row, so both facts arrive with an identical printed label,
identical metric, identical (wrongly derived) period and identical (wrongly
carried) entity.

Given four identical identity fields and two different values, `genuine_conflict`
is the right verdict and flagging both for review is the right action. The
reconciler is not at fault here; it is being handed two facts that a person
would also be unable to separate from the row label alone. Fixing this means
fixing the inputs — the period (10.3) and the entity (10.1) — not the
reconciler.

### 10.5 A note on fact counts

Groww produces 118 facts and Paytm produces 25. That is not a quality ranking.
Groww is a dense statutory filing and Paytm is a slide deck; 118 undated facts
are worth less than 25 dated and attributed ones. A larger fact count is not
automatically better, and this corpus makes that concrete.

---

## 11. Test results

### 11.1 Backend suite

```
644 passed, 1 skipped, 1 warning
```

The skip is `test_api.py:248` — "nothing needed review on this document" — a
conditional case, not a failure.

Movement across the phase (the first row is already mid-phase — the entity and
routing tests had been added by then):

| | Tests | Result |
|---|---|---|
| Before the basis fix | 630 | 628 passed, **1 failed**, 1 skipped |
| After the entity/basis fixes | 632 | 631 passed, 1 skipped |
| Final, with the chart-radial tests | **645** | **644 passed, 1 skipped** |

The one failure was `test_a_declaration_survives_the_description_after_it`,
resolved by the section 2.2 basis fix. It is worth naming because the test was
written *before* the fix, from the real HDFC heading, and failed for the right
reason.

### 11.2 Tests added this phase — 42

| File | Cases | What they hold |
|---|---|---|
| `test_entity_headings.py` | 17 | Every case is a real heading from a real filing. Which headings name a reporting unit and which merely mention one, including the four wrong answers found on unseen documents |
| `test_page_routing.py` | 12 | That a page routing skips is one every spatial detector would have refused — asserted by running the detectors on the skipped pages |
| `test_chart_radial.py` | 13 | That the radial answer does not depend on the working size, and that large regions really are downscaled while small ones are left alone |

### 11.3 Section-12 categories from the brief

| Category | Covered by |
|---|---|
| Cache correctness | `test_ingestion.py`, plus the cold/warm measurement in 6.1 producing identical facts |
| Memory-aware concurrency | `test_production_readiness.py` — five cases including the 2.2 GB case that caused the real stall |
| Pause / resume / cancel | `test_lifecycle.py` |
| Document deletion | `test_api.py`, `test_lifecycle.py` |
| Exports | `test_api.py`, `test_intelligence_e2e.py`, plus the live CSV check in 11.4 |

These were already covered by Phase 15/16 tests and were not rebuilt.

### 11.4 Export check, run live

`GET /export?format=csv` on the reprocessed document returns 25 rows and 43
columns, carrying the full reconciliation state — `reconciliation`,
`corroborating_sources`, `corroboration_count`, `conflicting_value`,
`conflicting_page`, `extraction_strategy`, `review_reason` — with review reasons
in plain English rather than enum names:

> "This metric appears more than once for the same entity and period with
> different values, and neither source is clearly better."

Phase 17's export of reconciliation state survives Phase 18 unchanged.

### 11.5 A note on what the tests could not have caught

The section 10.1 finding — that region typing differs between processing modes —
was found by a discrepancy between two runs, not by a test. Nothing in the suite
compares the two modes' region types against each other, and adding that
comparison is part of the section 14 recommendation.

---

## 12. Frontend validation results

### 12.1 Gate

| Check | Result |
|---|---|
| `tsc --noEmit` | 0 errors |
| `npm run build` | success — 431.38 kB JS (125.27 kB gzip), 32.61 kB CSS |
| `npm run lint` (oxlint) | 6 warnings — unchanged from the Phase 17 baseline, none in changed files |

### 12.2 Two measured dishonesties in the processing view, fixed

The processing panel already showed stage, pages done/total, elapsed, an
estimate, and pause/resume/cancel. Two things it showed were not true.

**The estimate counted down to "~0s" through the last quarter of every run.**
The estimate is derived from pages completed against elapsed time, and it went
to zero the moment the last page was read — but the run is not over then. On the
corpus the tail after the final page is **7% to 38% of wall clock** (27% on the
18-page deck measured live: 39 seconds of tables, charts, extraction and
analysis, with the panel showing "~0s remaining" for all of it). The estimate is
now withdrawn when the pages finish, and the panel says why:

> All pages read. The remaining stages — currently detecting charts — have no
> per-page rate to measure, so there is no estimate for them.

**The three page phases each displayed the full shared window.** Rendering, OCR
and layout interleave inside one parallel loop, so the backend records the same
wall clock against all three, and printing it three times read as though the run
took three times as long as it did. The loop is now one row, and the split
inside it comes from `phase_seconds` — thread time, labelled as a share rather
than as a duration, because the phases overlap and there is no honest per-phase
wall clock to print.

### 12.3 Browser inspection

Verified live against a real 18-page document mid-run, not against a fixture.
Mid-run the panel showed "Reading pages 16/18" with the three phases indented
beneath it. On completion of the page loop it showed:

```
100%                                    18 of 18 pages read
ELAPSED   2m 10s    PER PAGE  —    PAGES REMAINING  all read

Reading pages                    18/18
   Rendering pages                       3% of page work
   Reading text (OCR)                   95% of page work
   Analysing layout                      2% of page work
Reconstructing tables             9/9
Detecting charts                  0/6 · 0%
```

The 95% shown there is measured from that run, and it independently reproduces
the corpus bottleneck finding in section 7.1.

"Per page" reads "—" because the panel was opened partway through the run and so
had too few samples of its own to measure a rate. That is the intended
behaviour: it reports rates it measured, not rates it inferred.

---

## 13. Known limitations

1. **~7 pages/minute is the ceiling on this CPU.** Roughly 90% of wall clock is
   OCR inference and every parameter that reduces it was measured and rejected
   (section 8). Materially faster needs a different recogniser or a GPU.
2. **Groww: all 118 facts are undated** — table header under-segmentation
   (section 10.2). Honest, but it makes the document far less useful than its
   fact count suggests.
2b. **Paytm: comparative columns can be mis-dated** by a date in the prose above
   the table — six months wrong on page 11 (section 10.3). This is the more
   serious of the two, because a wrong period is worse than no period.
3. **VISUAL_ONLY types regions less accurately than HYBRID**, and entity
   attribution depends on region type (section 10.1). This is the highest-value
   open defect in the system.
4. **A product heading and a segment heading can be structurally identical**
   (section 10.1b). No fix without evidence the system does not yet read.
5. **"Digital Services" no longer resolves** — a deliberate narrowing whose cost
   is recorded in a test rather than hidden (section 2.1).
6. **Charts still produce zero financial facts**, on three further documents.
   Triage reports what could be extractable; nothing extracts it.
7. **`FactsTab.tsx` was reconstructed, not recovered.** During Phase 17 a
   `git checkout` of that file destroyed Phase 15/16 work on it and it was
   rebuilt from the session record and the Phase 16 dist bundle. It typechecks,
   builds and behaves correctly under inspection, but it deserves a careful
   human read against the original intent. This is restated here because it has
   not been reviewed since.
8. **RIL_DEGRADED sits at F1 0.695** and has not moved for three phases. It is a
   scanner-degraded document; twelve image-repair variants were measured in an
   earlier phase and none recovered accuracy.
9. **The scorecard is single-run.** Given the drift documented in section 7.3,
   the wall-clock columns in section 5.2 should be read as ±25%, not as precise
   figures — HDFC alone measured 138.0s and 174.4s on runs that differed by no
   relevant code. The fact and attribution columns are exact and repeatable.

---

## 14. Recommended next phase

In priority order, by measured value rather than by appeal.

### First — region typing in VISUAL_ONLY (section 10.1)

The best-localised defect in the system. The same document, same code, produces
materially different entity attribution between modes solely because the layout
detector types a heading as a caption when native text is unavailable. It is a
classification problem with a ready-made oracle: run every corpus document in
both modes and treat each disagreement in region type as a labelled case. It
directly improves the mode the project leads with.

### Second — period assignment from table columns (sections 10.2 and 10.3)

Two failures with one shape: a fact does not get its period from its own column.

- **Groww**: the header row merges into one blob, no column carries a date, and
  all 118 facts end up undated (10.2).
- **Paytm**: the comparative column takes a date out of the prose above the
  table instead of its own header, and is dated six months wrong (10.3).

These are the same problem seen from both sides — column-to-period binding is
too weak, so it either yields nothing or yields the nearest date. Dating facts
correctly is worth more than extracting new ones, and 10.3 is the more urgent
half because an undated fact is honest while a mis-dated one is not.

### Third — decide what chart triage is for

Charts have now produced zero facts across eight documents while costing
0.2s–8.2s each. Either extract from the charts that triage says are extractable,
or stop paying for detection on documents where the Charts tab is the only
consumer. Both are defensible; carrying the cost indefinitely without deciding
is not.

### Fourth — a second machine, or a fixed measurement harness

Section 7.3 and 7.4 both cost real time this phase. A quiet baseline — or at
minimum a harness that always A/Bs in-process and always verifies the running
PID — would have saved most of it.

### Explicitly not recommended

- **More OCR parameter sweeps.** Detection resolution, DPI, worker count,
  intra-op threads and engine sharing have all been measured. That ground is
  covered; section 8 is the record.
- **Raising fact counts.** Groww already produces 118 facts and they are worth
  less than HDFC's 70. Attribution and dating are where the value is.

---

## Appendix — how to reproduce

```bash
# backend suite
cd backend && ../.venv/Scripts/python.exe -m pytest tests/ -q

# keyed accuracy gate
cd backend && ../.venv/Scripts/python.exe ../scratch/p16_benchmarks.py

# quality + speed scorecard for the three new documents
cd backend && ../.venv/Scripts/python.exe ../scratch/p18_run_corpus.py --cold HDFC PAYTM GROWW

# chart detection A/B, both versions in one process
./.venv/Scripts/python.exe scratch/p18_chart_ab.py

# frontend gate
cd frontend && npx tsc --noEmit && npm run build && npm run lint
```

Machine: 12 logical cores, Windows 11, CPU-only ONNX Runtime. Renders at 200dpi;
RapidOCR PP-OCR with `det_limit_side_len=1600`, `text_score=0.35`,
`angle_cls=off`. Page workers resolved to 4 during the corpus runs (memory-bound,
2.2 GB available).
