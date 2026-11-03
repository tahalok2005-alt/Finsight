# Evaluation

> **Note.** The figures below are for the production **HYBRID** mode. The same
> document processed in strict **VISUAL_ONLY** mode — with the PDF text layer
> hard-blocked from reaching extraction — scores identically (F1 0.988) and is
> audited separately in [VISUAL_ONLY.md](VISUAL_ONLY.md), together with an
> image-only PDF test.

Two kinds of measurement, kept separate because they answer different questions:

- **Intrinsic metrics** need no ground truth and run on any document. They
  measure whether the system is internally coherent — OCR quality, table
  structural soundness, normalisation coverage, provenance completeness,
  validation pass rate.
- **Accuracy against ground truth** requires a hand-checked answer key and
  measures whether the numbers are actually right.

Both are produced by `python cli.py evaluate <document_id>`.

## Ground-truth methodology

The answer key for the acceptance document lives at
`data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.truth.json`.

**How it was built.** Every value was read directly off the source PDF by
extracting its own text layer with coordinates and reading the resulting rows —
*not* by copying pipeline output. The key records the page each figure appears
on, its unit and its currency. The pipeline has no access to the file at
runtime; it is loaded only to score a finished run.

**Coverage.** 85 facts spanning:

- 6 reporting entities (Consolidated, JPL, RRVL, O2C, Oil & Gas, and operational
  KPIs)
- 4 reporting periods (Q1 FY27, Q4 FY26, Q1 FY26, FY26)
- 5 different tables across pages 1, 4, 5, 7, 8, 11 and 16
- monetary, percentage, ratio and count value types

This is a rigorous spot check, not exhaustive coverage of all 397 extracted
facts. It was chosen to exercise the parts most likely to break: multi-entity
attribution, period-to-column mapping, percentage-versus-monetary typing, and
operational tables with a separate unit-of-measure column.

**Scoring.** A fact matches when `(canonical_metric, entity, period)` agrees and
the value is within 0.5% — enough to absorb rounding, not enough to hide a
misread digit. Entity names are compared on their distinctive tokens, so the key
can say "Oil and Gas" where the document's banner says "OIL AND GAS (EXPLORATION
AND PRODUCTION)"; scoring rewards correct *attribution*, not identical spelling.


## Fact counts after evidence reconciliation

From Phase 17 a document's fact count is a count of *reconciled* figures, not
of readings. Where two strategies read one figure, or OCR reads one row twice
under slightly different labels, those become one fact carrying both sources.

The Apple benchmark moved from 52 facts to 44 for exactly this reason, and it
is a correction rather than a loss. OCR returned that filing's total-net-sales
row three times per period -- `Total net sales (1)`, `Total net sales $`,
`$ Total net sales` -- with the same value on the same page. Twelve readings
became four facts. The previous identity kept them apart because it included
the printed label.

Accuracy against the ground-truth keys is unaffected: precision, recall and F1
are measured against matched truth facts, and no truth fact lost a match.

| Benchmark | F1 before | F1 after |
|---|---:|---:|
| RIL media release | 0.9882 | 0.9882 |
| RIL image-only | 0.9877 | 0.9877 |
| RIL scanner-degraded | 0.695 | 0.695 |

A fact count is therefore not comparable across phases without saying whether
reconciliation was running. F1 is.


## What is measured, and what is not

Two different things are reported throughout, and they are never combined:

**Benchmark ground-truth accuracy** — precision, recall and F1 against a
hand-built answer key. The keys hold **85 facts** (RIL media release) and
**81 facts** (image-only / scanner-degraded). This measures correctness on
high-value figures that were checked by hand.

**Total extracted facts** — how many facts the pipeline produced, and how many
carry a period, an entity, a unit, a currency and a bounding box. This measures
coverage. It is *not* an accuracy figure: no claim is made that all 432 facts
extracted from the RIL filing are correct, only the 85 that were checked.

A document with no answer key (the Apple filing) reports coverage and **no
accuracy at all**.

---

## Benchmark documents

| Document | Pages | Text layer | Key | Why it is here |
|---|---|---|---|---|
| RIL Q1 FY2026-27 media release | 34 | 68,853 chars | 85 facts | The acceptance document |
| RIL Q1 FY27 image-only | 7 | **0 chars** | 81 facts | Proves the no-text-layer path |
| RIL Q1 FY27 scanner-degraded | 7 | **0 chars** | 81 facts | Real scanner artefacts on the same pages |
| Apple Inc. FY25 Q3 statements | 3 | 6,399 chars | none | A genuinely different filer |

