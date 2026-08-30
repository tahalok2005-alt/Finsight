# API reference

Base URL: `/api/v1` · Interactive docs: `/docs` · OpenAPI: `/openapi.json`

All responses are JSON unless stated. Errors use a single envelope:

```json
{ "error": { "code": "document_not_found", "message": "…", "detail": {} } }
```

| Status | Meaning |
|---|---|
| 400 / 422 | invalid request or unusable document (corrupt, empty, encrypted) |
| 404 | document, page or fact not found |
| 409 | duplicate upload (same content hash) |
| 413 | file exceeds the size limit |
| 415 | unsupported file type |
| 503 | an engine (e.g. OCR) is unavailable |

Internal exceptions are logged with a full traceback and returned as a generic
`internal_error` — internals are never leaked to the client.

---

## System

### `GET /health`
Liveness plus a real dependency check.

```json
{
  "status": "ok",
  "app": "FinSight", "version": "1.0.0", "environment": "dev",
  "database": { "ok": true, "error": null },
  "workers": { "active": 0, "max": 2 }
}
```

### `GET /system`
The engines and configuration actually in force — useful for reproducibility.
Reports each OCR engine's real availability (an engine that cannot run says so
rather than claiming it can), render settings, validation tolerances, confidence
thresholds, limits, and whether the optional local LLM is enabled.

### `GET /overview`
Dashboard summary across every document: counts by status, extraction volume,
confidence-band distribution, top metrics, validation summary, review queue
size, recent documents and job counts.

### `GET /metrics`
The financial taxonomy: every canonical metric with its display name, family,
value type and aliases.

---

## Documents

### `POST /documents`
Upload a PDF. `multipart/form-data` with a `file` field, optional
`allow_duplicate` (boolean).

Validates content-based file type, size and PDF structure **before** accepting,
then queues background processing and returns immediately. The returned status
is the real one (`QUEUED`) — never a premature success.

```json
{
  "id": "c8970f5722bb4163a7a0c1c4",
  "original_filename": "Media_Release_RIL_Q1_FY2026-27.pdf",
  "content_hash": "…", "size_bytes": 858218,
  "page_count": 34, "status": "QUEUED",
  "job_id": "…", "warnings": []
}
```

`409` if the same content was already ingested; the error detail carries the
existing `document_id`.

### `GET /documents`
List documents. Query: `limit`, `offset`, `status`, `search`. Each item carries
its extraction counts (`fact_count`, `table_count`, `validation_warning_count`,
`review_queue_count`, …).

### `GET /documents/{id}`
One document, plus the section structure inferred from its content.

### `GET /documents/{id}/status`
Live processing status. **Progress is computed from stages that actually
completed** — it is never a timer or an estimate.

```json
{
  "document_id": "…", "status": "REQUIRES_REVIEW", "progress": 1.0,
  "current_stage": null,
  "stages": [
    { "stage": "ocr", "status": "SUCCEEDED", "engine": "rapidocr",
      "items_total": 34, "items_done": 34, "items_failed": 0,
      "duration_ms": 61234.5, "error": null, "detail": {} }
  ],
  "pages_total": 34, "pages_done": 34, "pages_failed": 0,
  "error": null, "run_id": "…", "engine_versions": {}
}
```

Stages, in order: `validating`, `rendering`, `ocr`, `layout`,
`document_assembly`, `tables`, `charts`, `extraction`, `validating_facts`.

### `POST /documents/{id}/reprocess`
Re-run the pipeline. Query: `force_ocr` (boolean) to bypass the OCR cache.
Returns `202` with a `job_id`. Derived rows are replaced; **review history is
preserved**.

### `DELETE /documents/{id}`
Delete the document, every derived row (cascade) and its stored files. `204`.

---

## Pages and imagery

### `GET /documents/{id}/pages`
Every page with render dimensions, DPI, OCR statistics and per-page counts.

### `GET /documents/{id}/pages/{n}`
One page. Query: `include_regions` (default true), `include_ocr` (default
false).

Regions and OCR elements carry **both** pixel `bbox` and relative
`bbox_relative` (0..1), so an overlay works at any zoom without knowing the
render DPI.

