# Architecture

## The core problem

A financial PDF is a *visual* document. Consider one row of a results table:

```
1   Gross Revenue   340,257   325,290   273,252   24.5   1,175,919
```

Nothing in that sequence of glyphs says that `340,257` belongs to Q1 FY27,
that `24.5` is a year-on-year percentage rather than another rupee amount, or
that the whole row is denominated in crore because a caption three lines above
said `(₹ in crore)`. All of that is carried by **position**: which column a
figure sits under, which header spans it, which section heading governs the
table, which caption precedes it.

A PDF text extractor discards exactly this information. It returns an ordered
list of glyph placements, and on this document it does not even keep a row
together — the content stream emits each span separately, so the label "Gross
Revenue" and the number "340,257" arrive as unrelated lines.

FinSight therefore treats **rendered page images as the canonical
representation**. Every page is rasterised; OCR reads the pixels and keeps a
bounding box for every token; layout analysis recovers columns, ruling lines and
regions from geometry. The PDF text layer is kept as a *corroborating* channel,
never as the foundation — which is also what makes scanned documents work
through the identical path.

## Pipeline

```
                      PDF upload
                          │
                    ┌─────▼──────┐
                    │  VALIDATE  │  magic bytes, encryption, page count,
                    └─────┬──────┘  text-layer coverage → scanned?
                          │
                    ┌─────▼──────┐
                    │   RENDER   │  PyMuPDF → PNG @ 200 dpi, cached
                    └─────┬──────┘
                          │  ← canonical representation from here on
              ┌───────────┼───────────┐
        ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
        │    OCR    │ │  TEXT  │ │    CV    │  ruling lines, ink density,
        │ (pixels)  │ │ LAYER  │ │ SIGNALS  │  colour, whitespace valleys
        └─────┬─────┘ └───┬────┘ └────┬─────┘
              └───────────┼───────────┘
                    ┌─────▼──────┐
                    │ CORROBORATE│  agreement raises confidence;
                    └─────┬──────┘  recovers currency glyphs
                          │
                    ┌─────▼──────┐
                    │   LAYOUT   │  typed regions + reading order
                    └─────┬──────┘
                          │
                    ┌─────▼──────┐
                    │  ASSEMBLE  │  cross-page: chrome, sections, entities
                    └─────┬──────┘
              ┌───────────┼───────────┐
        ┌─────▼─────┐           ┌─────▼─────┐
        │  TABLES   │           │  CHARTS   │
        └─────┬─────┘           └─────┬─────┘
              └───────────┬───────────┘
                    ┌─────▼──────┐
                    │ EXTRACTION │  taxonomy match → facts, commentary, guidance
                    └─────┬──────┘
                    ┌─────▼──────┐
                    │ NORMALISE  │  numbers, units, currency, periods, growth
                    └─────┬──────┘
                    ┌─────▼──────┐
                    │  VALIDATE  │  growth, margins, identities, ratios, totals
                    └─────┬──────┘
                    ┌─────▼──────┐
                    │ CONFIDENCE │  6 evidence channels → band
                    └─────┬──────┘
                    ┌─────▼──────┐
                    │ PROVENANCE │  page, region, cell, bbox, crop URL
                    └─────┬──────┘
                    ┌─────▼──────┐
                    │  DATABASE  │
                    └─────┬──────┘
             ┌────────────┼────────────┐
        ┌────▼───┐   ┌────▼───┐   ┌────▼────┐
        │DASHBOARD│  │  API   │   │ EXPORT  │
        └────┬───┘   └────────┘   └─────────┘
        ┌────▼─────┐
        │  REVIEW  │  corrections stored *alongside* the extraction
        └──────────┘
```

## Processing modes and the native-text boundary

The visual-first claim is only meaningful if it can be enforced and audited, so
it is expressed in code rather than in a comment.

| Mode | What extraction may see |
|---|---|
| `VISUAL_ONLY` | The rendered page image, OCR text and boxes, and the layout, tables and charts derived from them. Any native-PDF-text read that feeds extraction raises `NativeTextAccessViolation`. **No fallback** — an unrecoverable field is left absent or flagged, never filled in from glyphs. |
| `HYBRID` (default) | The same visual pipeline, plus the text layer as an independent corroborating channel. |

