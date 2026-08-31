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

## Results on the acceptance document

Reliance Industries Q1 FY2026-27 media release — 34 pages, 20 numbered pages of
content plus statements.

| | |
|---|---|
| Pages processed | **34 / 34** (0 failed) |
| Financial facts | **397** across 42 canonical metrics |
| Tables reconstructed | **30** (1,663 cells, 100% numeric parse rate) |
| Charts / figures | 1 |
| Validation checks | **119** — 106 passed, 4 warnings, **0 failures** |
| Facts needing review | 49 (12%) |
| Provenance coverage | **100%** — every fact resolves to a bounding box |
| Accuracy vs. hand-built key | **precision 1.00, recall 1.00, F1 1.00** (85 facts) |
| Same, with the PDF text layer blocked | **precision 1.00, recall 1.00, F1 1.00** |
| Processing time | 321 s cold (9.4 s/page) on a CPU-only laptop; 131 s re-run with the OCR cache warm |

Full detail, including the baseline comparison, is in
[docs/EVALUATION.md](docs/EVALUATION.md).

## Can it process a PDF with no text layer?

**Yes.** A 7-page PDF containing only rasterised images — 0 extractable
characters, 0 words, 0 embedded fonts — processed in `VISUAL_ONLY` mode:

| | native text (naive) | native text (strong) | **VISUAL_ONLY** |
|---|---|---|---|
| Financial facts | 0 | 0 | **251** |
| Tables reconstructed | 0 | 0 | **9** |
| Facts with a bounding box | 0 | 0 | **251** |
| F1 vs. hand-built key | 0 | 0 | **1.00** |
| Extraction used PDF text | true | true | **false** |

Both text baselines return nothing, because there is nothing to read. The full
audit — how the boundary is enforced, what it caught, and the honest
limitations — is in [docs/VISUAL_ONLY.md](docs/VISUAL_ONLY.md).

## Why visual processing matters

Measured on the same document, with the same taxonomy and parsers — only the
visual stages removed:

| | Text baseline (naive) | Text baseline (strong) | Visual pipeline |
|---|---|---|---|
| Facts extracted | 1 | 78 | **397** |
| Tables reconstructed | 0 | 0 | **30** |
| Facts with a unit | 1 | 4 | **369** |
| Facts with a currency | 0 | 0 | **309** |
| Facts with growth linkage | 0 | 0 | **96** |
| Facts with a source bounding box | 0 | 0 | **397** |
| F1 against the hand-built key | 0 | 0.067 | **1.00** |
| Numbers seen but discarded | — | 423 | 0 |

The naive baseline reads the PDF's content stream in its native order and finds
almost nothing: this document emits each span separately, so a row label and its
figures are not even adjacent. The *strong* baseline rebuilds lines from word
coordinates — already a geometric step — and does much better, but it still
cannot tell which **column**, and therefore which reporting period, a figure
belongs to. It keeps the first number on each line and throws away 423 others:
precisely the comparative-period figures an analyst wants. And on a scanned page
both baselines return nothing at all.

## Quick start

Requires **Python 3.11+** and **Node 18+**. No GPU, no system OCR binary, no API key.

```bash
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

To process the acceptance document without the UI:

```bash
cd backend && python cli.py ingest ../data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf
```

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Pipeline design, every technology choice and why, data model, limitations |
| [docs/SETUP.md](docs/SETUP.md) | Installation, configuration, running, troubleshooting, deployment |
| [docs/API.md](docs/API.md) | Every endpoint with request and response shapes |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Metrics, ground-truth methodology, baseline comparison, results |
| [docs/VISUAL_ONLY.md](docs/VISUAL_ONLY.md) | The visual-only audit: enforcement, image-only test, three-mode comparison |

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
  tests/            226 tests
  cli.py            command-line interface
frontend/src/
  views/            dashboard, upload, document tabs, compare, search, system
  components/       page canvas with overlays, provenance panel, UI primitives
  api/              typed API client
data/benchmark/     acceptance PDF, image-only fixture, ground-truth keys
docs/
```

## Testing

```bash
cd backend && python -m pytest
```

226 tests covering normalisation, taxonomy, table reconstruction, fact
extraction, the validation engine, confidence scoring, ingestion security,
rendering, the full pipeline (including corrupt, non-PDF, image-only and
OCR-failure cases), the API contract, background jobs and the evaluation
framework, and the visual-only boundary (including that it holds inside worker
threads).

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
- **Commentary summarisation is optional** and off by default; a local Ollama
  model can be enabled, but it can never introduce a fact.
