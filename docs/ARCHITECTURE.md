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
                    │  ANALYSIS  │  entities, table semantics, derived metrics,
                    └─────┬──────┘  relations, duplicates/conflicts, risks,
                          │         insights, anomalies  (see below)
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
(F1 0.988), and on a PDF containing only rasterised images `VISUAL_ONLY` extracts
255 facts where both text baselines extract zero. The full audit is in
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

#### Fragments are rejoined before anything reads them

A statement carries horizontal rules between its sections, and the layout
detector reads a rule as a band boundary. One filer's profit and loss came back
as four table regions: the first held the column heads and almost no figures,
the other three held figures and no heads at all. Every figure in the document
then lost its period, and the orphaned head — with no numeric columns left to
align against — collapsed three columns of text into one string and produced a
date in 2031.

`tables/fragments.py` rejoins them before the table builder runs. The test is
geometric and carries no notion of what any filer's tables look like: adjacent
regions whose horizontal extents agree to within a tenth, separated by less
than a few per cent of page height, with no heading or paragraph between them.
Rejoining is cheaper and safer than re-tuning layout, which would move every
document in the corpus; and the table builder still applies its usual
structural tests to the merged band and is free to reject it.

#### The header block is a structure, not a string

`tables/header_block.py` reads the head into one `ColumnHeader` per data
column. Three distinctions carry it, and each is geometric:

* **A banner is not a column head.** Text crossing every data column describes
  the table — a unit declaration, a title — and never becomes part of one
  column's head. It *is* consulted for the table's unit, because spanning the
  whole table is exactly what a table-wide declaration does.
* **A group head spans a set of columns and is placed by partition.** "Quarter
  ended" can sit wholly inside column 2 while governing columns 1 to 3, so
  containment cannot express it. Group heads cut the table at their own left
  edges. A header row containing any spanning cell is a group row, and every
  cell on it is a group head however narrow — which is what lets a "Year ended"
  over a single column stay distinct from the "Quarter ended" beside it.
* **A column head is assembled down its own column.** "30 June" and "2026" are
  separate elements on separate lines; only reading downwards recovers the
  date. Audit status and note references are captured and then withheld from
  the period parser, which has no use for "(Audited)" and can only be misled by
  "(Refer Note 3)".

The head text a column offers the period parser therefore contains only cells
that stand in that column. This is what makes a period *structural*: it travels
table → header cell → column → cell → fact, and never by proximity.

#### A merge must be argued for

Rejoining fragments on geometry alone carried a risk recorded when it was
built: two genuinely separate tables of the same width, with nothing between
them, would be merged, and a merge is invisible once made.
`tables/fragment_identity.py` replaces that with an evidence model whose
asymmetry is the whole design — **positive structural evidence is required, and
any single piece of contrary evidence is decisive.**

Contrary evidence is a new statement title, a heading or prose between the
bands, a change of reporting basis, a different unit declaration, a gap wider
than a statement's internal rules leave, extents that do not line up, or value
columns standing at different edges. Supporting evidence is adjacency, agreeing
extents, aligned value columns, a lower band with no title or basis of its own,
and a band carrying no figures at all. Two supporting pieces are the minimum.

The verdict is written to the table as a sentence — *"Merged because they are
adjacent (-36px apart); their horizontal extents agree 100%; the lower band
declares no title, basis or period of its own"* — so a reader can see why two
halves were joined, or why they were not.

A rule that decisive makes weak signals dangerous, and the first version proved
it: column alignment was consulted unconditionally, and a header block's
numbers are its year row — bare and centred — while a data band's are
right-aligned figures. The edges differ for a reason unrelated to whether the
bands belong together, so the model refused the very join it exists to make and
a statement's head was orphaned from its figures again. Column alignment now
requires three rows of figures on both sides before it speaks.

#### The table's shape decides what it owes

`tables/shape.py` classifies the columns: instants (a balance sheet), spans (a
profit and loss, a cash flow), dated-but-unqualified (a KPI table), or not
periods at all (segments, geographies). The distinction decides whether a
missing period is a defect or a property of the document — the same
`period = None` means opposite things in a statement and in a segment table,
and nothing downstream could otherwise tell them apart. `UNCLASSIFIED` is a
real answer and is treated as "not mandatory", because demanding a period from
a table we could not read would flag every unusual layout in the corpus.