`app/core/modes.py` gates every native-text read through `guard_native_text`,
which classifies each call site by scope: `INGESTION_VALIDATION` (metadata only,
permitted everywhere), `CORROBORATION` (feeds layout typography and currency
recovery, so **blocked** in visual-only) and `EVALUATION_BASELINE` (measurement,
permitted everywhere). Every access is recorded in a per-run ledger stored on the
`processing_runs` row and returned by `GET /documents/{id}/status`, so a run can
be audited afterwards rather than trusted.

Because the pipeline analyses pages on a `ThreadPoolExecutor` and `ContextVar`s
do not propagate into worker threads, the mode is copied into each task
explicitly — without that the guard would be inactive in exactly the threads
doing the work. A test pins this.

Measured outcome: on the acceptance document the two modes score identically
(F1 1.00), and on a PDF containing only rasterised images `VISUAL_ONLY` extracts
251 facts where both text baselines extract zero. The full audit is in
[VISUAL_ONLY.md](VISUAL_ONLY.md).

## Technology choices

Every choice below was made against the actual target machine: Windows 11,
Intel i7-1355U, **no CUDA GPU**, 16 GB RAM, Python 3.13, and **no paid API key**.

### PDF rendering — PyMuPDF

Pip-installable with no system dependency (Poppler and Ghostscript are absent
on the target machine and would need separate installers). Renders an A4 page
at 200 dpi in ~0.1 s, exposes page geometry, vector drawings and the text layer
through one library, and does not load the whole document into memory.

Pages are rendered one at a time straight to disk and cached under a key that
includes the DPI and format, so changing render settings transparently
invalidates old renders instead of silently reusing them.

### OCR — RapidOCR (PP-OCR exported to ONNX)

Selected after testing the practical options against this machine *and* against
financial-page characteristics.

| Engine | Verdict |
|---|---|
| **RapidOCR / PP-OCR ONNX** | **Chosen.** Pip-installable with models bundled (~12 MB), no system binary, no GPU, works on Python 3.13. PP-OCR's DBNet detector handles the dense, small, right-aligned numeric columns of financial tables and returns quadrilaterals, which is exactly what layout and table reconstruction need. Measured ~13 s/page at 200 dpi with every figure on the consolidated table read correctly. |
| Tesseract | Needs a system installer that is not present. Its LSTM line model is also weaker on tightly-spaced numeric columns. Kept as a working second implementation of the engine interface (`app/ocr/tesseract_engine.py`) so the abstraction is demonstrably real; it reports itself unavailable rather than degrading silently. |
| PaddleOCR (native) | No `paddlepaddle` wheel for Python 3.13 on Windows. The ONNX export used here is the same model family. |
| EasyOCR | Would pull the full torch stack for detection quality below PP-OCR on printed tabular text. |

Two tuning decisions matter. The default config resizes so the *minimum* side is
736 px, which would downscale a 1654×2339 page and destroy small table text;
FinSight sets `limit_type: max` at 1600 instead. Angle classification is
disabled, since results releases are not rotated, saving roughly 20% of runtime.

**Known limitation, handled explicitly:** the PP-OCR recognition charset has no
rupee sign, so currency glyphs are dropped from recognised text. In hybrid mode
the `app/ocr/textlayer.py` channel recovers them (measured: 8 symbols recovered
on page 1 alone). In visual-only mode that channel is blocked, so the currency is
inferred from India-only magnitude words read from the pixels — "crore" and
"lakh" — and flagged as `currency_inferred` with a small confidence penalty, so a
deduction is never mistaken for a symbol that was read. A scanned rupee document
that never spells out a currency word leaves the currency `UNKNOWN`.

Results are cached on disk keyed by **content hash** + page + render signature +
OCR signature. Identical pixels always produce identical OCR, so re-ingesting or
reprocessing a document reuses several minutes of inference.

