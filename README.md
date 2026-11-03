# FinSight — Financial Document Intelligence

Turns financial PDFs into **structured, validated, traceable** data.

Every page is rendered to an image and analysed visually — OCR with bounding
boxes, layout geometry, ruling lines, column detection — because a financial PDF
encodes its meaning in *layout*, not in its glyph stream. Every extracted figure
carries its unit, currency, fiscal period, reporting entity, a confidence score
built from independent evidence, the arithmetic checks it passed or failed, and
the exact pixels it came from.

**No paid LLM API is used anywhere.** All models run locally on CPU.

---

## What it does

```
PDF → validate → render pages → OCR (+ text-layer corroboration)
    → layout regions → document assembly → table reconstruction → chart detection
    → financial extraction → normalisation → validation → confidence → provenance
    → database → API → dashboard → human review
```

- **Visual-first, and provably so.** Rendered page images are the canonical
  representation, so scanned and born-digital PDFs go through the same path. A
  strict `VISUAL_ONLY` mode hard-blocks the PDF text layer from reaching
  extraction — see [docs/VISUAL_ONLY.md](docs/VISUAL_ONLY.md) for the audit.
- **Traceable.** Click any number and see the page, the region, the table cell,
  the OCR text and a crop of the exact source area.
- **Validated.** Reported growth rates, margins, accounting identities and
  segment totals are independently recomputed. Discrepancies are surfaced, never
  silently corrected.
- **Honest.** A document reaches `COMPLETED` only when it genuinely did. Charts
  whose values were not printed are recorded as such rather than guessed.
- **Every figure carries its period.** Not just a fiscal label but the exact
  days the source supports, the span it covers, and how the period was
  established — stated in the document, derived from the statement header, or
  inherited from the table structure. Where the evidence does not support a
  period, the figure says so rather than borrowing one.
- **Long documents stay under your control.** Processing runs in the background
  and can be paused at a page boundary, resumed from where it stopped, or
  cancelled outright; documents can be deleted with everything derived from
  them. See [docs/LIFECYCLE.md](docs/LIFECYCLE.md).

### Evidence reconciliation

Six extraction strategies read the same page, and a document repeats its own
figures on purpose. One layer decides whether two readings describe the same
fact, and says which of three things it found:

* **corroborated** -- another strategy read the same value from different
  pixels. Confidence rises, and both sources are kept.
* **disputed** -- something else in the document reports a different value for
  the same measure, entity and period. Both figures are retained, both are
  flagged, and neither is chosen. Contradiction caps the confidence below the
  review threshold rather than being averaged into it.
* **single source** -- read once. The ordinary case.

Two figures in different periods, entities, currencies or reporting bases are
never a contradiction; they are different facts. A figure with no period can
corroborate but never contradict, because two undated figures may belong to
different quarters.

## Reporting periods

A number without its period is not information. FinSight resolves the period at
the level of the individual column, not the document, because a half-yearly
report contains three-month, six-month, nine-month and balance-sheet columns
side by side.

Each figure is shown as the source supports it:

```
Gross Margin           Three months ended 28 June 2025      $43,718 million
Consolidated           Q3 FY25 · derived from the statement header
```

The exact date leads and the fiscal label follows, because a label is a
convention and a date is a fact — Apple's Q3 FY25 closed on **28 June 2025**,
not the 30th, and re-deriving that date from the label would be wrong by two
days. So the dates the resolver established travel with the figure through
persistence and the API rather than being recomputed anywhere downstream.

Where a period cannot be established, the interface says
`Period not identified` instead of showing a fiscal label nobody can support.

## Results

Measured on a CPU-only laptop. **Accuracy is measured against hand-built answer
keys** (85 facts for the RIL filing, 81 for the image-only one); it is *not* a
claim about every extracted fact. Documents without a key report no accuracy.

### Reliance Industries Q1 FY2026-27 media release — 34 pages

| | HYBRID | VISUAL_ONLY |
|---|---|---|
| Financial facts | 432 | 431 |
| Tables / cells | 30 / 1,663 | 30 / 1,663 |
| Reporting entities discovered | 8 | 8 |
| Provenance coverage | **100%** | **100%** |
| Facts with a period | 406 (94%) | 405 (94%) |
| Accuracy vs. key (85 facts) | **P 0.988 · R 0.988 · F1 0.988** | **P 0.988 · R 0.988 · F1 0.988** |
| Native text reaching extraction | yes (corroboration) | **no** |
| Cold / warm | 385 s / 320 s | 269 s / 239 s |

### Generalization — Apple Inc. FY25 Q3 condensed statements, 3 pages

