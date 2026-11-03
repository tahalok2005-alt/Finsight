# Phase 17 — financial evidence reconciliation

Every number here was measured on this machine, on the final code. Where a
result is worse than Phase 16's, it is here rather than absent, and where a
measurement is noisy the spread is given rather than the best of it.

Documents under test: `IDFC_Q12027.pdf` (75 pages), the RIL media release
(34 pages), its image-only and scanner-degraded variants, and Apple's Q3 FY25
consolidated statements.

---

## 1. Architecture changes

The phase began with an audit of the existing reconciliation —
`docs/EVIDENCE_RECONCILIATION.md`, written by instrumenting the running system
before any code changed. It reordered the work: the defect that mattered most
was not on the brief's list.

New modules:

| Module | What it is |
|---|---|
| `app/analysis/identity.py` | What makes two readings the same fact — and, separately, whether they can be compared at all |
| `app/normalization/evidence.py` | Whose statement about a figure's kind wins: cell > row > table > taxonomy |
| `app/spatial/transposed.py` | Reading a grid of figures the document drew without ruling it |
| `app/charts/triage.py` | What, if anything, a detected chart can honestly be asked for |
| `frontend/src/utils/evidence.ts` | Corroboration and contradiction, in a reader's words |
| `frontend/src/components/FigureDetail.tsx` | Everything known about one figure |

Changed:

| Module | Change |
|---|---|
| `analysis/reconcile.py` | Rewritten around the new identity; all eight verdicts now reachable; both sides of a contradiction flagged |
| `document/assembly.py` | `_section_bands` — a terminator for entity carry-forward |
| `services/pipeline.py` | Containment-based entity for spatial facts; the grid reader as a stage; chart triage; corroboration fed into confidence |
| `extraction/confidence.py` | `apply_reconciliation` — agreement raises, contradiction caps |
| `extraction/facts.py` | The shared value-type precedence, which the table path did not have |
| `analysis/insights.py` | Period ordering by end date, so an "as at" series is chronological |
| `api/routes_documents.py` | An `understanding` block: what FinSight made of the document |
| `export/exporters.py` | Six reconciliation columns |

---

## 2. The audit, and what it found

Measured across four documents, not read off the code.

**Six of `classify()`'s eight verdicts were unreachable.** Every discriminator
it tested — entity, period, printed label, value type, unit, currency — was
already part of the bucket key, so inside a bucket they were equal by
construction. Only `identical_repeat` and `genuine_conflict` could ever be
returned. The vocabulary the module advertised described distinctions it could
not draw.

**78 % of IDFC's figures were outside reconciliation entirely.** A fact with no
period was its own bucket, so it could neither corroborate nor contradict
anything. On IDFC that was 81 of 104.

**The printed label was part of the identity**, so two readings of one row
never met:

```
RIL   depreciation  Q1 FY27   'Depreciation'                  15,100 crore
                              'Depreciation / Amortisation'   15,100 crore
```

Same number, same period, same entity, two rows of one statement — recorded as
two independent facts. 36 such groups on RIL, 14 on the degraded scan.

**Every contradiction's survivor was presented as clean.** `reconcile` set
`duplicate_resolution` on the loser only; `review_reason_for` decides from that
field, and the later confidence pass overwrote `requires_review` from the
reason. Measured:

| Document | facts in a conflict | **not** flagged |
|---|---:|---:|
| IDFC | 2 | 1 |
| RIL | 6 | 3 |
| Degraded | 70 | 37 |

On IDFC the surviving side of the document's only contradiction was shown at
**confidence 0.9103, band HIGH, no review flag**.

---

## 3. The defect that was not on the list

`entity_by_region` carried the current entity forward until another entity
heading appeared. There was no terminator.

On RIL this works, because each segment has its own heading page and the next
heading ends the previous. On IDFC, one segment heading appears on page 23 and
nothing resets it:

| IDFC entity | Phase 16 pages | facts | Phase 17 pages | facts |
|---|---|---:|---|---:|
| **RAM** | **41–73** | **67** | **30** | **4** |
| RETAIL BANKING | 8–18 | 24 | 8–18 | 24 |
| Consolidated | 2 | 13 | 2–73 | **79** |

`RAM` is the deck's Retail/Agri/MSME lending book. It owned page 45 (the bank's
own profit and loss), page 59 (capital adequacy for the whole bank), page 62
(the shareholding pattern) and page 73 (the merger background). **64 % of the
document's figures carried a segment name that did not own them.**

### The terminator had to be chosen from evidence

The obvious rule — end a segment at the next section heading — was tested
against both documents and is wrong. RIL's segments span 2 to 7 plain headings
each; ending them at the first would shatter attribution that currently works.
IDFC's `RAM` spans 53.