The Apple filing is a real published document from Apple's newsroom
(`FY25_Q3_Consolidated_Financial_Statements.pdf`, 3,148,382 bytes, sha256
`7c982ae6fed09ee53acab12afb1ad374b3fd6a5905c44bf0ef3130f8a8bd67fe`). It is
USD / millions / September year end / US GAAP against the RIL benchmark's
INR / crore / March year end. No value in it was fabricated, and no code refers
to it.

The scanner-degraded fixture is generated by
`backend/scripts/make_scanned_fixture.py` from the *same real pages* as the
image-only benchmark: 150 dpi capture, 0.6 degree skew, Gaussian sensor noise,
JPEG quality 55, uneven illumination. Only image quality changes, so the same
answer key applies.

### The golden corpus: facts read from the source

Keyed accuracy scores whether a change broke something. It cannot say whether a
figure carries the *right period* -- the answer key records values and metrics,
not the column each value came from.

`data/golden/*.json` closes that gap. Each file is a statement transcribed from
the rendered page **by eye**, never from FinSight's output: the table's basis
and the evidence for it, its unit declaration and where that declaration sits,
every column with its group head, its head lines, its resolved period, its audit
status and its note reference, and every row's values aligned to those columns.
A printed dash is recorded as `"nil"` and a blank cell as `null`, because
neither is a fact and a system producing `0.0` for them would be inventing a
figure.

| File | Document | Page | Golden facts |
|---|---|---|---|
| `GROWW_p6_consolidated_pl.json` | Groww consolidated P&L | 6 | 99 |
| `HDFC_p1_standalone_results.json` | HDFC Bank standalone results | 1 | 52 |

The two were chosen to disagree with each other. Groww writes its dates out and
banners its unit across the table; HDFC dots them (`30.06.2026`), declares its
basis in the title, puts the unit in a corner note, and numbers its rows. Three
defects of Phase 19 were found precisely because HDFC broke a rule Groww had
confirmed -- which is the whole reason a corpus needs documents that fail
differently.

`app/evaluation/golden.py` scores extraction against them and reports what the
quality bar asks for: correct-period, correct-unit and correct-entity rates over
the facts actually extracted, and separately the golden facts that were not
extracted at all. A run producing fewer facts but attributing them all correctly
is the better run, and these numbers can say so.

### Coverage and accuracy are measured separately

Two numbers, and confusing them is how a corpus stops being useful:

* **Coverage** = golden facts correctly extracted / golden facts expected.
* **Accuracy** = extracted golden matches with the right period, unit and
  entity / extracted golden matches.

A change that lifts coverage while introducing wrong periods has failed, and a
single blended score cannot say so. `scratch/p20_baseline.py` reports both, per
table, alongside a root-cause bucket for every miss.

`app/evaluation/diagnose.py` produces that bucket by walking the extractor's own
path and stopping at the first stage that cannot account for the fact. The order
matters: a row whose label the taxonomy does not know will also have no period,
and reporting it as a period failure sends the next phase to the wrong module.
The buckets are the brief's own: taxonomy, row recognition, label parsing,
number parsing, column geometry, period resolution, entity, unit, fragmentation,
strategy not invoked, safety gate, reconciliation, duplicates, and genuinely
unreadable source.

Measured on the two golden tables, the classification is what chose the work:
68 of 79 misses were vocabulary, and none at all were geometry, period, entity
or unit. Coverage went from 47.7% to 90.1% with accuracy unchanged at 100%.

### The unkeyed corpus

Five further real documents are processed but not scored against a key, because
no key exists for them. They exist to answer a different question: *does this
generalise to a filer we have never seen?* Keyed accuracy tells us whether a
change broke something; these tell us whether it was ever right.

| Document | Pages | Why it is here |
|---|---|---|
| IDFC FIRST Bank Q1 FY27 | 75 | Investor deck: KPI tiles, wheel diagrams, 82 chart regions |
| Standard Chartered HY 2026 | 165 | The scale case; also two currencies and two scales for one figure |
| HDFC Bank results | 16 | Reports standalone *and* consolidated in one filing, with an OCR-degraded header band |
| Paytm results | 18 | Slide deck: marketing headings, product names, a unit declaration in a footnote |
| Groww results | 14 | Long legal entity name that the line grouper splits; dense undated tables |

They are read in place from their original locations, recorded in
`data/benchmark/corpus.json`; nothing is copied into the repository. The
quality-and-speed scorecard for the last three is written to
`data/benchmark/scorecard.json` by `scratch/p18_run_corpus.py`.

The most useful property of this set is that its documents fail *differently*.
Three of them independently exposed one shared cause in entity resolution, which
is not something a single document could have shown: each one alone looked like
a document-specific quirk.