### Layout — heuristic detector, behind an engine interface

No pretrained layout model is used. On a CPU-only machine a
DocLayout/LayoutLMv3-class model would add gigabytes of download and seconds per
page for a modest gain on the clean, ruled documents this system targets. The
detector instead combines signals that are individually cheap and jointly
strong:

- **typography** — relative font size, weight, case (title vs heading vs body)
- **geometry** — position bands, indentation, line gaps, alignment
- **ruling lines** — horizontal and vertical rules recovered by directional
  morphology on the page image
- **numeric alignment** — repeated right-aligned numeric columns, the single
  most reliable indicator of a financial table
- **ink and colour** — non-text graphical blocks for charts and figures

Each region records which detector produced it, and the detector sits behind an
interface so a learned model can replace it without touching anything
downstream. Its limitations are stated below rather than hidden.

### Table reconstruction

Column boundaries are found in priority order:

1. **Vertical ruling lines** inside the band — exact, when the table is drawn
   with a grid.
2. **Whitespace valleys** in the horizontal ink projection — this is what makes
   borderless financial tables work, since the columns of a well-set table are
   separated by consistent vertical gutters even with no rules drawn.
3. **Numeric right-edge clustering** as a fallback.

Two refinements proved necessary on real documents. Banner rows (a section title
running the full width) are excluded from the projection: a single such row has
ink in every gutter and collapses three label columns into one. And *spanning*
header cells are attached to every column band they overlap, which is what lets
a statement column headed only `30th Jun'26` under a centred `Quarter Ended`
resolve to Q1 FY27.

Header rows are detected rather than assumed. Zero is a legitimate answer: a
fragment whose first row is plainly data — typical of a statement split by a
wide gap — has no header of its own, and inventing one would discard a row of
real values and label the columns with numbers. Such fragments are linked to
their parent table and inherit its column semantics.

### Chart understanding — deliberately conservative

Charts are detected from ink, colour and geometry, and classified into families
(bar, column, line, pie) from radial ink, rectangle detection and line
continuity. Titles, axis labels, legends, categories and printed data labels are
harvested from OCR.

**Values are read only when they are printed on the chart.** Deriving a number
from bar height against an axis is unreliable at this resolution and would
produce confident-looking financial figures with no basis in the document. So a
chart without data labels is stored with its text, marked
`values_extracted = false`, and its `notes` field states plainly why. Every
chart is flagged for review regardless, because even a well-read chart's
series-to-value mapping is positional and unverified.

### Financial taxonomy

~60 canonical metrics across revenue, earnings, margins, costs, per-share,
balance sheet, cash flow, returns and operational KPIs.

**Near-synonyms are deliberately not collapsed.** "Revenue", "Gross Revenue",
"Revenue from Operations" and "Net Revenue" are related but different measures,
so each keeps its own canonical key and they are linked through a `family`
instead. Merging them would silently corrupt any comparison. The same applies to
"Profit After Tax" versus "Profit After Tax and Share of Profit of Associates &
JVs" — a results table lists both, and collapsing them would make a
period-over-period comparison compare different things.

Matching is exact-alias first, then a trailing-qualifier retry (so
`ARPU (Rs/Sub/month)` matches), then noise-stripped, then fuzzy above a 0.82
threshold. A prefix match must cover ≥55% of the label — without that test the
6-character alias "income" claims "income tax relating to items that will not be
reclassified…". **Returning no match is a valid outcome**: an unrecognised row
produces no fact rather than a guessed one.

### Normalisation

Numbers keep everything the document said. A parsed value carries its
`raw_text`, whether it was negative because of parentheses, the magnitude word
(crore/lakh/million/billion/thousand), the currency, *and* a `base_value` in
base units as an **additional** field — never a replacement. A value reported in
crore stays a crore value.

Units are never invented: if the document does not say whether a figure is crore
or million, the magnitude stays `NONE` and `base_value` stays `None`. Context
(a caption, a column header) can *supply* a missing unit but never overrides one
stated on the token, and context is deliberately never consulted for the value
*type* — a nearby sentence mentioning "bps" must not re-type an unrelated
revenue figure.