A genuinely different filer: **USD, millions, September fiscal year end, US
GAAP** — against the RIL benchmark's INR, crore and March year end. Downloaded
from Apple's public newsroom; no value was fabricated and no company-specific
code exists.

| | HYBRID | VISUAL_ONLY |
|---|---|---|
| Financial facts | 56 | 56 |
| Tables / cells | 5 / 363 | 5 / 363 |
| Entities discovered | 2 (`Apple Inc.`, `Consolidated`) | 2 |
| Currency resolved to USD | 52 / 56 | 52 / 56 |
| Provenance coverage | **100%** | **100%** |
| Facts with a period | **56 / 56** | **56 / 56** |

Ingestion, rendering, OCR, layout, tables, entity discovery, currency, units,
provenance, export **and period attribution** all generalize. Apple's column
heads are printed as a stack rather than a line — a banner spanning a pair of
columns, the day and month under it, the year wrapped onto a third row, and
the whole head sitting *above* the band the layout stage cuts — so they are
reconstructed geometrically rather than by flattening them to a string:

| Column head, as printed | Resolved |
|---|---|
| `Three Months Ended` / `June 28,` / `2025` | `Q3 FY25` |
| `Nine Months Ended` / `June 29,` / `2024` | `9M FY24` |
| `June 28,` / `2025` (balance sheet) | `As at 2025-06-28` |
| `September 28,` / `2024` (balance sheet) | `As at 2024-09-28` |

The September year end is never written anywhere in the filing. It is derived
instead from the statement's own arithmetic: a nine-month column closing on the
same date as a three-month column is the year to date, so the year began nine
months before 28 June and ends in September. All 56 periods were checked by
hand against the source. See [docs/EVALUATION.md](docs/EVALUATION.md).

### Image-only and scanner-degraded — 7 pages, no text layer at all

| | clean rasterisation | scanner-degraded |
|---|---|---|
| Facts | 255 | 251 |
| Accuracy vs. key (81 facts) | **F1 0.988** (80/81) | **F1 0.695** (49/81) |
| Provenance coverage | 100% | 100% |
| Pages measured as degraded | 0 / 7 | **7 / 7** |

The degraded fixture applies real scanner artefacts — 150 dpi, 0.6° skew,
sensor noise, JPEG compression, uneven illumination — to the *same real pages*.
Accuracy falls sharply, and the system responds by flagging figures for review
rather than returning wrong answers quietly.