```json
{
  "page_number": 1, "render_width": 1654, "render_height": 2339, "dpi": 200,
  "ocr_element_count": 149, "ocr_mean_confidence": 0.9016,
  "regions": [
    { "id": "region_…", "type": "table", "confidence": 0.98,
      "reading_order": 9, "text": "…", "detector": "heuristic-v1",
      "bbox": { "x": 46, "y": 1004, "width": 1560, "height": 660 },
      "bbox_relative": { "x": 0.0278, "y": 0.4292, "width": 0.9432, "height": 0.2822 } }
  ]
}
```

### `GET /documents/{id}/pages/{n}/image`
The rendered page as PNG.

### `GET /documents/{id}/pages/{n}/thumbnail`
Reduced-width PNG.

### `GET /documents/{id}/pages/{n}/crop?x=&y=&w=&h=`
A crop of the exact source area, in page pixel coordinates. This is what backs
the evidence view. Cached on disk.

---

## Financial facts

### `GET /documents/{id}/financials`

Filters: `metric`, `entity`, `period`, `nature`, `kind`, `band`,
`validation_status`, `requires_review`, `min_confidence`.
Sorting: `sort` (`page|confidence|metric|period|value`), `order`.
Paging: `limit`, `offset`.

Returns the page of facts plus `facets` (all metrics, entities and periods
present) so a UI can build filters without a second call.

```json
{
  "items": [{
    "fact_id": "fact_4727e48159f6",
    "metric": "Gross Revenue", "canonical_metric": "gross_revenue",
    "entity": "Consolidated", "kind": "table_cell", "nature": "actual",
    "value": 340257.0, "corrected_value": null, "effective_value": 340257.0,
    "raw_value": "340,257", "value_type": "monetary",
    "currency": "INR", "unit": "crore", "base_value": 3402570000000.0,
    "period": "Q1 FY27", "period_type": "quarter",
    "fiscal_year": 2027, "quarter": 1,
    "comparison_period": "Q1 FY26", "comparison_value": 273252.0,
    "growth": 24.5, "growth_type": "YoY", "growth_reported": true,
    "confidence": 0.9076, "confidence_band": "HIGH",
    "confidence_breakdown": {
      "ocr": 0.93, "layout": 0.98, "structure": 0.98, "semantic": 1.0,
      "consistency": 1.0, "corroboration": 0.7, "overall": 0.9076,
      "band": "HIGH", "notes": ["text-layer agrees with OCR"]
    },
    "validation_status": "PASSED", "requires_review": false,
    "source_page": 1, "original_text": "340,257",
    "provenance": {
      "page_number": 1, "table_id": "table_…", "cell_id": "cell_…",
      "bbox": { "x": 750, "y": 1076, "width": 105, "height": 35 },
      "bbox_relative": { "x": 0.4534, "y": 0.4600, "width": 0.0635, "height": 0.0150 },
      "extraction_method": "table_cell",
      "page_image_url": "/api/v1/documents/…/pages/1/image",
      "crop_url": "/api/v1/documents/…/pages/1/crop?x=750.0&y=1076.0&w=105.0&h=35.0"
    }
  }],
  "total": 400,
  "facets": { "metrics": [...], "entities": [...], "periods": [...] }
}
```

Notes on the shape:

- `value` is the extraction; `corrected_value` is a human correction;
  `effective_value` is what to display. **The extraction is never overwritten.**
- `unit` and `currency` are what the document said. `base_value` is an
  *additional* convenience field, `null` when the magnitude is unknown.
- `growth_reported` distinguishes a figure the document stated from one computed
  here.
- Percentage metrics carry `meta.change_pp` / `meta.change_bps` instead of a
  misleading relative growth.

### `GET /documents/{id}/financials/{fact_id}`
One fact with the validation results that touched it and its review history.

---

## Structure

### `GET /documents/{id}/tables`
Reconstructed tables. Query: `page`, `include_cells`.