Periods normalise `1Q FY27`, `Q1 FY2027`, `FY26`, `H1 FY26`, `9M FY26`,
`CY2025`, `quarter ended 30th June, 2026`, `Year Ended (Audited) 31st Mar'26`
and bare dates such as `30th Jun'26`. Fiscal and calendar years are kept
strictly distinct — FY26 ends 31 March 2026, CY2026 ends 31 December 2026 — and
the fiscal year-end month is *inferred from the document* rather than assumed,
so a December- or June-year-end filer works without configuration. A bare date
carries no periodicity of its own, so it resolves to a quarter only when a
spanning header supplies one; otherwise it is recorded as a point in time.

### Cross-page document reasoning

A release is not a bag of independent pages.

- **Page chrome detection.** A single page cannot tell a letterhead from a
  title. Across the document, text recurring at the same vertical position on
  ≥50% of pages is chrome and is retyped as header/footer — preserving its box
  and text. On the acceptance document this correctly reclassified 114 regions
  and stopped the registered-office block being parsed as a table.
- **Sections and entity attribution.** Headings cut sections that span pages,
  and the reporting entity is carried down onto every fact beneath. The subtle
  case that matters most: `CONSOLIDATED JIO PLATFORMS LIMITED ("JPL")` is *not*
  the group's consolidated result — it is a subsidiary's. Treating it as the
  group mixed a subsidiary's EBITDA with the group's revenue and manufactured
  eight false margin failures. The word "consolidated" now designates the group
  only when nothing else is named, and the document's own abbreviation (JPL,
  RRVL, O2C) becomes the entity label.
- **Narrative attribution.** A sentence that names a known entity is attributed
  to it rather than to its enclosing section, so "Robust O2C performance with
  EBITDA at ₹17,010 crore" on page 1 belongs to O2C, not to Consolidated.
- **Table continuations** are linked across a page boundary *and* within a page.

### Validation engine

Six rule families, all recomputing from extracted values:

| Family | Check |
|---|---|
| `growth` | reported % change vs `(current − prior)/prior × 100` |
| `margin` | reported margin vs numerator/denominator |
| `derived` | PBT − Tax = PAT; Debt − Cash = Net Debt |
| `ratio` | Net Debt / EBITDA, accepting both as-reported and annualised bases |
| `aggregation` | segment values vs the reported consolidated total |
| `consistency` | sign, plausible range, unit determinacy, period coverage |

Two invariants hold throughout. **The document is never corrected** — both the
reported and the calculated value are stored and the discrepancy is surfaced.
And **a check only runs when its inputs are genuinely comparable**: same entity,
period, currency and magnitude. Comparing a crore figure with a million figure
would manufacture false warnings, so it is refused instead (or converted
explicitly, with the conversion recorded).

A failing rule family is caught, recorded as a `*_rule_error` failure and the
other families still run — a bad rule never silently reduces coverage.

### Confidence scoring

A weighted blend of six independent evidence channels, not a number lifted from
the OCR engine:

| Channel | Weight | Question |
|---|---|---|
| `ocr` | 0.30 | how well were the characters read? |
| `semantic` | 0.25 | how confidently did the label map to a metric? |
| `structure` | 0.18 | how sound is the reconstructed grid? |
| `layout` | 0.12 | how sure are we of the region type? |
| `consistency` | 0.10 | did arithmetic validation agree? |
| `corroboration` | 0.05 | did the text layer or a duplicate agree? |

Hard floors apply: OCR below 0.5 caps the overall score at 0.55 however clean
everything else was, and a failed arithmetic check drives the fact into review
regardless. Bands are coarse — HIGH / MEDIUM / LOW / REQUIRES_REVIEW — because
reporting "0.8734" would imply a precision this evidence does not support.

Duplicate facts are used as evidence rather than noise: the same figure
appearing in a highlights table and again in the statements raises confidence
and records the pages. **Disagreeing duplicates are never silently resolved** —
both are kept and both are flagged.