Each page's capture quality is measured from the pixels — skew, contrast,
sharpness, noise, effective text height — with no learned model. Across the 44
clean pages of the three other documents not one page is flagged; all seven
degraded pages are, with the defects they actually have. Whether repairing
those defects helps is a separate question, and the measured answer is no:
see [docs/EVALUATION.md](docs/EVALUATION.md#does-preprocessing-help).

### Baseline: what the PDF text layer alone recovers

| Document | Text-layer baseline | Visual pipeline |
|---|---|---|
| RIL media release | 87 facts, 0 bounding boxes | 431 facts, 100% traceable |
| Image-only PDF | **0 facts** (no text layer) | 255 facts, F1 0.988 |
| Apple statements | 23 facts, 0 bounding boxes | 56 facts, 100% traceable |

The baseline is given its strongest form — words regrouped by y-coordinate
rather than content-stream order — so the comparison is not against a straw man.

## Can it process a PDF with no text layer?

**Yes.** A 7-page PDF containing only rasterised images — 0 extractable
characters, 0 words, 0 embedded fonts — processed in `VISUAL_ONLY` mode:

| | native text (naive) | native text (strong) | **VISUAL_ONLY** |
|---|---|---|---|
| Financial facts | 0 | 0 | **255** |
| Tables reconstructed | 0 | 0 | **9** |
| Facts with a bounding box | 0 | 0 | **255** |
| F1 vs. hand-built key (81 facts) | 0 | 0 | **0.988** |
| Extraction used PDF text | true | true | **false** |

Both text baselines return nothing, because there is nothing to read. The full
audit — how the boundary is enforced, what it caught, and the honest
limitations — is in [docs/VISUAL_ONLY.md](docs/VISUAL_ONLY.md).

## Why visual processing matters

Measured on the same document, with the same taxonomy and parsers — only the
visual stages removed:

| | Text baseline (naive) | Text baseline (strong) | Visual pipeline |
|---|---|---|---|
| Facts extracted | 1 | 87 | **431** |
| Tables reconstructed | 0 | 0 | **30** |
| Facts with a unit | 1 | 4 | **372** |
| Facts with a currency | 0 | 0 | **312** |
| Facts with growth linkage | 0 | 0 | **77** |
| Facts with a source bounding box | 0 | 0 | **431** |
| F1 against the hand-built key (85 facts) | 0 | 0.067 | **0.988** |
| Numbers seen but discarded | — | 453 | 0 |

The naive baseline reads the PDF's content stream in its native order and finds
almost nothing: this document emits each span separately, so a row label and its
figures are not even adjacent. The *strong* baseline rebuilds lines from word
coordinates — already a geometric step — and does much better, but it still
cannot tell which **column**, and therefore which reporting period, a figure
belongs to. It keeps the first number on each line and throws away 453 others:
precisely the comparative-period figures an analyst wants. And on a scanned page
both baselines return nothing at all.

## Quick start

Requires **Python 3.11+** and **Node 18+**. No GPU, no system OCR binary, no API key.

```bash
git clone https://github.com/tahalok2005-alt/Finsight.git
cd Finsight
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r backend/requirements.txt
cd frontend && npm install && cd ..
```

Run the backend and the dashboard in two terminals:

```bash
cd backend && python -m uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && npm run dev
```

Open <http://localhost:5173>, drop a PDF on the upload page, and watch it
process. Full instructions, including the command-line path, are in
[docs/SETUP.md](docs/SETUP.md).

Or run the whole application as one container:

```bash
docker build -t finsight .
docker run --rm -p 7860:7860 finsight      # then open http://localhost:7860
```

To deploy it — free on Hugging Face Spaces, or on your own server — see
[docs/DEPLOY.md](docs/DEPLOY.md). Before it goes on a public address, choose a
mode: **public**, where every browser gets a private workspace and documents
are deleted after a set time, or **private**, behind one shared password.

To process the acceptance document without the UI:

```bash
cd backend && python cli.py ingest ../data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf
```

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Pipeline design, every technology choice and why, data model, limitations |
| [docs/SETUP.md](docs/SETUP.md) | Installation, configuration, running, troubleshooting |
| [docs/DEPLOY.md](docs/DEPLOY.md) | One-container deployment: Hugging Face Spaces, your own server, the shared password |
| [docs/API.md](docs/API.md) | Every endpoint with request and response shapes |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Metrics, ground-truth methodology, baseline comparison, results |
| [docs/VISUAL_ONLY.md](docs/VISUAL_ONLY.md) | The visual-only audit: enforcement, image-only test, three-mode comparison |
| [docs/LIFECYCLE.md](docs/LIFECYCLE.md) | Document states, pause/resume/cancel, deletion, progress and restart |
| [docs/FAILURE_KB.md](docs/FAILURE_KB.md) | Every class of wrong fact found so far, its cause, and the rule that now refuses it |
| [docs/reports/](docs/reports/) | Engineering reports from each development phase, with measured results |

## Repository layout

```
backend/
  app/
    api/            FastAPI routers
    core/           logging, errors, geometry, ids, processing modes
    config/         settings (all tunables in one place)
    ingestion/      upload storage, PDF validation
    rendering/      PyMuPDF page rasterisation + cache
    ocr/            engine interface, RapidOCR, Tesseract adapter, text-layer, cache
    layout/         CV signals, region detection, reading order
    document/       cross-page assembly, sections, entity attribution
    tables/         table structure reconstruction
    charts/         chart detection and label harvesting
    extraction/     taxonomy, table facts, narrative, guidance, confidence
    normalization/  numbers, units, currency, fiscal periods
    validation/     arithmetic and consistency rule engine
    provenance/     source resolution and evidence crops
    review/         human-in-the-loop workflow
    comparison/     period and document comparison
    export/         JSON / CSV / XLSX
    evaluation/     metrics, baseline and three-mode comparison
    database/       SQLAlchemy models, session, repository
    services/       pipeline orchestrator, persistence bridge
    workers/        background job queue
    analysis/       derived metrics, identities, anomalies, insights, explanations
    spatial/        reading figures from cards, tiles and transposed layouts
  tests/            ~1,320 tests
  cli.py            command-line interface
frontend/src/
  views/            dashboard, upload, document tabs, compare, search, system
  components/       page canvas with overlays, provenance panel, UI primitives
  api/              typed API client
data/benchmark/     acceptance PDF, image-only fixture, ground-truth keys
data/golden/        hand-typed ground truth for the evaluation corpus
data/corpus/        corpus manifest: every document's source URL and sha256
docs/               design, setup, API, evaluation; docs/reports/ per phase
```

## Testing

```bash
cd backend && python -m pytest
```

**About 1,320 tests** covering normalisation, taxonomy, table reconstruction, fact
extraction, the validation engine, confidence scoring, ingestion security,
rendering, the full pipeline (including corrupt, non-PDF, image-only and
OCR-failure cases), the API contract, background jobs, the evaluation
framework, the visual-only boundary (including that it holds inside worker
threads), the document lifecycle (pause, resume, cancel, delete with cascade,
restart reconciliation), period provenance end to end (a source-derived date
surviving pipeline, database, read-back and API without being recomputed from
its fiscal label), and the financial-intelligence layer — entities, derived
metrics, duplicate and conflict handling, insights, anomalies and
explanations.

## Known limitations

These are real and deliberately not hidden — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#limitations) for the full list.

- **Chart values are only read when printed.** Nothing is inferred from bar
  geometry; every chart is flagged for review.
- **Layout detection is heuristic**, not a learned model. It is strong on the
  clean, ruled documents this system targets and would need a trained model for
  heavily designed investor presentations. The engine interface exists for that.
- **OCR drops currency glyphs.** The PP-OCR charset has no rupee sign. In hybrid
  mode the symbol is recovered from the PDF text layer; in visual-only mode the
  currency is inferred from India-only magnitude words read from the pixels
  ("crore", "lakh") and flagged as inferred. A scanned rupee document that never
  spells out a currency word would leave it `UNKNOWN`.
- **No language model is used anywhere.** Management commentary is classified
  by rule (topic, sentiment, speaker) and quoted verbatim -- never summarised or
  rewritten. The `app/llm` package and the `FINSIGHT_LLM_*` settings are unused
  placeholders.
- **A fiscal year end the document never evidences is left unknown.** Where
  neither an explicit fiscal label nor a datable year-length or cumulative
  column appears, periods keep their exact dates (`3M ended 2025-06-28`) and
  carry no fiscal year at all. That is deliberate: an incomplete period is
  recoverable, an invented one is not.
- **Scanner degradation costs accuracy, and preprocessing does not buy it
  back.** On a 150 dpi, skewed, JPEG copy of real pages, F1 falls from 0.988
  to 0.695. Deskew, unsharp, CLAHE, upscaling and two binarisations were built
  and measured against that fixture; none recovered accuracy (see
  [docs/EVALUATION.md](docs/EVALUATION.md#does-preprocessing-help)), so the
  repair pass was not shipped. Per-page capture quality *is* measured and
  reported, so a bad result can be explained rather than guessed at.
- **Throughput is about 7 pages per minute on a 12-core CPU**, and roughly 90%
  of that is OCR inference. Detection resolution, render DPI, worker count,
  intra-op threads and per-thread engines were all swept; the only lever that
  saved real time cost a document its unit declaration, so it was rejected. See
  [docs/EVALUATION.md](docs/EVALUATION.md#the-ocr-levers-that-were-measured-and-rejected).
  Re-processing a file already seen is ~3.5x faster, from the content-keyed
  OCR cache.
- **Every figure from a dated statement carries its own column's period, or
  none.** A statement's columns are reporting periods, and a figure that cannot
  be matched to one is reported as unresolved and flagged for review -- never
  as "not period-specific", which is what a segment column genuinely is. Held
  to 151 golden facts transcribed from two real statements by eye: of the 72
  extracted, 100% carry the right period, unit and entity
  ([docs/EVALUATION.md](docs/EVALUATION.md#the-golden-corpus-facts-read-from-the-source)).
  The remaining 79 produce no fact at all, because their row labels are not in
  the taxonomy or were corrupted by OCR -- coverage, not attribution, is now
  the binding constraint.
- **A table fragment is only rejoined when the structure argues for it.** A
  statement split across bands by its own section rules is put back together;
  two tables that merely share a width are not. Positive structural evidence is
  required and any single piece of contrary evidence — a new statement title, a
  change of reporting basis, a different unit declaration — refuses the join,
  and every merge records the sentence explaining itself.
- **A figure is only as good as the least-established thing about it.** Metric,
  value, period, entity and unit are scored separately and the weakest decides
  the fact's state, so a figure read perfectly from a clean page but never
  matched to a column reads as unresolved rather than as 99%. An accounting
  check that passes cannot rescue a fact with no period.
- **Region typing is weaker without a text layer, and entity attribution
  depends on it.** The same deck types a basis declaration as a `title` in
  hybrid mode and a `caption` in visual-only mode; because entity resolution
  reads only headings, sixteen figures stay under the wrong entity in
  visual-only mode. The OCR text is correct in both — this is a classification
  gap, not a recognition one, and it is the largest open defect in the system.