### The Phase 21 corpus: fifteen companies, twenty documents

Two golden tables from two companies cannot show that anything generalises.
Phase 21 added seventeen official filings from twelve more companies -- banks
(public and private, standalone and consolidated side by side, typeset and
scanned), IT services under IFRS with a US-dollar translation column, a German
software group reporting in euros, telecom, steel, consumer goods, an NBFC deck,
a newspaper-printed results extract -- chosen for how differently they set out
the same statements. `data/corpus/manifest.json` records each file's source URL,
SHA-256, page count and its shape, period, unit and entity characteristics,
written down *before* any of them was processed.

Twenty-six more golden tables (2,616 golden facts across 28 tables in all) were
transcribed by eye from those documents, with the same rules as the first two
plus: every printed subtotal is checked as an identity against the transcribed
components before a file is written (which caught two of my own misreads), and
a row that means nothing without its parent carries the parent in its name
("Interest earned: (d) Others", "Tax expense: Current tax").

The harness never reprocesses a document to answer a question about it:

| Script | Does |
|---|---|
| `scripts/corpus_run.py --tag T [KEYS]` | Processes each document once (VISUAL_ONLY, OCR cache honoured) and records the facts, tables and validations as `data/corpus/runs/<KEY>.<T>.json`. A document already recorded for `T` is skipped, so a run stopped midway must be resumed on the same code or restarted under a new tag. |
| `scripts/corpus_score.py --tag T [--compare U]` | Scores every golden table against the recorded run; classifies every missing, wrong or incomplete golden fact into the brief's categories A-Q by reading the stored evidence and the OCR cache; audits accounting alarms (a *false* alarm is one where every involved figure is correct). Writes `scores/T.json` and `DASHBOARD_T.md`. |
| `scripts/corpus_compare.py --tag T` | The source-to-FinSight comparison sheet: 20 golden facts per company, evenly spread, beside FinSight's reading of the same cell. |
| `scripts/corpus_corroborate.py --tag T` | Cross-source agreement between documents of one company (scanned vs typeset, results vs press release). |

A golden fact is **correct** when every dimension the source states is right,
**incomplete** when nothing is asserted wrongly but something the source states
is unknown to FinSight, **wrong** when any dimension is asserted wrongly, and
**missing** when no fact was produced. Unknown is never scored as wrong, and a
wrong fact is reported separately according to whether it carried a review
flag. Figures FinSight attached to the wrong row, and values it read under a
known row name that the row never printed, are counted as well -- a fabricated
figure is the failure this system exists to prevent, so it must be visible even
when no golden fact is paired with it.

One dimension the scorer does **not** check is the measure itself. Golden rows
record the label as printed, not a taxonomy key, so a figure read correctly and
filed under the wrong canonical metric ("Operating expenses to NTI" as operating
expenses) scores as found. At the end of Phase 21 every distinct label→metric
pairing among found golden facts was read by eye (371 pairings); that audit, not
the scorer, is the evidence for the measure.

---

## Three-mode experiment

What each system may read is fixed and enforced in code:

| | Allowed to read | Enforced by |
|---|---|---|
| **A. Baseline** | the PDF text layer, nothing else | never renders a page |
| **B. Visual-only** | rendered page images only | `app/core/modes.py` blocks native text except the ingestion probe |
| **C. Hybrid** | images, plus the text layer as corroboration | corroboration scope permitted |

The baseline is given its **strongest** form: words regrouped by y-coordinate
rather than trusted in content-stream order. It is not a straw man.

### RIL Q1 FY2026-27 media release (34 pages, 85-fact key)

| | A. Baseline | B. Visual-only | C. Hybrid |
|---|---|---|---|
| Facts | 87 | 431 | 432 |
| Numeric facts | — | 428 | 428 |
| Tables / cells | 0 / 0 | 30 / 1,663 | 30 / 1,663 |
| Bounding boxes | **0** | 431 | 432 |
| Provenance coverage | **0%** | **100%** | **100%** |
| Facts with a period | 87 | 405 | 406 |
| Facts with a unit | 4 | 372 | 372 |
| Entities discovered | — | 8 | 8 |
| **Precision** | — | **0.988** | **0.988** |
| **Recall** | — | **0.988** | **0.988** |
| **F1** | — | **0.988** | **0.988** |
| Matched | — | 84 / 85 | 84 / 85 |

The baseline recovers 87 numbers but cannot attach a unit to 83 of them and
cannot point at a single one on the page. It has no table structure, so it
cannot tell which column a figure belongs to — which is what a period is.

### RIL image-only (7 pages, no text layer, 81-fact key)

