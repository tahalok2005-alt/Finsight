# Performance — how FinSight spends its time, and what was changed (Phase 22)

Every number in this document is a **controlled measurement**: one run at a
time, a fresh process per run, its own cache directory, nothing else of ours
running. Cold and warm runs are reported side by side and never averaged
together. A corpus run's wall time is *not* a measurement: two corpus-run
timings in this phase were contaminated by the machine being suspended
mid-run (FAILURE_KB X4), which is exactly why the rule exists.

## Tools

| Tool | What it measures |
|---|---|
| `backend/scripts/perf_benchmark.py --doc KEY ...` | cold then warm, per document: wall, engine load, each stage, pages/s, facts/s, peak working set (OS counter), OCR cache hits/misses, second-read reads/cache, page time P50/P90/P95 |
| `backend/scripts/perf_benchmark.py --knee KEY --workers 1,2,4,...` | the parallelism sweep: cold runs at each page-worker count, each in its own empty cache |
| `backend/scripts/perf_second_read.py --tag TAG KEY ...` | the second-read stage alone, clip vs page strategy, on a recorded run's facts, empty second-read cache each time |

A cold run sets `FINSIGHT_CACHE_DIR` **and** `FINSIGHT_RENDER_DIR` to an empty
directory the tool creates: the OCR cache, the second-read cache and the page
renders all start empty. The main cache (`data/cache`) is never read, written
or deleted (the Phase 16/17 scratch scripts that emptied it are not used).

## Where the time goes

RESULTS_PROFILE

## The benchmark matrix

RESULTS_MATRIX

## Parallelism

RESULTS_KNEE

## Caching — the levels that exist

| Level | Key | Saves |
|---|---|---|
| OCR per page | content hash + page + render signature (dpi, format) + OCR signature (engine, version, settings) | recognition of an unchanged page |
| Second read per cell | content hash + page + cell box + dpi + padding + **strategy** + engine version | the 400 dpi re-read of an unchanged cell |
| Page render (new, Phase 22) | content hash + page + dpi + format + **MuPDF version** | rendering an unchanged page under a new document id |
| Page render (per document) | document id + page + dpi + format | serving the page image to the viewer |

Content-hash processing: the OCR and second-read caches were already keyed by
the file's sha256, so a re-upload of identical bytes reused recognition. Its
renders were keyed by document id and were re-made for every new id -- every
re-upload, and every evaluation tag. They are now shared by content hash (a
hard link where the volume allows, a copy otherwise); the per-document path the
API serves is unchanged.

## Improvements made — each measured, each checked for correctness

RESULTS_IMPROVEMENTS

## Investigated and not changed

**Text-first routing** (reading a native PDF's text layer instead of OCR'ing
its pages). The evaluation runs in VISUAL_ONLY mode -- pixels only -- by
definition, so text-first routing cannot be part of the evaluated pipeline.
The product's HYBRID mode already uses the native text layer for extraction
on native pages. What the corpus says about the opportunity: ROUTING_TEXT

**Page-type routing** (skipping OCR or table analysis on pages that hold no
figures -- auditors' reports, notices, certificates). Deciding what a page
holds requires reading it; a cheap low-resolution pre-read could decide, but
no measurement of what such a pre-read would miss exists, and a page skipped
wrongly is a statement silently lost. What the corpus says about the size of
the opportunity: ROUTING_PAGES

**Selective high resolution**: the second read IS selective high resolution
-- only table figures' cells are read at 400 dpi. Raising the page read's
resolution for all pages was measured in Phase 17 and rejected (cost without
accuracy gain on the benchmark).

## Checkpoint and resume

RESULTS_RESUME

## The processing screen's estimate

The estimate is measured throughput on this run -- pages done against elapsed
time -- shown only after two pages and eight seconds of evidence, and
withdrawn when the pages are done (the remaining stages have no per-page
rate). Phase 22 found one path to "~0s": a warm run reads cached pages in
milliseconds and a sub-half-second remainder rounded to "0s". It now reads
"under 1s", and only while pages remain.