`tables/validate.py` then checks the reconstruction against itself before facts
are persisted: cells sitting outside the column they were filed under, a dated
column that is nearly empty beside full ones, two columns resolving to the same
period. A failure means the reconstruction is internally inconsistent, so the
column a figure came from — and therefore its period — is not established, and
the facts are held for review rather than published as settled.

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

The radial test -- the circle search that separates a pie from a bar chart --
runs on a copy of the region bounded at 800px on its shorter side. Every
parameter of that search is a fraction of the region, so the answer does not
depend on how many pixels the shape is drawn with, and at the full 200dpi
render it was the dominant cost of chart detection: a page-sized figure meant
sweeping radii from 300 to 860px to answer a yes/no question. Measured over the
274 chart regions of the benchmark corpus, bounding it is 3.3x faster and
changes one classification -- a false positive on a page of plain text, where
the full-resolution pass reported a circle three quarters of which lay off the
page. Downscaling with INTER_AREA is a genuine low-pass filter, so the smaller
image is also the less noisy input.

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
so a December-, June- or September-year-end filer works without configuration.
A bare date carries no periodicity of its own, so it resolves to a quarter only
when a spanning header supplies one; otherwise it is recorded as a point in
time.

**A column head is a stack, not a line.** A statement prints one head across
several rows — a banner spanning a pair of columns, the day and month beneath
it, the year wrapped onto a third row — and frequently prints the whole head
*above* the band of figures that layout analysis cuts as the table. Flattening
that to a string only works when the fragments happen to land in a parseable
order, so the head is reconstructed geometrically instead:

- each level takes the fragment whose box overlaps that column's band most,
  because a date is centred over its column while the figures beneath it are
  right-aligned, so a date's own column index is frequently one band off;
- a fragment reaching across two or more bands is a banner, shared by every
  band it covers — counting bands rather than comparing widths, because band
  widths follow the figures beneath them;
- a year alone on a row is rejoined to the date above it;
- if the head is above the cut, the reconstruction walks upward from the top of
  the band, taking lines that lie within the table's horizontal extent and read
  as header fragments, and stopping at the first that does not. A line starting
  inside the row-label column is a page title, not a banner;
- a leaf that names its own period beats the banner above it, and the most
  specific reading wins: `1Q` over `FY27` is `Q1 FY27`, not `FY27`.

**What the document never states is not invented.** The year end is established
from an explicit fiscal label, from a year-length column, or from a cumulative
column closing on the same date as a shorter one — nine months ending 28 June
puts the year end in September, which is arithmetic rather than a regional
convention. Contradictory evidence establishes nothing. Where it cannot be
established, periods keep their exact dates and drop the fiscal label
(`3M ended 2025-06-28`, `fiscal_year = null`, reduced confidence) rather than
inheriting a default. An explicitly written `FY26` is the document's own
statement and always survives; the calendar dates it spans, which genuinely are
unknown, are left unset.

### The period travels; it is never rebuilt

A resolved period is four things, and all four are persisted with the figure:

| Field | Meaning |
|---|---|
| `period` | the fiscal label a reader recognises — `Q3 FY25`, `9M FY24`, `As at 2024-09-28` |
| `period_start` / `period_end` | the exact days the **source** supports, ISO-8601, or NULL |
| `period_type` | quarter, half year, nine month, fiscal year, calendar year, TTM, point in time |
| `period_basis` | `stated`, `derived`, `inherited` or `unknown` — how it was arrived at |
| `period_confidence` | the resolver's own confidence, separate from confidence in the value |

The dates are stored rather than recomputed, and that is the whole point. A
fiscal label cannot reproduce them:

```
resolved from the header  "Three Months Ended / June 28, / 2025"  ->  end 2025-06-28
re-derived from the label "Q3 FY25"                              ->  end 2025-06-30
```

Both are "Q3 FY25" under a September year end. Only the first is what the
document printed, because a 52/53-week filer's quarter does not close on the
last day of the month. Rebuilding the date downstream would put a figure two
days into the wrong period and nothing later could tell.

