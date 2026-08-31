# Visual-only audit

The central architectural claim of this system is that it understands financial
PDFs **as images**, not as glyph streams. This document is the audit of that
claim: what was enforced, what was measured, and what the limitations are.

The short answer to the question the audit exists to settle:

> **Can the system process a PDF with no text layer? — YES.**
> A 7-page PDF containing only rasterised page images (0 extractable characters,
> 0 words, 0 embedded fonts) yields 846 OCR elements, 9 reconstructed tables,
> 607 table cells and 251 financial facts, every one with a bounding box on the
> rendered image, scoring **precision 1.00 / recall 1.00 / F1 1.00** against a
> hand-built key. Both native-text baselines extract **zero** from the same
> file. Evidence is below and reproducible with two commands.

---

## 1. The two processing modes

```python
class ProcessingMode(str, Enum):
    VISUAL_ONLY = "VISUAL_ONLY"   # pixels only; native text hard-blocked
    HYBRID      = "HYBRID"        # pixels + text layer as corroboration
```

`VISUAL_ONLY` permits downstream extraction to see only:

- the rendered page image
- OCR text and OCR bounding boxes
- layout regions derived from those boxes and from CV signals
- tables and charts derived from that geometry

Any attempt to read the PDF's native text for extraction raises
`NativeTextAccessViolation` and stops the run. **There is no fallback.** If the
visual pipeline cannot recover something, the field is absent, the period is
unattributed, or the fact is flagged for review — it is never quietly filled in
from the glyph stream.

`HYBRID` is the production default: the identical visual pipeline, with the text
layer added as an independent corroborating channel.

## 2. Where the boundary is enforced

`app/core/modes.py` gates every native-text read through one function:

```python
guard_native_text(caller, scope, detail="")
```

Each call site is classified, and the classification is enforced rather than
merely documented:

| Scope | Class | Call site | Feeds extraction? | Visual-only |
|---|---|---|---|---|
| `INGESTION_VALIDATION` | A | `ingestion/validator.py` — counts characters to decide whether a text layer exists | No. Pure metadata; nothing downstream branches on it | **Permitted**, and recorded |
| `CORROBORATION` | B/C | `ocr/textlayer.py` — confirms OCR readings, supplies font size and weight to layout, recovers currency glyphs | **Yes** — typography feeds heading classification, and glyphs feed currency | **Blocked** |
| `EVALUATION_BASELINE` | A | `evaluation/baseline.py` — the text-only baseline that exists to be measured against | No. Measurement, not extraction | **Permitted**, and recorded |

Every access, allowed or blocked, is appended to a per-run **access ledger**, so
a run can be audited afterwards rather than trusted:

```json
{
  "mode": "VISUAL_ONLY",
  "allowed_by_scope": {"ingestion_validation": 1},
  "blocked_total": 0,
  "corroboration_accesses": 0,
  "extraction_fed_by_native_text": false
}
```

The ledger is stored on the `processing_runs` row, returned by
`GET /documents/{id}/status`, and shown in the dashboard as a `visual-only` chip
next to the document title.

### A real bug this found

`ContextVar`s do **not** propagate into `ThreadPoolExecutor` workers, and the
pipeline analyses pages on a worker pool. The first implementation therefore had
the guard silently inactive in exactly the threads doing the work:

```
without propagation (visual_only?, native_allowed?): [(False, True), (False, True)]
with    propagation (visual_only?, native_allowed?): [(True, False), (True, False)]
```

The pipeline now copies the context into every worker
(`parent_context.copy().run(...)`), and
`test_boundary_holds_inside_worker_threads` pins the behaviour so it cannot
regress.

## 3. The image-only fixture

`scripts/make_image_only_pdf.py` rasterises chosen pages and places **only the
bitmap** on each new page. It then verifies its own output and refuses to emit a
file that still carries glyphs:

```
wrote data/benchmark/RIL_Q1_FY27_IMAGE_ONLY.pdf  (2.5 MB, 7 pages @ 200 dpi)
   source page   1 -> 1654x2339 px      (consolidated results table, headings, bullets)
   source page   4 -> 1654x2339 px      (JPL segment financials)
   source page   5 -> 1654x2339 px      (JPL operational table, UoM column)
   source page   7 -> 1654x2339 px      (Reliance Retail financials)
   source page   8 -> 1654x2339 px      (Retail operational table, small numerics)
   source page  11 -> 1654x2339 px      (O2C financials + narrative)
   source page  21 -> 1700x2200 px      (dense unaudited financial statements)

verification that no text layer survived:
   pages               7
   extractable chars   0
   text blocks         0
   words               0
   embedded fonts      0 []
   embedded images     7
   IMAGE-ONLY          True
```

The pipeline correctly classifies it as **scanned** on ingestion.

## 4. Results

### Image-only PDF, VISUAL_ONLY mode

| | |
|---|---|
| Pages | **7 / 7**, 0 failed |
| OCR elements | 846 (mean confidence 0.80) |
| Layout regions | 79 |
| Tables | 9 |
| Table cells | 607 (461 numeric, 100% parse rate) |
| Charts | 0 |
| Financial facts | 251 |
| Validation checks | 104 — 0 failures |
| Provenance | **100%** with a bounding box |
| Review items | 16 (6%) |
| **Precision / Recall / F1** | **1.00 / 1.00 / 1.00** (81/81) |
| Unit accuracy | 1.00 |
| Period resolved | 100% |
| Processing time | 28.9 s (4.1 s/page) |
| **Native text used for extraction** | **False** |

### Three modes, image-only PDF

| | native text (naive) | native text (strong) | **VISUAL_ONLY** |
|---|---|---|---|
| Pages processed | 0 | 0 | **7** |
| OCR elements | 0 | 0 | **846** |
| Tables | 0 | 0 | **9** |
| Table cells | 0 | 0 | **607** |
| Financial facts | 0 | 0 | **251** |
| Facts with bounding box | 0 | 0 | **251** |
| Validation checks | 0 | 0 | **104** |
| **F1** | **0** | **0** | **1.00** |
| Extraction used PDF text | true | true | **false** |

Both text baselines return literally nothing, because there is nothing to read.

### Three modes, original digital PDF (34 pages)

| | native text (naive) | native text (strong) | **VISUAL_ONLY** | HYBRID |
|---|---|---|---|---|
| Pages processed | 34 | 34 | 34 | 34 |
| OCR elements | 0 | 0 | 2,936 | 2,936 |
| Layout regions | 0 | 0 | 336 | 344 |
| Tables | 0 | 0 | 30 | 30 |
| Table cells | 0 | 0 | 1,663 | 1,663 |
| Financial facts | 1 | 78 | 395 | 397 |
| Numeric facts | 1 | 78 | 392 | 393 |
| Facts with period | 1 | 78 | 366 | 368 |
| Facts with unit | 1 | 4 | 368 | 369 |
| Facts with currency | 0 | 0 | 308 | 309 |
| Facts with growth | 0 | 0 | 94 | 96 |
| Facts with bounding box | 0 | 0 | 395 | 397 |
| Facts with provenance | 0 | 0 | 395 | 397 |
| Validation checks | 0 | 0 | 118 | 119 |
| Ground-truth matched | 0 | 4 / 85 | **85 / 85** | **85 / 85** |
| Precision | 0 | 0.114 | **1.00** | **1.00** |
| Recall | 0 | 0.047 | **1.00** | **1.00** |
| **F1** | **0** | **0.067** | **1.00** | **1.00** |
| Processing time | 4.8 s | 2.6 s | 125 s | 131 s |
| Extraction used PDF text | true | true | **false** | true |

The strong text baseline pairs labels with figures correctly but still scores
F1 0.067, because it cannot tell which **column** — and therefore which
reporting period — a figure belongs to. It keeps the first number on each line
and discards the comparatives.

**Visual-only and hybrid are within two facts of each other.** The text layer
adds corroboration and confidence, not capability.

## 5. Financial OCR verified on image-only pages

Counted directly in the OCR output of the image-only fixture — no glyphs
available:

| Feature | Hits | Examples |
|---|---|---|
| Thousands separators | 231 | `325,290`, `340,257`, `273,252` |
| Decimal numbers | 71 | `24.5`, `10.1`, `15.9` |
| Negatives in parentheses | 29 | `(27)`, `(0)` |
| Basis-point changes | 3 | `(210 bps)`, `(80 bps)`, `(100 bps)` |
| Percent signs | 46 | `up 24.5% Y-o-Y` |
| Quarter labels | 18 | `1Q`, `4Q` |
| Fiscal-year labels | 24 | `FY26`, `FY27` |
| Growth column headers | 6 | `% chg.` |
| Unit declarations | 4 | `( in crore)` |
| "crore" keyword | 23 | in bullets and captions |
| **Rupee glyph `₹`** | **0** | outside the PP-OCR charset |
| `Rs.` / `INR` keyword | 0 | this document uses the symbol only |

### The currency limitation, and how it is handled honestly

PP-OCR's recognition charset cannot emit `₹`, so on a scanned page the symbol is
simply absent — and this document never spells out "Rs." either. Falling back to
the PDF text layer is exactly what visual-only forbids.

The system instead infers the currency from the document's **own visible text**:
`crore` and `lakh` are read from the pixels like any other word and belong only
to the Indian numbering system, so an amount carrying one is in rupees. That
inference is:

- made from OCR output, not from the glyph stream;
- flagged as `currency_inferred = true`, distinct from a symbol that was read;
- penalised slightly in confidence.

Result: 200 of 251 facts on the image-only PDF carry `INR`. The remainder are
percentages, ratios and counts, which correctly have no currency.

**Honest limitation:** a scanned document that uses `₹` and never writes "crore",
"lakh" or "Rs." would leave the currency `UNKNOWN` in visual-only mode. Reading
the symbol from pixels would need either a recogniser whose charset includes it
or a template match for the glyph. Neither is implemented; the gap is reported
rather than papered over.

## 6. Table reconstruction from OCR alone

All 9 tables on the image-only pages, rebuilt with no glyph coordinates:

| Page | Shape | Confidence | Detector | Header cells | Numeric cells | Periods resolved | Unit |
|---|---|---|---|---|---|---|---|
| 1 | 18×7 | 0.98 | whitespace+lines | 13 | 91 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | crore |
| 2 | 13×7 | 0.98 | whitespace+lines | 13 | 65 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | crore |
| 3 | 6×8 | 0.98 | whitespace+lines | 13 | 31 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | — |
| 4 | 15×7 | 0.98 | whitespace+lines | 13 | 78 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | crore |
| 5 | 6×8 | 0.98 | whitespace+lines | 13 | 30 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | crore |
| 5 | 3×4 | 0.88 | whitespace+lines | 9 | 2 | — | million |
| 6 | 8×7 | 0.98 | whitespace+lines | 13 | 36 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | crore |
| 7 | 24×6 | 0.98 | **ruling+lines** | 8 | 80 | Q1 FY27, Q4 FY26, Q1 FY26, FY26 | crore |
| 7 | 16×6 | 0.98 | **ruling+lines** | 2 | 48 | — | crore |

Every cell carries a non-zero bounding box in rendered-image pixel space. Page 7
(the dense statements page) used **ruling lines detected in the bitmap** — a
signal that only exists visually.

## 7. Provenance chain

For a fact extracted from the image-only PDF:

```
fact       gross_revenue  Q1 FY27 = 90,408 crore INR
page       4              method = table_cell
bbox       x=766  y=1036  w=110  h=37        (rendered-image pixels, 200 dpi)
cell       cell_cf1f8ae10c9d  in  table_35def47f6cfe   (row 3, col 2, role=data)
ocr text   '90,408'
ocr ids    ['ocr_863855eff2a0']
  -> element ocr_863855eff2a0: text='90,408'  source=ocr
     bbox=(766, 1036, 110, 37)  corroborated=False  aux_text=None

chain:  rendered page image -> OCR element -> table cell -> bounding box
        [no PDF text spans involved at any step]
```

`source=ocr`, `corroborated=False` and `aux_text=None` are positive evidence
that the text layer never touched this fact.

The dashboard resolves the same chain visually: opening the fact's source panel
loads a crop of the exact area (`/pages/1/crop?x=766&y=1690&w=105&h=35`, 121×51 px)
and draws the box on the full rendered page at relative coordinates
(left 46.31%, top 72.25%). Verified in the browser against the image-only
document, whose header displays `SCANNED` and `VISUAL-ONLY` chips.