### Provenance

Every fact stores document, page, region, table, cell, bounding box, the OCR
element ids and the original text. The API returns coordinates in both pixel and
**relative (0..1)** space, so the frontend overlay stays aligned at any zoom
without knowing the render DPI, and a crop of the exact source area is generated
on demand and cached.

## Data model

16 tables, relational and queryable — JSON columns are used only for genuinely
open-ended metadata (engine parameters, detector notes, confidence breakdowns),
never as the primary store.

```
documents ─┬─ pages
           ├─ processing_runs ── stage_runs
           ├─ ocr_elements
           ├─ regions
           ├─ tables ── table_cells
           ├─ charts
           ├─ financial_facts ── provenance
           ├─ commentary
           ├─ guidance
           ├─ validation_results
           └─ review_actions
jobs
```

Every child row carries `document_id`, so per-document queries never need
multi-level joins and cascade deletes are cheap. Types are PostgreSQL-compatible
throughout (`JSONVariant` maps to `JSONB` on PostgreSQL, `JSON` on SQLite);
switching is a matter of changing `FINSIGHT_DATABASE_URL`. Foreign keys are
enforced on SQLite via a `PRAGMA foreign_keys=ON` connect hook, which is off by
default.

## Concurrency and memory

Processing a 34-page document takes minutes, so it never runs inside an HTTP
request. Jobs are recorded in the database (so their state survives a restart
and is queryable) and executed by a small in-process thread pool. ONNX Runtime
releases the GIL during inference, so threads give real parallelism without the
memory cost of extra processes.

Celery/RQ were not used: both need a broker this deployment does not have, and
the workload is a handful of long CPU-bound jobs rather than high-throughput
messaging. The queue is a thin, replaceable seam — `submit`/`get_state` is all
the API depends on.

Pages are rendered and analysed one at a time and written straight to disk, so
peak memory stays at roughly one page bitmap per worker rather than the whole
document.

## Security

- Content-based file typing (magic bytes), not the declared MIME type
- Filename sanitisation stripping path components, Unicode tricks and Windows
  reserved names
- Streaming size enforcement that removes the partial file on rejection
- Per-document directories with a resolved-path assertion against traversal
- Uploads are never executed; internal filesystem paths are never exposed — the
  API serves opaque ids
- Structured API input validation, with domain errors mapped to status codes and
  internals never leaked to the client

## Limitations

Stated plainly, because a system whose job is trustworthy financial data should
not overstate itself.

1. **Chart values are only read when printed.** Nothing is inferred from bar or
   point geometry. Charts without data labels yield text only, and every chart
   is flagged for review.
2. **Layout detection is heuristic.** It is strong on clean, ruled results
   releases and financial statements. A heavily designed investor presentation
   with overlapping graphics and unusual reading order would need a trained
   layout model; the `LayoutEngine` seam exists for exactly that.
3. **OCR drops currency glyphs.** Recovered from the text layer in hybrid mode;
   in visual-only mode the currency is inferred from Indian magnitude words and
   flagged as inferred. A scanned rupee document that never writes "crore",
   "lakh" or "Rs." leaves the currency `UNKNOWN`.
4. **Multi-page table continuation** is detected structurally (matching column
   count and headers). A continuation whose structure changes mid-table will be
   treated as a new table.
5. **The ground-truth key covers 85 facts**, chosen to span 6 entities, 4
   periods and 5 tables. It is a rigorous spot check, not exhaustive coverage of
   all 400 extracted facts.
6. **Typography is coarser without a text layer.** In visual-only mode font size
   falls back to the OCR box height and boldness is unavailable, so heading
   detection rests on size, case, position and length. This costs nothing
   measurable on the benchmark, but a document that distinguishes sections *only*
   by weight would be harder.
7. **Commentary summarisation is optional and off by default.** A local Ollama
   model can be enabled for summaries only; it can never introduce a fact, and
   the entire pipeline is fully functional without it.
8. **Single-node deployment.** The job queue is in-process. Horizontal scaling
   would mean swapping the queue module for a broker-backed one.