| | A. Baseline | B. Visual-only | C. Hybrid |
|---|---|---|---|
| Facts | **0** | 255 | 255 |
| Pages with no text | 7 / 7 | — | — |
| Tables / cells | 0 / 0 | 9 / 607 | 9 / 607 |
| Provenance coverage | — | **100%** | **100%** |
| **F1** | — | **0.988** | **0.988** |
| Matched | — | 80 / 81 | 80 / 81 |

The baseline extracts nothing at all. This is the case the architecture exists
for.

### Apple FY25 Q3 statements (3 pages, no key)

| | A. Baseline | B. Visual-only | C. Hybrid |
|---|---|---|---|
| Facts | 23 | 56 | 56 |
| Tables / cells | 0 / 0 | 5 / 363 | 5 / 363 |
| Bounding boxes | **0** | 56 | 56 |
| Facts with a unit | 2 | 56 | 56 |
| Currency resolved to USD | — | 52 / 56 | 52 / 56 |
| Facts with a period | 11 | **56** | **56** |
| Entities | — | 2 | 2 |

No accuracy is claimed: there is no answer key for this document.

---

## Generalization: what transferred and what did not

Processing a filer the system had never seen is the real test.

| Component | Transferred? | Evidence on the Apple filing |
|---|---|---|
| Ingestion | Yes | 3/3 pages validated and stored |
| Rendering | Yes | 3 pages at 200 dpi |
| OCR | Yes | 200 elements on page 1, 128 of them numeric |
| Layout | Yes | regions typed; 0 facts missing a region |
| Table reconstruction | Yes | 5 tables, 363 cells |
| Financial extraction | Yes | 56 facts, every one matched to a taxonomy metric |
| Entity model | Yes | `Apple Inc.` + `Consolidated`, discovered not configured |
| Units | Yes | "In millions" read from the head; share counts correctly `thousand` |
| Currency | Yes | USD on 52 of 56, from the dollar sign; no Indian-magnitude heuristic involved |
| Provenance | Yes | 100% of facts carry a bounding box |
| Validation | Yes | 4 checks ran |
| Export | Yes | JSON and CSV produced |
| Period attribution | Yes | **56 of 56**, each checked by hand |
| Fiscal calendar | Yes | September year end derived, never written in the filing |

### Why period attribution needed a hierarchy, precisely

US statements set their column heads across three lines, with banners spanning
column pairs, and print the whole head *above* the band of figures that layout
analysis cuts as the table:

```
        Three Months Ended            Nine Months Ended
      June 28,      June 29,        June 28,      June 29,
        2025          2024            2025          2024
```

Five separate problems, all now fixed and each fixed generically:

1. **A day was being read as a fiscal year.** `Three Months Ended June 28,` —
   with the year on the next line — resolved to *fiscal year 2028*, and the
   document reported a fabricated period `9M FY29`. **Fixed:** a bare two-digit
   number after a month name is a day unless a day was already captured.
2. **Column detection welded the four value columns into one.** Whitespace
   projection failed because share-count rows (`15,410,332`) reach across the
   gutters that dollar rows leave. **Fixed:** when whitespace under-segments,
   columns are taken from right-edge alignment clustering, selected by matching
   the width the data implies. Facts rose from 41 to 56.
3. **The head was flattened to a string before being parsed.** A head spread
   over three rows only parses when the fragments happen to land in a
   parseable order. **Fixed:** the head is read as a *stack*. Each level takes
   the fragment whose box overlaps that column's band most, a fragment
   reaching across two or more bands is treated as a banner shared by all of
   them, and a year on its own row is rejoined to the date above it.
4. **The head was outside the table.** Layout analysis cuts a table region at
   the first row of figures, so on the balance sheet and the cash-flow
   statement the entire head sat above the cut and every column came out
   undated. **Fixed:** the reconstruction walks upward from the top of the
   band, taking each line that lies within the table's horizontal extent and
   reads as a header fragment, and stopping at the first that does not. A line
   beginning inside the row-label column is a page title addressing the whole
   page, not a banner, and is not taken.
5. **The fiscal year end is never written in the filing.** Apple's September
   year end appears nowhere as text. **Fixed** — by arithmetic rather than by
   pattern-matching prose: a nine-month column closing on the same date as a
   three-month column is the year to date, because that is what an interim
   statement's cumulative column is. Nine months of the year having closed on
   28 June puts the year end three months later. The shorter column ending on
   the same date is what makes this an observation rather than an assumption.
   Contradictory evidence yields no calendar at all.