What actually separates the two is that IDFC prints its own structure in a
running header that holds still across a section, and RIL does not:

```
IDFC   p6  'Section 1: Background'      p32 'Section 4: Asset Quality'
       p10 'Section 2: Liabilities'     p44 'Section 5: Profitability & Capital'
       p21 'Section 3: Assets'          ...  5 bands over pages 6-63

RIL    p22 'Particulars Quarter Ended…' p24 '5. The Audit Committee has…'
       p23 '4. Formulae for computation…'  ... 12 changes in 30 pages
```

So a running header is treated as section structure only when it **repeats
across at least three consecutive pages**. A header that changes every page is
page content the layout detector mislabelled, and it terminates nothing. IDFC
yields five bands; RIL yields one that disturbs nothing.

The second half of the fix replaces `art.regions[0]` — the first entry of a
list the detector fills tables-first, which is neither reading order nor
containment — with the smallest region that actually contains the figure.

---

## 4. Fact identity

`FactIdentity` is `(metric, entity, period, period_type, basis)`. It excludes
four things deliberately:

* **the value**, because two candidates with one identity and different values
  are the case this layer exists for;
* **the printed label**, which moves to the *classification*, where it explains
  a difference instead of preventing one from being seen;
* **the unit and currency**, which move to *comparability* — so ten thousand
  million and one thousand crore are compared through `base_value` instead of
  being filed as unrelated;
* **the source**, because which strategy read a figure says nothing about which
  figure it is.

All eight verdicts are now reachable and all eight appear on real documents:

| Verdict | IDFC | RIL | Degraded | Apple |
|---|---:|---:|---:|---:|
| `uncertain` (undated, cannot be shown to disagree) | 57 | 16 | 0 | 0 |
| `different_definition` | 3 | 16 | 8 | 4 |
| `identical_repeat` | 2 | 28 | 2 | 4 |
| `genuine_conflict` | 2 | 8 | 72 | 0 |
| `different_unit` | 0 | 5 | 4 | 0 |

---

## 5. Value-type evidence, shared

Defect A. The precedence — **cell > row > table > taxonomy** — now lives in one
module and both paths use it. The spatial reader always refused a percentage
standing for a monetary metric; the table path did not, and recorded
`casa_deposits = 50.80 %` as an amount of money. Where two claims sit at the
same level and disagree, the verdict is `conflicted` and the caller records
UNKNOWN rather than picking the convenient reading.

---

## 6. Transposed tables

`spatial/transposed.py` finds a *lattice*: figures at repeated x and y
positions, a label axis down one side, a period axis along the other. It reads
both orientations without assuming either — which axis names the measures is
decided by what the axes contain.

Two refusals were added after measuring what the first version produced on real
pages, and both are Part 6's warning:

**A growth column is not a period value.** A balance sheet row read
`4,088 / 5,000 / 5,137 / 25.7 %`. The last is the year-on-year change; read as
a fourth balance it is the wrong kind of number *and* dated to the quarter
beside it. A cell whose kind disagrees with its row's is refused.

**A header serving two columns dates neither.** On RIL a quarter-ended head sat
between the quarter column and the year-ended column; taking it for both would
have dated a full year's figure as a quarter.

What it found on IDFC — page 30's loan-book table, which the table extractor
misses entirely:

| Metric | Value | Period |
|---|---|---|
| Total Loan Assets | US$ 11,051 m | As at 31 December 2018 |
| Total Loan Assets | US$ 26,738 m | As at 30 June 2025 |
| Total Loan Assets | US$ 30,649 m | As at 31 March 2026 |
| Total Loan Assets | US$ 32,243 m | **Period not identified** |

The fourth column's header did not line up, so it carries no period rather
than borrowing its neighbour's.

---

## 7. Chart triage — and why it extracts nothing

The audit found chart data labels are **geometrically excluded from their own
charts**. A chart region is the ink of the bars; printed labels sit above them.
On IDFC page 51, a bar chart with seven printed percentages:

| label | `contains_ratio` | kept by the old test? |
|---|---:|---|
| 553.0 % | 0.58 | no |
| 226.5 % | 0.28 | no |
| the other five | 0.00 | no |

Zero of seven. Across 82 charts the detector captured 38 labels in total, and
those were the accidental ones — `343397434477.0` from a page whose figures are
in the thousands. Page 33, the single most extractable chart in the document
(seventeen printed percentages on a clean `Q1 FY23 … Q1 FY27` axis), **has no
chart region at all**.

The triage therefore gathers evidence by *association* — a band around the plot
where a designer puts labels and axes — and reports what it finds:

| | IDFC | RIL |
|---|---:|---:|
| charts | 82 | 1 |
| likely financial | 24 | 1 |
| with data labels | 39 | 1 |
| with period labels | 18 | 0 |
| with a **recognised metric** | 12 | 1 |
| **extraction possible** | **0** | **0** |

**No chart in any document passes every gate, and no chart produced a fact.**
An earlier draft of the triage reported 17 IDFC charts as extractable; their
"metric labels" were `In US$ Mn.`, `#Excluding Money Market Borrowings` and
`IDFC FIRST`. Requiring the metric to match the taxonomy — the same standard
the tile and row readers are held to — took it to zero.

The remaining gate, even with all three signals present, is that the values and
the periods must correspond one for one. A chart plotting seventeen figures
against fourteen ticks has an association problem this phase does not solve,
and guessing at it is how a figure from one year gets reported under another's.

Nothing estimates a value from a bar's height, a line's position, or any other
pixel geometry.

---

## 8. Cross-strategy reconciliation

The clearest evidence that the layer works is what happened on RIL. The grid
reader produced **115 candidates on page 21** — a page the table extractor
already reads well. Net effect on the fact count: **+4**. The rest were
absorbed as corroboration of the table facts rather than counted twice, and the
benchmark F1 did not move.

On IDFC:

* **Corroboration.** `Loans & Advances` rose from HIGH 90 to **HIGH 92**, with
  "Corroborated" shown beside it — the KPI tile and the narrative sentence read
  the same figure from different pixels.
* **Contradiction.** Page 2's `Total Deposits` is US$ 32,931 m on the KPI tile
  and US$ 31,613 m in the footnote. Both are now **LOW 64 · Sources disagree**,
  both retained, neither chosen. In Phase 16 the first was shown at HIGH 0.9103
  with no warning at all.

---

## 9. Confidence

`apply_reconciliation` runs after the evidence layer, which is why it can see
anything at all — previously confidence was scored before anything had compared
the facts.

Agreement is quantitative and moves the corroboration component, counted once
per *strategy*: three rows of one table agreeing is one method agreeing with
itself. Contradiction is categorical and caps the score below the review
threshold; it is not blended into the weighted sum, so no amount of clean OCR
lifts a disputed figure back out of review.

One bug found by its own test: the first version recomputed the weighted sum
from the components, discarding caps and floors applied earlier, so a fact that
*gained* a corroborating source could come out less certain. It now applies the
delta to the score as it stands.

---

## 10. IDFC before and after

| | Phase 16 | Phase 17 |
|---|---:|---:|
| Facts | 104 | **107** |
| — statement table | 80 | 79 |
| — KPI tile | 12 | 12 |
| — horizontal row | 3 | 3 |
| — **drawn grid** | 0 | **4** |
| — narrative | 9 | 9 |
| Facts on the wrong entity | **67** | **0 known** |
| Corroborated | not detected | 2 |
| Contradictions surfaced | 1 of 2 sides | **2 of 2** |
| Figures with no period | 81 | 81 |
| Requiring review | 32 | 31 |
| Charts analysed | 82 | 82 |
| Charts analytically readable | not measured | **0** |
| Cold run | 326.4 s | **344.8 s** |

The fact count rose by three because four grid facts were added and one
duplicate merged. It is not the point of the phase and should not be read as
one.

---

## 11. Performance

Clean cold run, nothing else running, OCR cache cleared.

| Stage | Phase 16 | Phase 17 |
|---|---:|---:|
| rendering + OCR + layout | 288.7 s | 301.8 s |
| charts (detect + triage) | 15.2 s | 20.7 s |
| extraction (all five strategies) | 22.0 s | 21.4 s |
| tables | 0.2 s | 0.4 s |
| **wall clock** | **326.4 s** | **344.8 s** |

**+18.4 s, or +5.6 %.** Phase 16 measured the run-to-run spread on this machine
at ±14 % across seven clean cold runs, so a single 5.6 % difference is inside
the noise and should not be read as either a cost or a saving. What can be said
from the stage split: extraction did **not** grow despite gaining a whole
strategy, because the grid reader works on OCR geometry the pipeline already
has. Nothing is rendered or recognised twice. The chart stage grew by 5.5 s,
which is the triage pass over 82 regions.

---

## 12. Benchmarks

| Benchmark | Baseline | Phase 17 | Matched | Verdict |
|---|---:|---:|---:|---|
| RIL media release | 0.9882 | **0.9882** | 84 / 84 | unchanged |
| RIL image-only | 0.9877 | **0.9877** | 80 / 80 | unchanged |
| RIL scanner-degraded | 0.695 | **0.695** | 49 / 49 | unchanged |
| Apple statements | 52 facts | **44 facts** | — | see below |

