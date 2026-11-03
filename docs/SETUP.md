# Setup

## Requirements

| | |
|---|---|
| Python | 3.11 or newer (developed and tested on 3.13.7) |
| Node | 18 or newer (tested on 24.6) |
| Disk | ~1.5 GB for dependencies, plus ~30 MB per 34-page document processed |
| RAM | 4 GB free is comfortable; peak usage is roughly one page bitmap per worker |
| GPU | **not required** — everything runs on CPU |
| Network | needed once, to install dependencies |

There is **no** system OCR binary to install, no Poppler, no Ghostscript, no
API key and no model download step. The OCR models ship inside the
`rapidocr-onnxruntime` wheel (~12 MB).

## Install

```bash
git clone https://github.com/tahalok2005-alt/Finsight.git
cd Finsight

python -m venv .venv
```

Activate the environment:

```bash
.venv\Scripts\activate
```

```bash
source .venv/bin/activate
```

Then install both sides:

```bash
pip install --upgrade pip
pip install -r backend/requirements.txt

cd frontend
npm install
cd ..
```

## Run

### 1. Initialise the database

```bash
cd backend && python cli.py init-db
```

Creates `data/finsight.db` with all 16 tables. Safe to re-run.

### 2. Start the API

```bash
cd backend && python -m uvicorn app.main:app --reload --port 8000
```

- API root: <http://localhost:8000/api/v1>
- Interactive docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/api/v1/health>

### 3. Start the dashboard

In a second terminal:

```bash
cd frontend && npm run dev
```

Open <http://localhost:5173>. The dev server proxies `/api` to port 8000, so
page images and evidence crops resolve directly from the URLs the backend
records in provenance.

### 4. Process a document

Drag a PDF onto the upload page, or use the command line:

```bash
cd backend && python cli.py ingest ../data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf
```

Expect roughly 2 minutes for 34 pages on a typical laptop CPU. Subsequent runs
of the same file are much faster because OCR is cached against the file's
content hash.

## Command line

```bash
cd backend

python cli.py init-db [--drop]        # create (or recreate) the schema
python cli.py engines                 # which OCR engines are actually usable
python cli.py ingest <file.pdf>       # ingest and process, printing a summary
python cli.py ingest <file.pdf> --mode VISUAL_ONLY   # block the PDF text layer
python cli.py list                    # all documents with status and counts
python cli.py status <document_id>    # one document's counts and metadata
python cli.py show <document_id>      # extracted facts as a table
python cli.py export <document_id> --format json|csv|xlsx [-o out]
python cli.py evaluate <document_id>  # metrics, accuracy, baseline comparison
python cli.py baseline <file.pdf> --document-id <id>   # baseline only
python cli.py modes <file.pdf> --visual <id> --hybrid <id>   # three-way comparison
```

Useful flags: `ingest --force-ocr` bypasses the OCR cache;
`ingest --allow-duplicate` re-ingests a file already stored under the same
content hash.

## Configuration

Everything is configurable through environment variables prefixed
`FINSIGHT_`, or a `.env` file at the repository root. Copy `.env.example` to
`.env` to start. All defaults are what the acceptance run used.

The settings that matter most:

| Variable | Default | Effect |
|---|---|---|
| `FINSIGHT_DATABASE_URL` | `sqlite:///./data/finsight.db` | PostgreSQL works unchanged |
| `FINSIGHT_RENDER_DPI` | `200` | Higher improves OCR on small table text; costs time and memory |
| `FINSIGHT_OCR_ENGINE` | `rapidocr` | `tesseract` also implemented, needs the system binary |
| `FINSIGHT_OCR_DET_LIMIT_SIDE_LEN` | `1600` | Detector input cap; lower is faster and misses small text |
| `FINSIGHT_GROWTH_TOLERANCE_PCT` | `0.6` | Percentage points allowed between reported and recomputed growth |
| `FINSIGHT_MARGIN_TOLERANCE_PCT` | `0.35` | Same, for margins |
| `FINSIGHT_TOTAL_TOLERANCE_REL` | `0.02` | Relative tolerance on subtotal and identity checks |
| `FINSIGHT_REVIEW_THRESHOLD` | `0.65` | Facts below this confidence enter the review queue |
| `FINSIGHT_PAGE_WORKERS` | `2` | Pages analysed in parallel; raise on a machine with more cores and RAM |
| `FINSIGHT_WORKER_THREADS` | `2` | Documents processed concurrently |
| `FINSIGHT_MAX_UPLOAD_MB` | `200` | Upload size cap |
| `FINSIGHT_LOG_JSON` | `false` | JSON structured logs for aggregation |