**Where the evidence is absent, no fiscal year is invented.** A filing that
prints exact dates and never evidences its year end keeps the dates and drops
the label: `3M ended 2025-06-28`, `fiscal_year = null`, confidence reduced. An
explicitly written label (`FY26`, `Q1 FY27`) is the document's own statement
and always survives — but the calendar dates it spans, which genuinely are
unknown, are left unset rather than guessed.

### Every Apple period, checked by hand

All 56 facts now carry a period. Each of the six distinct column heads was
compared against the source PDF:

| Statement | Column head, as printed | Resolved | Correct? |
|---|---|---|---|
| Operations | `Three Months Ended` / `June 28,` / `2025` | `Q3 FY25` | yes |
| Operations | `Three Months Ended` / `June 29,` / `2024` | `Q3 FY24` | yes |
| Operations | `Nine Months Ended` / `June 28,` / `2025` | `9M FY25` | yes |
| Operations | `Nine Months Ended` / `June 29,` / `2024` | `9M FY24` | yes |
| Balance sheet | `June 28,` / `2025` | `As at 2025-06-28` | yes |
| Balance sheet | `September 28,` / `2024` | `As at 2024-09-28` | yes |
| Cash flows | `Nine Months Ended` / `June 28,` / `2025` | `9M FY25` | yes |
| Cash flows | `June 29,` / `2024` | `9M FY24` | yes |

Apple's Q3 FY2025 did end on 28 June 2025 and its FY2024 closed on 28
September 2024, so the fiscal labels and the balance-sheet instants are both
right. Two properties are worth calling out separately:

- the **balance sheet columns stayed instants**. Nothing stamped a quarter
  onto a stock figure, which is the failure a date-plus-nearby-banner rule
  makes and which no later stage could detect;
- the **cash-flow comparative inherited nine months, not three**. Its banner
  was typeset too narrow to reach it, so the span came from the sibling column
  under the same head — and only because no other column had already claimed
  that period.

This is measured on one additional filer. It is evidence that the mechanism is
geometric rather than tuned to one document; it is not a claim about every
statement layout in existence.

---

## Scanner-degraded stress test

Same real pages, real scanner artefacts, same answer key.

| | Clean rasterisation | Scanner-degraded |
|---|---|---|
| File size | 2,513,479 B | 1,444,989 B |
| Extractable characters | 0 | 0 |
| Facts | 255 | 251 |
| Tables / cells | 9 / 607 | 8 / 613 |
| Facts with a period | 255 | 251 |
| Bounding boxes | 255 (100%) | 251 (100%) |
| Mean OCR confidence | 0.783 | 0.773 |
| Flagged for review | 25 | **45** |
| Validation warnings | 6 | **24** |
| Pages measured as degraded | **0 / 7** | **7 / 7** |
| **Precision** | **0.988** | **0.817** |
| **Recall** | **0.988** | **0.605** |
| **F1** | **0.988** | **0.695** |
| Matched | 80 / 81 | **49 / 81** |

**Read this honestly.** The structural machinery survives: tables are still
reconstructed, columns still resolve to periods, units and percentages are
still typed, and every figure still carries a bounding box. Character
recognition does not survive: about a third of the spot-checked figures come
back wrong. Degradation appears as declared uncertainty rather than as
confident wrong answers — review flags nearly double and validation warnings
quadruple — but that is not a substitute for a clean scan.

### Measuring how a page was captured

Every rendered page is measured before it is read — skew, noise, ink-to-paper
contrast, illumination drift, stroke sharpness, and the effective height of a
text row in pixels. There is no model file and no training set: each signal is
a statistic over the pixels with a stated meaning, so a page that scores badly
can be *explained*.

| Document | Pages | Flagged | Defects found |
|---|---|---|---|
| RIL media release | 34 | 0 | — |
| RIL image-only | 7 | 0 | — |
| Apple statements | 3 | 0 | — |
| RIL scanner-degraded | 7 | **7** | blur ×7, low contrast ×7, skew ×3 |

Across 44 clean pages of three different documents not one is flagged; all
seven degraded pages are, with the defects they actually have.

Two thresholds are set from measurement rather than from theory, and the
reasoning is worth stating because it cuts against the obvious choice:

- **Effective text height** fires below 16px, not below the recogniser's
  nominal 48px training height. The benchmark filing's dense statement pages
  measure 19–21px and read at F1 0.988, so type that size is demonstrably
  fine; a threshold of 22px flagged a third of a clean document.
- **Uneven illumination** is measured and reported but deliberately *not*
  actionable. On this benchmark it cannot separate a lighting gradient from a
  printed design: the media release's coloured masthead measures 0.15–0.16,
  higher than the deliberately vignetted scan of those same pages at
  0.12–0.13, and fitting a plane to the paper level reverses the ordering
  rather than separating them. A signal that cannot tell the defect from the
  design must not drive a transformation.