So the resolved `NormalizedPeriod` rides on `TableColumn.period_resolved` and is
handed to the fact as-is. `_period_for` reads that object; re-parsing the label
survives only as a fallback for columns whose period was settled by the
document-level repair pass, which works in labels and has no dates to offer.

**A date that was never established stays NULL.** Nothing downstream — not the
repository, not the API, not the browser — computes one from a label. The UI
says "exact dates not established" instead, which is a smaller claim and a true
one.

### How the basis is decided

The basis is set where the period is decided, never inferred afterwards from
the shape of the answer:

* **stated** — `parse_period` matched words the document wrote: a `1Q FY27`
  column head, a `Three Months Ended June 28, 2025` banner, an `As at` date.
* **derived** — `parse_period_levels` assembled it from the column hierarchy: a
  span phrase on one header row, a date on the next, a year wrapped onto a
  third. No single line said it.
* **inherited** — the period came from adjacent structure rather than from the
  column itself: a continuation fragment taking its parent's heads, a sibling
  column under the same banner lending its span, or a harmonised header row
  matching one elsewhere in the document. The dates travel with it.
* **unknown** — nothing established it. The figure keeps whatever exact date
  the source gave, if any, and claims nothing further.

A period resolved from a level that names itself keeps `stated` even when the
surrounding hierarchy had to be walked to find it, because the document did say
it — the walking was only to locate the words.

### Numeric column heads, and the one case that is refused

UK, European and Indian filings head their columns with all-numeric dates —
`6 months ended / 30.06.26` over the statement, a bare `31.12.25` over the
balance sheet — and write the span in digits rather than words. Both are read.

`30.06.26` can only be day-first: there is no thirtieth month. `06.30.26` can
only be month-first. **`06.07.26` is genuinely both** — 6 July under one
convention, 7 June under the other — and nothing in the string settles it, so
it resolves to nothing. Picking a convention would put a figure in the wrong
month and leave no trace that a choice had been made.

The refusal costs almost nothing real: reporting periods close at month ends,
and every month end past the twelfth disambiguates itself. It also governs
header *detection* — an ambiguous numeric string is not treated as evidence of
a column head, because acting on it would let table reconstruction cut the
header band in the wrong place.

This mattered more than it sounds. A 165-page half-yearly report processed
before numeric heads were read came back with **308 of its 335 figures carrying
no period at all**, and the 27 that had one were labelled a *three-month* span
on a half-yearly filing. The period model was already correct; the resolver
simply could not read the document's column heads.