Benchmarks were run at four separate checkpoints, because each core change
could have moved them.

**Apple 52 → 44 is a correction, not a regression, and it was verified fact by
fact.** OCR read one income-statement row three times per period — `Total net
sales (1)`, `Total net sales $`, `$ Total net sales` — same value, same page,
same period. Twelve readings became four facts. The old identity kept them
apart because the printed labels differed. The harness now records 44 as the
baseline with that reason in a comment.

The same change also let `different_definition` do its job for the first time:
"Total current assets" (122,491) and "Other current assets" (14,359) both map
to `current_assets`, now meet, and are correctly told apart and both retained.

---

## 13. Tests

| | Phase 16 | Phase 17 |
|---|---:|---:|
| Backend | 526 passed, 1 skipped | **597 passed, 1 skipped** |
| Frontend typecheck | clean | clean |
| Production build | succeeds | succeeds |
| Lint | 6 warnings | 6 warnings, none new |

71 tests added across `test_reconciliation.py` (identity, comparability,
classification, reconciliation, confidence, provenance) and
`test_transposed_and_charts.py` (drawn grids, refusals, shared value-type
evidence, chart triage).

---

## 14. Regressions encountered

| # | What | How found | Fix |
|---|---|---|---|
| 1 | Apple 52 → 44 facts | benchmark gate | not a regression; twelve OCR reads of one cell became four facts, verified individually |
| 2 | Agreement could *lower* a confidence score | its own unit test | `apply_reconciliation` rebuilt the weighted sum and discarded earlier caps; now applies the delta |
| 3 | An "as at" series sorted arbitrarily, reporting a 2018 figure as a decline against 2025 | browser inspection | `_period_rank` understood only fiscal year and quarter; now ranks by end date, and refuses to report growth between two figures it cannot order |
| 4 | A conflicting source read "Another source" in the UI | browser inspection | `narrative_sentence` and `spatial_grid` were missing from the frontend strategy vocabulary |
| 5 | A column with two equally-aligned headers took one arbitrarily | its own unit test | a stacked head is resolved by height; two heads at the same height leave the column with none |
| 6 | Chart triage reported 17 extractable charts | inspecting the output | their metric labels were unit declarations and footnotes; requiring a taxonomy match took it to 0 |

**A self-inflicted one, outside the phase.** I ran `git checkout` on
`frontend/src/views/doc/FactsTab.tsx`, which reverted it to the last commit and
destroyed the Phase 15 and Phase 16 work on that file — the period display, the
review-reason wording, the strategy label. It was rebuilt from the session
record and the Phase 16 production bundle. It typechecks, builds and behaves
correctly in the browser, but **that one file is reconstructed rather than
original** and deserves review. No other file was touched.

---

## 15. What still does not work

**Charts produce no facts, and the reason is geometric.** Chart regions do not
contain their own labels, and the most readable chart in the document is not
detected as a chart at all. The triage now measures this honestly rather than
working around it, but reading charts needs the region geometry fixed first —
widening regions to their plot area, and finding the charts the blob detector
misses.

**81 of 107 IDFC figures still have no period**, unchanged from Phase 16. They
now at least participate in reconciliation — 57 met another reading and were
classified `uncertain` rather than being invisible — but an undated figure can
only ever corroborate, never contradict, because two figures with unknown
periods may belong to different quarters.

**`RETAIL BANKING` still owns 24 figures on pages 8–18.** The heading is a
label inside a wheel diagram ("RETAIL BANKING PRODUCTS"), not a section
heading. The band terminator bounds it correctly, and Section 2 of the deck is
genuinely about the retail liability franchise, so the attribution is
defensible — but it rests on a diagram label, which is weaker evidence than it
appears.

**The transposed reader is not routed by page structure.** It runs on every
page and is held back only by the `occupied` list and the shared acceptance
rules. On IDFC it offered 248 candidates and 4 became facts; on RIL 115 became
4. That is the reconciliation layer doing its job, but a page classifier would
avoid the work rather than absorb it.

**Two `Total Deposits` readings are flagged as contradicting when they are
arguably different measures.** The deck's footnote says total deposits include
customer deposits plus certificates of deposit — 32,931 and 31,613 are a total
and a component. The taxonomy maps both to `total_deposits`, so the layer
correctly reports a conflict on the evidence it has. Resolving it needs the
taxonomy to distinguish the two, not the reconciler to guess.

**Validation remains thin.** Five validation results for 107 facts. It is not a
meaningful contradiction detector, and the evidence layer now does more of that
work than validation does.

**The document quality summary is served but not rendered.** The
`understanding` block is on `/diagnostics`; the Overview tab still shows the
older card set. The data is there and correct; the frontend does not yet read
it.