### Does preprocessing help?

The obvious next step is to repair the defects and re-read the page. It was
built, run and measured, and **it does not work** — so it was not shipped.

Twelve variants were run through the same OCR engine on the two worst pages of
the degraded fixture, scored by total confidence mass (the sum of per-element
confidences, which rewards both finding more text and recognising it better):

| Variant | Page 3 | Page 5 |
|---|---|---|
| original | — | — |
| unsharp ×0.6 | −0.1% | +0.5% |
| unsharp ×1.2 | +0.7% | +0.8% |
| upscale ×1.5 | −0.2% | **+1.3%** |
| upscale ×2.0 | −0.3% | +0.9% |
| upscale ×1.5 + unsharp | +0.8% | +0.1% |
| upscale ×2.0 + unsharp | +0.0% | +0.7% |
| CLAHE | −0.8% | +0.9% |
| CLAHE + unsharp | −0.4% | +0.4% |
| Otsu threshold | +0.1% | −0.4% |
| adaptive threshold | −0.4% | +0.6% |
| upscale ×1.5 + adaptive | **−2.0%** | **+1.3%** |

The best result is +1.3%, inside run-to-run variation; several are negative.
Wired into the pipeline behind a selective, per-defect repair pass and a
3%-margin comparison, **every candidate on every page was rejected**, and the
second OCR pass cost 134.7 s against 76.6 s — 1.8× the wall clock for nothing.

The element counts explain why. They barely move: 80 → 80 on page 3, 85 → 85–86
on page 5. The detector is finding the *same text regions* either way. What the
degradation destroyed is stroke-level detail inside those boxes — which
characters the recogniser sees — and no filter puts back information that 150
dpi and JPEG quality 55 already threw away.

So the repair pass was deleted and the simpler architecture kept. What remains
is the measurement, which is useful on its own terms: it tells a reader why a
page read badly instead of leaving them to guess. This is a result about *this*
fixture and this recogniser, not a general claim that preprocessing never helps.

---

## Failure breakdown

Where attribution falls short, by category, for each document in visual-only
mode. A fact may appear under more than one heading, so these are not a
partition.

| Category | RIL (431) | Image-only (255) | Degraded (251) | Apple (56) |
|---|---|---|---|---|
| OCR — low confidence (< 0.75) | 147 | 72 | 99 | 16 |
| Layout — no region | 0 | 0 | 0 | 0 |
| Table structure — no table | 0 | 0 | 0 | 0 |
| Metric semantics — unmatched label | 0 | 0 | 0 | 0 |
| Entity — unattributed | 0 | 0 | 0 | 0 |
| Period — unattributed | 26 | 0 | 0 | 0 |
| Unit — missing | 59 | 15 | 12 | 0 |
| Unit — inherited, not declared | 295 | 196 | 200 | 51 |
| Currency — unknown on a monetary fact | 4 | 4 | 0 | 0 |
| Currency — inferred, not read | 0 | 0 | 0 | 0 |
| Validation — failed or warned | 31 | 18 | 40 | 6 |
| Validation — no rule applicable | 165 | 72 | 130 | 50 |
| Provenance — missing bounding box | 0 | 0 | 0 | 0 |
| Flagged for human review | 64 | 25 | 45 | 6 |

Three things stand out:

* **Provenance and entity attribution never fail.** Every fact on every
  document resolves to a bounding box and a reporting entity.
* **Period attribution no longer fails on the unfamiliar layout.** Apple went
  from 46 of 56 facts undated to none, and the image-only and degraded
  documents date every fact they extract. The 26 undated facts on the full
  filing are narrative figures in prose that names no period, not table cells.
* **The review rate tracks genuine difficulty rather than missing metadata.**
  It is 10% on the clean image-only document, 15% on the full filing, 11% on
  Apple, and 18% on the degraded scan — where it is driven by low OCR
  confidence, which is the real problem there.

---

## Performance profile

CPU-only laptop, one document at a time, nothing else running. **Cold** means
the OCR cache was cleared first; **warm** means it was populated.

### Where the time goes (cold, VISUAL_ONLY)

