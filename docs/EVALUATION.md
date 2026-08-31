# Evaluation

> **Note.** The figures below are for the production **HYBRID** mode. The same
> document processed in strict **VISUAL_ONLY** mode — with the PDF text layer
> hard-blocked from reaching extraction — scores identically (F1 1.00) and is
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

## Results — RIL Q1 FY2026-27 media release

34 pages, born-digital, processed on an Intel i7-1355U with no GPU.

### Accuracy

| Metric | Result |
|---|---|
| Ground-truth facts | 85 |
| Matched | **85** |
| Wrong value | 0 |
| Missing | 0 |
| **Precision** | **1.00** |
| **Recall** | **1.00** |
| **F1** | **1.00** |
| Unit accuracy | 1.00 |
| Provenance page accuracy | 0.988 |

Provenance page accuracy is 0.988 rather than 1.0 because one operational figure
was matched from its appearance on an adjacent page — the value is right and the
box is right, but the key names the other occurrence.

### Intrinsic metrics

| Area | Metric | Result |
|---|---|---|
| **Pages** | processed / total | 34 / 34 (coverage 1.00) |
| | failed | 0 |
| **OCR** | elements extracted | 2,936 |
| | mean confidence | 0.842 |
| | worst page confidence | 0.761 |
| | text-layer agreement | **0.932** |
| **Tables** | reconstructed | 30 |
| | mean confidence | 0.942 |
| | cells | 1,663 (1,195 numeric) |
| | numeric parse rate | **1.00** |
| | with resolved period columns | 19 |
| | continuations linked | 4 |
| **Normalisation** | unit resolved | 0.93 |
| | currency resolved | 0.78 |
| | period resolved | **0.93** |
| | entity resolved | **1.00** |
| | growth linked | 0.24 |
| **Provenance** | facts with a record | **1.00** |
| | records with a bounding box | **1.00** |
| **Confidence** | HIGH / MEDIUM / LOW | 326 / 59 / 12 |
| **Validation** | checks run | 119 |
| | passed / warnings / **failures** | 106 / 4 / **0** |
| | pass rate | 0.891 |
| **Review** | queue | 49 (12% of facts) |
| **Performance** | total, cold (empty OCR cache) | 321 s |
| | per page, cold | 9.4 s |
| | total, OCR cache warm | 131 s |

Reading the softer numbers honestly:

- **currency resolved 0.78** — the remaining 22% are percentages, ratios and
  counts, which correctly have no currency. This is the metric behaving as
  designed, not a gap.
- **growth linked 0.24** — growth is only attached where the document supplies a
  comparison column or states a change in prose. Most cells are a single
  period's value with nothing to compare against, and inventing a comparison
  would be wrong.
- **validation pass rate 0.891** — the 4 warnings and 9 review flags are real
  observations about the document (segment sums that legitimately differ from
  consolidated totals because of eliminations, and figures whose magnitude the
  document never declared), not extraction errors.

### Validation by rule family

| Family | Passed | Warning | Requires review |
|---|---|---|---|
| growth | 41 | — | — |
| margin | 20 | 3 | — |
| derived | 8 | — | — |
| ratio | 4 | — | — |
| aggregation | 33 | 1 | — |
| consistency | — | — | 9 |

All 41 reported growth rates on the document recompute correctly from the two
period values. The `derived` family confirms PBT − Tax = PAT and Debt − Cash =
Net Debt across every period.

## Baseline comparison: does visual processing actually help?

This exists to *test* the project's central claim rather than assert it. Both
baselines use the same taxonomy, number parser and period parser as the real
pipeline — only the visual stages are removed.

**`naive`** reads the PDF text layer in its native content-stream order. This is
what a plain text extractor gives you.

**`line_reconstructed`** is a deliberately *strong* baseline: words are pulled
with their coordinates and grouped into visual lines by y-position, so labels and
figures are correctly paired. This already borrows one geometric idea from the
visual pipeline, and it is included so the comparison cannot be accused of
beating a strawman. What it still cannot do is decide which *column* — and
therefore which period — a figure belongs to.

| | naive text | strong text | **visual pipeline** |
|---|---|---|---|
| Facts extracted | 1 | 78 | **397** |
| Distinct metrics | 1 | 32 | **43** |
| Tables reconstructed | 0 | 0 | **30** |
| Table cells | 0 | 0 | **2,291** |
| Facts with a unit | 1 | 4 | **369** |
| Facts with a currency | 0 | 0 | **309** |
| Facts with growth linkage | 0 | 0 | **96** |
| Facts with an entity | 0 | 0 | **397** |
| Facts with a source bounding box | 0 | 0 | **397** |
| Facts passing arithmetic validation | 0 | 0 | **179** |
| F1 against the hand-built key | **0** | **0.067** | **1.00** |
| Numeric tokens seen | 2,136 | 2,238 | — |
| Numbers seen but discarded | — | **423** | 0 |
| Runtime | 4.8 s | 2.6 s | 321 s cold / 131 s warm |

### What the numbers mean

**The naive baseline finds one usable fact.** This is the most striking result
and it is not a bug in the baseline: this document's content stream emits each
span separately, so "Gross Revenue" and "340,257" are not adjacent lines. Reading
a PDF's text in its native order simply does not recover table rows.

**The strong baseline gets to 78 facts and then hits a wall.** It pairs labels
with figures correctly, but with no column geometry it cannot tell which of the
four numbers on a row belongs to which period. It keeps the first and discards
**423 others** — precisely the comparative-period figures an analyst wants. It
labels every fact with whatever period the *page* mentions, which on a table with
four period columns is right at best one time in four.

**Units and currency are structural, not lexical.** The strong baseline resolves
a unit for 4 facts out of 78 and a currency for none, because `(₹ in crore)` sits
in a caption above the table — only the table structure connects that caption to
the figures beneath it. The visual pipeline resolves units for 384 facts and
currency for 316.

**Validation is only possible downstream of correct attribution.** 179 visual
facts passed an independent arithmetic check. The baselines pass none, because
recomputing a growth rate requires knowing that two numbers are the same metric,
same entity, adjacent periods — which is exactly what column geometry establishes.

**And on a scanned document both baselines return zero.** The visual pipeline is
unaffected, because it starts from pixels. The test suite verifies this directly
against a generated image-only PDF.

The visual pipeline costs roughly 130× the runtime of a text baseline on a cold
run — the honest price of running OCR over 34 rendered pages. That buys a 5×
increase in facts, plus units, currency, entity attribution, period attribution,
growth linkage, arithmetic validation and pixel-level provenance — none of which
the text approach can provide at any speed.

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