**Capture quality is measured, and not acted on.** Every rendered page is
measured for skew, noise, ink-to-paper contrast, illumination drift, sharpness
and effective text height (`app/rendering/quality.py`) — statistics over the
pixels, with no model file. The measurement is recorded per page and reported.
It deliberately does not change what is read: selective repair was built and
measured against the scanner-degraded benchmark and recovered no accuracy, so
it was removed rather than shipped. See
[EVALUATION.md](EVALUATION.md#does-preprocessing-help).

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

### Evidence reconciliation

Every strategy converges on the same `FinancialFact`, and one layer then asks
whether two of them describe the same figure. The identity that decides it is
`(metric, entity, period, period_type, basis)` -- deliberately *not* the value,
the printed label, the unit or the source. Each of those is a real distinction,
and each belongs somewhere other than the identity:

* the **value**, because two candidates in one slot with different values are
  the case the layer exists for;
* the **printed label**, in the classification, where it explains a difference
  rather than hiding one -- "Depreciation" and "Depreciation / Amortisation"
  are one row of one statement, printed twice;
* the **unit and currency**, in comparability, so a figure in crore and the
  same figure in million are compared through their base value instead of being
  filed as unrelated;
* the **source**, because which strategy read a figure says nothing about which
  figure it is.

Within one identity every collision is classified: `identical_repeat`
(corroboration), `different_definition`, `different_unit`, `different_scope`,
`genuine_conflict`, or `uncertain` where one side has no period and the two
therefore cannot be shown to disagree. Only a repeat collapses, and the merged
fact records every source it absorbed. A conflict keeps **both** figures, flags
**both**, and links them; nothing picks a winner.

Confidence is scored after this rather than before it. Agreement between
independent strategies is quantitative and raises the corroboration component,
counted once per strategy -- three rows of one table agreeing is one method
agreeing with itself. Contradiction is categorical: it caps the score below the
review threshold instead of being averaged into it, because a figure the
document disputes needs a person, not a slightly lower number.

See `docs/EVIDENCE_RECONCILIATION.md` for the audit that produced this design.

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

### Fact state: dimensions kept apart

A single confidence number blends how clearly the digits were printed with
whether anyone established what period they belong to, and those fail
independently. Averaged together, excellent recognition hides poor attribution.

`extraction/fact_state.py` scores metric, value, period, entity and unit
separately, with accounting consistency and corroboration alongside as evidence
rather than requirements, and lets the **weakest** decide: a figure is only as
good as the least-established thing about it. The states are `VALIDATED`,
`HIGH_CONFIDENCE`, `MEDIUM_CONFIDENCE`, `REQUIRES_REVIEW` and `UNKNOWN`.

Two consequences are deliberate. An accounting check that passes cannot rescue
a fact with no period — arithmetic confirms a figure, it does not say which
quarter the figure is. And recognition confidence of 0.99 does not lift a fact
whose column was never matched to a heading.

What is *not* a gap is equally explicit: a segment column genuinely has no
period, so period is marked not-applicable for facts from non-periodized
tables; a percentage owes no currency; a per-share figure owes no magnitude.

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

Worker count is two thirds of the logical cores capped at eight, and then capped
again by the memory actually free -- roughly 400 MB per worker, never below two.
The core-count rule alone describes what a machine can compute and says nothing
about what it can hold: on a 16 GB machine with 2.2 GB free it still asked for
eight workers, and eight page bitmaps plus eight ONNX arenas do not fit. The run
did not fail, it thrashed -- one batch of eight pages took 35 minutes where the
median page took 59 seconds, and a 75-page document that had taken 14 minutes
took 48. Nothing reported an error; the pipeline simply appeared to hang. Sizing
against free memory turns a stall into a slower but honest run.

Concurrency was swept rather than guessed. Over 24 pages with the OCR cache
bypassed, and with extraction verified identical in every configuration, wall
clock fell from 363s at two workers to 255s at eight and flattened after four --
the machine is saturated, not under-scheduled. Per-thread OCR engines were
measured against the shared one and are roughly three times worse, because ONNX
Runtime already parallelises internally and extra sessions only multiply the
arenas.

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
7. **No language model is used anywhere.** Commentary is classified by rule
   and quoted verbatim, never summarised or rewritten. The `app/llm` package
   and the `FINSIGHT_LLM_*` settings are unused placeholders.
8. **Degradation is measurable but not repairable here.** Capture defects are
   detected reliably — 0 false positives across 44 clean pages, all 7 degraded
   pages caught — but repairing them did not recover accuracy on the degraded
   benchmark, and the repair pass was deleted rather than shipped. The loss
   from a 150 dpi JPEG scan is information the recogniser needs and no filter
   restores.
9. **Single-node deployment.** The job queue is in-process. Horizontal scaling
   would mean swapping the queue module for a broker-backed one.
10. **Throughput is roughly 7 pages per minute on a 12-core CPU**, and about 90%
    of that is OCR inference. Detection resolution, render DPI, worker count,
    intra-op thread count and per-thread engines have all been swept; the only
    lever that saved meaningful time cost a document its unit declaration and
    was rejected. Faster means a different recogniser or a GPU, not another
    parameter. Re-processing an already-seen file is a different matter: the
    content-keyed OCR cache makes it ~3.5x faster end to end.
11. **Region typing is weaker without a text layer, and entity attribution
    depends on it.** The same deck run in both modes types "Summary of
    Consolidated Financial Performance" as a `title` in hybrid mode and a
    `caption` in visual-only mode. Entity resolution reads only headings, so in
    visual-only mode that declaration never terminates the preceding segment's
    carry-forward and sixteen figures stay under the wrong entity. The OCR text
    itself is correct in both modes; this is a classification gap, not a
    recognition one.
12. **Taxonomy coverage, not attribution, now limits extraction quality.** Of
    151 golden facts read from two source statements, 72 are extracted and all
    72 carry the right period, unit and entity. The other 79 produce no fact at
    all: their row labels are absent from the taxonomy ("Profit before tax for
    the period/year" fails on the trailing words) or were corrupted by OCR
    ("i)Empioyees cost"). No fact is *wrong*; some are missing.
13. **A column head that absorbs the prose above it is refused, not guessed.**
    A comparative column headed `Jun-25` was reconstructed as "…discontinued in
    Dec'25 end Jun-25" and dated to December 2025 -- six months wrong, at high
    confidence, unflagged. A head naming two different dates cannot say which is
    its column's, so it now yields no period and the figures are held for
    review. The underlying defect -- the header cell taking in the sentence
    introducing the table -- remains, and shows up as undated comparatives on
    slide-style tables.

---

# The semantic layer

Everything above turns pixels into *facts*. This layer turns facts into
*financial information*. It runs as one pipeline stage after validation, reads
only what earlier stages produced, and writes to its own tables — so nothing in
it can influence what was read off the page.

## Entity model

A figure means nothing until you know **whose** it is. Consolidated revenue and
standalone revenue are different numbers for the same label, and a segment's
EBITDA must never be divided by the group's revenue.

`app/document/entities.py` builds a registry per document from the document's
own headings and page furniture:

```
<Filing company>              [company]        from the filer's stated legal name
├─ Standalone                 [standalone]     from a declared reporting basis
└─ Consolidated               [consolidated]   the default basis
   ├─ <segment>               [segment]        from section headings
   └─ <subsidiary>            [subsidiary]     from corporate-suffix vocabulary
```

No company, segment or abbreviation is hardcoded. Classification keys off
generic vocabulary — corporate suffixes (limited, ltd, inc, plc), segment
markers (segment, division, business, vertical), and the two reporting bases.

Two subtleties the code is careful about:

* **"Consolidated <Subsidiary> Limited" is a subsidiary**, not the group's
  consolidated column. The word "consolidated" designates the group only when
  nothing else is named.
* **A reporting basis is often declared in page furniture.** A statement block
  repeats "UNAUDITED STANDALONE FINANCIAL RESULTS" at the top of every page, so
  chrome detection classifies it as a running header and section building never
  sees it. Chrome is therefore scanned separately for a declared basis.

Narrative attribution resolves the entity **nearest the metric** in the
sentence, because a run of bullets arrives as one block:

```
"Consolidated Revenue at 340,257 crore ... <SEGMENT> EBITDA up 23.9%"
       ↑ consolidated                            ↑ that segment
```

## Table semantics

`app/tables/semantics.py` layers meaning over the reconstructed grid: what kind
of table this is, what each column is for (label, serial, unit-of-measure,
period value, change, share, note), and which columns are the current period,
the prior-year comparison and the sequential quarter. Those relationships come
from the **resolved periods**, never from column order — a statement may print
the prior year first.

## Column period repair

A statement column is headed by a bare date; what it *means* comes from a
banner above it ("Quarter Ended" / "Year Ended") which is routinely lost:

* a statement continuing to the next page repeats the dates without the banner;
* one banner spans three columns and another spans the fourth, but the banner
  cell binds to a single column.

`app/tables/periods.py` repairs this at document level using only what the
document shows — harmonisation (a header the document resolved elsewhere means
the same here), collision repair (two value columns cannot denote one period),
and collision-avoiding span inheritance.

## Derived metrics

`app/analysis/derived.py` computes ratios from figures the document published.
Every result carries its formula and the fact ids it used, and lives in a
**separate table** from reported facts so a calculated number can never be
mistaken for one management stated.

Four rules keep the arithmetic honest:

| Rule | Why |
|---|---|
| No flow ratio against a balance-sheet date | "Revenue as at 30 June" is not a quarter of revenue |
| Annualise a flow denominator under a stock | Net Debt ÷ one quarter of EBITDA is four times too high |
| Compare on the reported magnitude | A base-unit result against a figure printed in crore differs by 10⁷ |
| Identify the denominator the filer used | A filer may compute margins on gross revenue, not revenue from operations |

The last one matters: rather than flag eight false anomalies, the engine tries
the other revenue figures the document printed, adopts whichever reconciles
with the published ratio, and records *why* in the metric's notes.

## Duplicates and conflicts

Nothing is deleted. Every collision is classified into one of eight outcomes,
and only an exact repeat collapses (recording the pages its copies came from,
which raises confidence). A genuine conflict keeps **both** facts, flags both,
links them, and says "unresolved — both retained for human review".

Three rules stop the system inventing contradictions:

* **A fact with no period is its own scope.** Several columns of one table
  routinely fail period resolution together; bucketing them would declare this
  quarter's EBITDA to contradict last quarter's.
* **The printed label is part of the identity.** Two differently-labelled rows
  that happen to share a taxonomy key are two line items, not a contradiction.
* **The same printed number under two unit declarations is one fact at two
  scales.** "2,490" as crore on one page and as millions on another is a
  scale disagreement, not a conflict; it resolves to `different_unit`, carries
  `meta["scale_disagreement"]`, and reaches review as `UNIT_UNDETERMINED`.

A caution learned from a bank filing that reports on both bases: the
reconciler's verdicts are only as good as the identity it is given. When
standalone and consolidated results were attributed to one entity, twelve
perfectly correct pairs of figures were reported as contradictions. The layer
was behaving exactly as designed; the entity was wrong. Conflicts are therefore
worth reading as a signal about attribution, not only about the numbers.

## Explanation

`app/analysis/explain.py` answers *why do you believe this figure?* from stored
evidence only — why this metric, why this period, why this entity, why this
unit, reported or calculated, every confidence component with the weight it
carried, validation outcomes, related facts, and the source box.

It is templates over recorded values: deterministic (the same fact yields
byte-identical output, asserted by test) and grounded (a sentence that cannot
be backed by a recorded value is not written). No language model is involved,
and the response says so.

## Generalization

The extraction path contains **no company-specific logic**. What makes it work
across filers is that every document-specific fact is *discovered*:

| Varies by filer | How it is resolved |
|---|---|
| Company and segment names | Entity registry, from headings that *declare* a unit |
| Reporting basis | Declared banners: page chrome, or the top of the page body |
| Fiscal year end | Inferred; a cumulative span pins it arithmetically |
| Currency | Table-local declaration, never loose prose |
| Magnitude | Row unit cell → table declaration → document default |
| Metric vocabulary | 78-metric taxonomy with alias matching |
| Column layout | Rulings → whitespace gutters → numeric right-edge clusters |

Two refinements came from documents the system had not seen, and both are
structural rather than lexical, because a lexical rule is a company-specific
rule wearing a disguise.

**A heading has to declare a reporting unit, not merely mention one.** In a
results deck almost every heading contains retail, digital, services or
business; three unseen documents each had a non-entity governing most of their
figures -- a marketing line, a product name, and a label inside a wheel diagram.
The test is now either a legal suffix anywhere in the heading, or a head noun at
the *end* saying the heading is a unit. Position carries the distinction:
"JIOSTAR BUSINESS" is a reporting unit and "Paytm for Business App" is a
product, and the word is the same in both. An unbalanced bracket also
disqualifies a heading, because it means the line grouper cut a longer name in
half -- one filer's own legal name, split that way, was attributed half the
document as a subsidiary.

**A basis declaration can sit in body text.** Statutory filings often open the
page with "Standalone Segment information in accordance with the RBI guidelines
and Accounting Standard 17" rather than putting it in a running header, and
where the header band is OCR noise that line is the only statement of basis the
page makes. The top few non-chrome regions are read for it. Further down, the
same word is prose rather than a heading over the figures, so the limit matters.

Column detection picks whichever strategy matches the width the **data**
implies: a data row carries one figure per value column, so the modal count of
numbers per row states the table's width. That is what makes a US-style
statement work, where figures are right-aligned and share-count rows reach
across the gutters that dollar rows leave.