| Document | Pages | Total | Render | OCR | Layout | Tables | Charts | Extraction | Validation | Analysis |
|---|---|---|---|---|---|---|---|---|---|---|
| RIL media release | 34 | 268.6 s | 8.7 s | 322.9 s | 68.1 s | 0.29 s | 9.9 s | 56.4 s | 0.01 s | 0.04 s |
| RIL image-only | 7 | 41.5 s | 2.0 s | 59.0 s | 13.5 s | 0.05 s | 0.00 s | 0.06 s | 0.01 s | 0.03 s |
| RIL scanner-degraded | 7 | 47.0 s | 2.0 s | 64.2 s | 20.2 s | 0.05 s | 0.00 s | 0.05 s | 0.00 s | 0.02 s |
| Apple statements | 3 | 14.9 s | 0.6 s | 22.2 s | 0.9 s | 0.03 s | 0.00 s | 0.90 s | 0.00 s | 0.00 s |

Render, OCR and layout run interleaved in one parallel loop, so the figures for
those three are **time spent in each phase summed across worker threads** and
overlap each other — which is why OCR alone (322.9 s) exceeds the wall clock
(268.6 s). Everything after the page loop is sequential and its numbers are
plain wall time.

**OCR dominates completely.** On the 34-page filing it accounts for roughly
four fifths of the work; rendering is 9 s, and the entire semantic layer —
entities, table semantics, derived metrics, the relationship graph, duplicate
and conflict handling, risks, insights, anomalies — costs **40 milliseconds**.
Per-page capture measurement is part of the page loop and costs about 30 ms a
page, well inside OCR's shadow.

### Concurrency, and what the timings below describe

The per-page loop's width is now chosen from the machine
(`settings.resolved_page_workers`: two thirds of the logical cores, capped at
eight; eight on the 12-core machine these were measured on) with two ONNX
intra-op threads per OCR session. That was picked by measurement, over 24 pages
of a real 165-page report with the OCR cache bypassed:

| page_workers | intra threads | wall | s/page | facts | tables |
|---|---|---|---|---|---|
| 2 | 4 | 363.3 s | 15.14 | 119 | 38 |
| 4 | 3 | 291.9 s | 12.16 | 119 | 38 |
| 6 | 2 | 276.0 s | 11.50 | 119 | 38 |
| 4 | 4 | 281.0 s | 11.71 | 119 | 38 |
| **8** | **2** | **255.1 s** | **10.63** | 119 | 38 |

**29.8% faster than the previous 2/4 default, with identical extraction** —
119 facts and 38 tables in every configuration, as it must be: pages are
independent, so concurrency cannot change what is read.

Two honest caveats. Nothing beyond eight workers was measured, so eight is the
best of what was tried rather than a proven optimum. And **the full 165-page
document has never been benchmarked end to end** — two attempts were made and
both were discarded, the first for running alongside the test suite and the
second for being stopped before it finished. There is therefore no before/after
figure for a document that size, and none is claimed.

> **The timings in the table below predate this change.** They were measured at
> `page_workers=2, intra=4` and describe the old configuration, not the shipped
> one. They are kept because the *ratios* they show — OCR dominating, the cache
> separating cold from warm — still hold; the absolute seconds do not.

Worker count is additionally capped by free memory (~400 MB per worker, floor of
two). The core-count rule alone asked for eight workers on a machine with 2.2 GB
free, and the run did not fail — it thrashed: one batch of eight pages took 35
minutes where the median page took 59 seconds, and a 75-page document that had
taken 14 minutes took 48, with no error reported anywhere.

### The OCR levers that were measured and rejected

OCR is ~90% of wall clock, so it is where a speed win would have to come from.
Every parameter that could reduce it was swept. None shipped.

**Detection resolution (`det_limit_side_len`).** Genuinely faster on most pages
— 0.78x–0.83x at side=960 — but on one deck's KPI page, side=1280 lost this
string:

> `R1 Cr = R10 million; L Cr = Lakh Crore = 1 trllion equivalen`

That is the document's unit declaration. Losing it does not cost a fact; it
costs every figure on the page its scale. Thirteen strings were lost on that
page and nine gained, and the page was 1.06x *slower* anyway. Rejected.

**Render DPI.** Almost no effect, because the detector downscales to
`det_limit_side_len` before doing anything:

| Page | dpi=200 | dpi=160 | dpi=130 |
|---|---|---|---|
| StanChart p25 | 17.20 s | 17.51 s (1.02x) | 17.68 s (1.03x) |
| IDFC p18 | 5.50 s | 5.41 s (0.98x) | 5.47 s (0.98x) |
| HDFC p3 | 14.99 s | 16.09 s (1.07x) | 15.32 s (1.02x) |

Some combinations were slower, and lower DPI degrades every downstream consumer
that works on the render — crops, cell geometry, the page canvas. Rejected.

**Per-thread OCR engines.** The oversubscription hypothesis, disproved:

| Threads | Shared engine | Per-thread engines |
|---|---|---|
| 1 | 114.7 s | 114.7 s |
| 2 | **53.8 s** | 161.5 s |
| 4 | **48.8 s** | 152.1 s |
| 6 | **48.2 s** | 196.9 s |

Roughly three times worse, and worsening with thread count: ONNX Runtime
already parallelises internally, so extra sessions only multiply the arenas.
The shared engine was kept.

**The honest conclusion.** There is no safe OCR speed win at this parameter
level. ~7 pages/minute is the ceiling on a 12-core CPU, and beating it needs a
different recogniser or a GPU. The one place real time was recovered is chart
detection, where the radial circle search now runs at a bounded resolution:
3.3x over the corpus's 274 chart regions (65.2 s → 19.9 s), with classifications
identical but for one false positive removed.

### Cold versus warm

| Document | Mode | Cold | Warm | Cold ms/page |
|---|---|---|---|---|
| RIL media release | HYBRID | 385.1 s | 319.5 s | 11,328 |
| RIL media release | VISUAL_ONLY | 268.6 s | 238.6 s | 7,899 |
| RIL image-only | HYBRID | 45.3 s | 43.5 s | 6,476 |
| RIL image-only | VISUAL_ONLY | 41.5 s | 40.4 s | 5,934 |
| RIL scanner-degraded | VISUAL_ONLY | 47.0 s | 16.5 s | 6,719 |
| Apple statements | VISUAL_ONLY | 14.9 s | 13.1 s | 4,952 |

The content-hash OCR cache is what separates the two. On short documents the
difference is inside run-to-run noise — the cache only pays for itself when
there is enough OCR to skip.

Visual-only is consistently *faster* than hybrid because it skips text-layer
corroboration entirely.


---

## Reproducing

```bash
cd backend
python cli.py ingest ../data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf
python cli.py evaluate <document_id>
```

The report includes the intrinsic metrics, the accuracy scoring against the
checked-in key, and the baseline comparison. To run only the baseline against
any PDF:

```bash
python cli.py baseline <file.pdf> --document-id <processed_document_id>
```

## Adding a benchmark document

1. Put the PDF in `data/benchmark/`.
2. Create `data/benchmark/<same-stem>.truth.json`:

```json
{
  "document": "<filename>.pdf",
  "facts": [
    {
      "canonical_metric": "gross_revenue",
      "entity": "Consolidated",
      "period": "Q1 FY27",
      "value": 340257,
      "unit": "crore",
      "currency": "INR",
      "page": 1
    }
  ]
}
```

3. Ingest and evaluate. The key is discovered automatically by filename.

Read the values from the document itself. A key derived from pipeline output
measures nothing.

## Test suite

226 automated tests (`cd backend && python -m pytest`), covering:

| Area | Coverage |
|---|---|
| Normalisation | number forms, accounting negatives, Indian digit grouping, unit/currency detection, "units are never invented", context cannot re-type a figure, all period formats, fiscal ≠ calendar year, non-March year-ends |
| Taxonomy | real labels from the document, near-synonyms staying distinct, unknown labels correctly rejected |
| Table extraction | one fact per period cell, unit inheritance, margin rows not given monetary units, reported vs calculated growth, margin change in points not relative percent, provenance on every fact |
| Deduplication | agreeing duplicates raise confidence; conflicting duplicates are both kept and flagged |
| Narrative | sentence facts, negative growth, guidance detection, safe-harbour boilerplate excluded, topic classification |
| Validation | every rule family, tolerance behaviour, cross-entity mixing refused, unit conversion, incomparable currencies refused, one result per scope, a broken rule not killing the run |
| Confidence | band thresholds, OCR floor, corroboration, failed validation forcing review |
| Ingestion | path traversal, Windows reserved names, size limits, content-based typing, corrupt and empty files, image-only detection |
| Rendering | caching, native-resolution loading, DPI-to-pixel scale, out-of-range pages |
| Pipeline | full end-to-end run, plus corrupt PDF, non-PDF, missing file and OCR-engine-failure all reported honestly |
| Persistence | full round trip, reprocess replacing rather than duplicating |
| API | every endpoint, upload validation, duplicate detection, provenance resolution, exports, review workflow preserving the extracted value |
| Workers | job success and failure both recorded on the job *and* the document |
| Evaluation | intrinsic metrics, accuracy scoring, wrong values counted rather than hidden, baseline behaviour on an image-only PDF |
| Visual-only | the native-text boundary (including inside worker threads), an image-only PDF processed end to end, provenance resolving to rendered-image coordinates, hybrid and visual-only remaining distinct — see [VISUAL_ONLY.md](VISUAL_ONLY.md) |