Processing mode is chosen per run rather than by configuration: `HYBRID` is the
default, and `--mode VISUAL_ONLY` (CLI) forbids the PDF text layer from reaching
extraction. The mode and its access ledger are recorded on every processing run
and returned by `GET /documents/{id}/status`.

The effective settings are recorded on **every processing run** (`settings_snapshot`
on `processing_runs`), so a result can always be tied back to the configuration
that produced it. `GET /api/v1/system` shows what is currently in force.

### PostgreSQL

The schema is PostgreSQL-compatible with no changes:

```bash
pip install "psycopg[binary]"
export FINSIGHT_DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/finsight"
cd backend && python cli.py init-db
```

## Testing

```bash
cd backend && python -m pytest
```

About 1,320 tests, roughly 14 minutes (the pipeline tests run real OCR). To skip those:

```bash
python -m pytest -m "not slow"
```

With coverage:

```bash
python -m pytest --cov=app --cov-report=term-missing
```

## Production build

```bash
cd frontend && npm run build          # emits frontend/dist
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Simplest: let the API serve the build itself, from the same address, by
setting `FINSIGHT_STATIC_DIR=../frontend/dist`. Or serve `frontend/dist` from
any static host and route `/api` to the backend. The container in the
repository root does the former — see [DEPLOY.md](DEPLOY.md).

Use `--workers 1`: the job queue is in-process, so multiple uvicorn workers
would each run their own queue. To scale horizontally, replace
`app/workers/queue.py` with a broker-backed implementation — the API depends
only on `submit` and the job rows.

For deployment, set:

```bash
FINSIGHT_ENVIRONMENT=prod
FINSIGHT_LOG_JSON=true
FINSIGHT_CORS_ORIGINS=["https://your-domain"]
FINSIGHT_DATABASE_URL=postgresql+psycopg://...
```

## Troubleshooting

**`rapidocr-onnxruntime is not installed`** — reinstall requirements inside the
activated virtual environment. Check with `python cli.py engines`, which reports
honestly which engines can actually be constructed.

**Processing is slow.** OCR dominates at roughly 13 s/page at 200 dpi on a
laptop CPU. Raise `FINSIGHT_PAGE_WORKERS` if you have cores and RAM to spare, or
lower `FINSIGHT_RENDER_DPI` to 150 — but check the accuracy impact on your own
documents first, because small table text suffers.

**A document is stuck in a processing state.** Check `GET /documents/{id}/status`
for the stage that failed and its recorded error, and the server log for the
full traceback. Errors are never swallowed. Restarting the API leaves the job
row in `running`; use the Reprocess button or `POST /documents/{id}/reprocess`.

**"This document has already been ingested."** Duplicates are detected by
content hash. Re-upload with `allow_duplicate=true`, or open the existing
document — the error response includes its id.

**Page images do not load in the dashboard.** Confirm the backend is on port
8000 (the Vite proxy target) and that `data/renders/` is writable.

**Unicode errors on a Windows console.** Set `PYTHONIOENCODING=utf-8` — the
documents contain the rupee sign.

**Tests crash with an access violation.** Only relevant if you modify the test
fixtures: the API tests use an inert job queue on purpose, because a real
background job outliving its test races the teardown of the in-memory SQLite
database.

## Resetting

```bash
rm -rf data/uploads data/renders data/cache data/finsight.db*
cd backend && python cli.py init-db
```

`data/benchmark/` is preserved — it holds the acceptance PDF and its
version-controlled ground-truth key.