## 8. What the audit fixed

The audit was not a formality — it surfaced four real defects.

1. **The guard was inactive in worker threads.** `ContextVar`s do not cross into
   a `ThreadPoolExecutor`. Fixed by copying the context per task; pinned by a
   test.
2. **Content-hash OCR cache corrupted a second ingestion.** Cached elements
   carried the *original* document's ids, so re-ingesting the same file violated
   the primary key. Element ids are now rebound to the processing document, and
   the recogniser and the cache loader share one id function so a warm and a cold
   run produce identical ids.
3. **Header absorption depended on the text layer's bold flag.** With no text
   layer, statement tables lost their header rows entirely and 40% of facts were
   left with no period. Replaced with a visual prose test; period resolution went
   from 0.61 to 0.93 in visual-only, and table detection improved in **both**
   modes (24 → 30 tables).
4. **Unit resolution scanned narrative prose.** A page whose bullets mention
   "533 million subscribers" declared its rupee table to be in millions. Unit
   resolution is now strictly layered — the row's own unit-of-measure cell, then
   an explicit `(Rs in crore)` declaration, and never loose prose. Unit accuracy
   went from 0.91 to 1.00 in **both** modes.

A fifth issue was a genuine validation flaw in both modes: facts with **no**
period were being bucketed together, letting a margin rule divide one quarter's
profit by another quarter's revenue. Period-less facts are now excluded from
cross-fact rules — "unattributed" is not a period — which removed the one
spurious failure.

## 9. Tests

`tests/test_visual_only.py` — 21 tests, all passing:

- the fixture genuinely has no text, words or fonts (guarding the guard)
- corroboration is blocked in visual-only and permitted in hybrid
- metadata and baseline scopes stay permitted in both
- the `textlayer` module itself refuses in visual-only
- **the boundary holds inside worker threads**
- the mode is restored after the context exits
- a PDF with no text layer processes end to end
- extraction never touched native text (ledger assertion)
- OCR produced bounding boxes, all `source=ocr`, none corroborated
- layout regions and tables were derived, with periods from OCR-read headers
- facts extracted, with the group's and a subsidiary's figures kept distinct
- currency inferred from visible text, not glyphs
- provenance points at rendered-image coordinates, inside the page, with
  relative coordinates in 0..1
- provenance resolves to real OCR element ids
- validation ran on visually extracted facts
- hybrid and visual-only remain observably different, yet extract the same
  headline figure
- visual-only does not claim to have found a text layer

Full suite: **226 passed, 1 skipped**.

## 10. Reproducing

```bash
cd backend

# build the image-only fixture (verifies it carries no text)
python scripts/make_image_only_pdf.py \
  ../data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf \
  ../data/benchmark/RIL_Q1_FY27_IMAGE_ONLY.pdf --pages 1,4,5,7,8,11,21

# process it with the text layer hard-blocked
python cli.py ingest ../data/benchmark/RIL_Q1_FY27_IMAGE_ONLY.pdf --mode VISUAL_ONLY

# score it, and compare all three approaches
python cli.py evaluate <document_id>
python cli.py modes ../data/benchmark/RIL_Q1_FY27_IMAGE_ONLY.pdf \
  --visual <visual_document_id> --hybrid <hybrid_document_id>

python -m pytest tests/test_visual_only.py -v
```

## 11. Remaining limitations

- **The rupee symbol cannot be read from pixels.** Currency is inferred from
  Indian magnitude words and flagged as inferred; a scanned rupee document that
  never writes "crore", "lakh" or "Rs." would leave currency `UNKNOWN`.
- **Typography is coarser without a text layer.** Font size falls back to the OCR
  box height and boldness is unavailable, so heading detection rests on size,
  case, position and length. After the header-absorption fix this costs nothing
  measurable on the benchmark, but a document that distinguishes sections *only*
  by weight would be harder.
- **`is_scanned` is not determined in visual-only.** The mode deliberately does
  not look for a text layer, so it reports `has_text_layer = false` rather than
  claiming knowledge it did not seek.
- **The ground-truth keys are spot checks** — 85 facts for the full document, 81
  for the image-only fixture — chosen to span entities, periods, value types and
  table shapes. They are not exhaustive coverage of all 395 extracted facts.