Each table returns a `grid` (rows × cols of text) for quick rendering **and**
`cells` with per-cell role, numeric value, confidence and both coordinate
spaces. `columns` exposes the interpretation: header text as read, the resolved
period, whether it is a growth column, and its numeric ratio.

### `GET /documents/{id}/charts`
Detected charts with type, confidence, title, axis labels, legend, categories
and any **printed** data labels.

`values_extracted` is `false` and `notes` explains why whenever no data labels
were printed — values are never inferred from bar geometry. `requires_review` is
always `true`.

### `GET /documents/{id}/commentary`
Topic-classified narrative. Query: `topic`, `sentiment`. Returns the passages
plus a topic histogram.

### `GET /documents/{id}/guidance`
Forward-looking statements only, with `target_value`, `target_period`,
`direction` and the trigger phrase that identified them. **Never mixed into the
fact set** — the response carries a `note` restating that.

---

## Validation, provenance, review

### `GET /documents/{id}/validation`
Query: `status`, `family`. Returns each check with both the reported and the
calculated value, the difference, the tolerance and the facts it touched, plus a
summary and a per-family breakdown.

```json
{
  "items": [{
    "rule": "growth_matches_values", "rule_family": "growth", "status": "PASSED",
    "message": "Reported YoY of 24.5% for Gross Revenue (Q1 FY27) agrees with the value pair.",
    "reported_value": 24.5, "calculated_value": 24.5213, "difference": -0.0213,
    "tolerance": 0.6, "fact_ids": ["fact_…"], "page_numbers": [1]
  }],
  "summary": { "PASSED": 116, "WARNING": 4, "REQUIRES_REVIEW": 5 },
  "by_family": { "growth": { "PASSED": 48 }, "margin": { "PASSED": 23, "WARNING": 3 } }
}
```

### `GET /documents/{id}/provenance/{fact_id}`
The full evidence chain for one fact: page, region (with its type and text),
table cell (row, column, role), bounding box in both coordinate spaces, the
original OCR text, the OCR element ids, the page render dimensions, and URLs for
the page image and the exact crop.

### `GET /documents/{id}/reviews`
The review queue, worst first (failed validation, then warnings, then low
confidence). Query: `limit`, `offset`, `include_resolved`.

### `POST /documents/{id}/reviews`

```json
{
  "target_type": "fact",
  "target_id": "fact_…",
  "action": "approve | reject | edit | comment | reopen",
  "reviewer": "analyst",
  "note": "verified against the source",
  "new_value": { "value": 340257 }
}
```

An `edit` stores `corrected_value` **alongside** the extracted value and records
the action in an immutable log. Once the queue empties, the document's status
moves from `REQUIRES_REVIEW` to `COMPLETED`.

### `GET /documents/{id}/reviews/history`
Every review action, optionally filtered by `target_id`.

---

## Search, comparison, export

### `GET /search?q=&document_id=`
Across documents, financial facts, table cells, commentary and guidance.
Minimum two characters.

### `GET /compare?left=&right=&left_period=&right_period=&entity=&metrics=`
Compares two documents, or two periods within one.

Each row labels its `change_basis`:

- `reported` — the document stated this change
- `calculated` — computed here from the two values
- `not_comparable` — refused, with a `note` saying why (differing currency,
  value type, or an unconvertible unit)

Where units differ but are convertible, the conversion is performed and recorded
in `note`. Each side is labelled `extracted` or `reviewed`. Also returns
guidance added, retained and removed between the two.

### `GET /documents/{id}/export?format=json|csv|xlsx`
Downloads with a `Content-Disposition` filename.

- **JSON** — full structured export: document metadata (including the settings
  snapshot), facts, tables with cells, charts, commentary, guidance and
  validation. `include=` selects sections.
- **CSV** — one row per fact, UTF-8 with BOM so Excel opens it correctly.
- **XLSX** — five sheets: Financial Facts, Validation, Table Cells, Commentary,
  Guidance, with frozen headers and auto-filters.

All formats preserve the full evidence chain: metric, canonical metric, value,
raw value, unit, currency, period, comparison period, growth and its basis,
confidence, validation status, source page, region, table, cell and bounding box.
