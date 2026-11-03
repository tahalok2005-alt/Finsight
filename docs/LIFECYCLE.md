# Document lifecycle

How a document moves from upload to results, and what a user can do to it on
the way.

---

## The state machine

```
                    upload
                      │
                      ▼
                  UPLOADED ──────────► QUEUED ◄──────────┐
                                          │              │
                                          ▼              │ re-queue
                              VALIDATING → RENDERING     │
                              → OCR_PROCESSING           │
                              → LAYOUT_ANALYSIS          │
                              → TABLE_ANALYSIS           │
                              → EXTRACTION               │
                              → VALIDATING_FACTS         │
                              → ANALYSING                │
                                 │      │      │         │
                     pause ──────┘      │      └──── cancel
                        │               │              │
                        ▼               ▼              ▼
                     PAUSED         COMPLETED       CANCELLED ──┐
                        │       PARTIALLY_COMPLETED             │
                 resume │         REQUIRES_REVIEW               │
                        │              FAILED ──────────────────┤
                        └──────► (back to processing)           │
                                                                │
             server restart while active ──► INTERRUPTED ───────┘
```

Three groupings matter more than the individual names, and the code exposes
them as properties on `DocumentStatus` so no caller has to re-derive them:

| Property | Meaning | Members |
|---|---|---|
| `is_active` | a worker is occupied with this document | `QUEUED` … `ANALYSING`, `PAUSED` |
| `is_terminal` | nothing further happens unless asked | `COMPLETED`, `PARTIALLY_COMPLETED`, `REQUIRES_REVIEW`, `FAILED`, `CANCELLED`, `INTERRUPTED` |
| `has_results` | opening it shows extracted data | `COMPLETED`, `PARTIALLY_COMPLETED`, `REQUIRES_REVIEW` |

`PAUSED` is deliberately **active, not terminal**: a paused run still holds its
worker and its in-memory state. That is what makes resuming exact rather than a
replay, and it is also its cost — see the limitation at the end.

The frontend never re-implements these rules. `GET /documents/{id}/control`
returns `can_pause`, `can_resume`, `can_cancel`, `can_requeue`, `can_open` and
`can_delete`, and the same flags ride along on every row of the document
listing, so a button the server would refuse is never offered.

---

## Pause and cancel

Both go through one small object, `app.core.control.Control`, which the
pipeline consults at points where stopping is safe.

```python
# app/services/pipeline.py, inside the per-page worker
def work(page_number: int) -> PageArtifacts:
    self._checkpoint(page=page_number, total=total, stage="ocr")
    return self._process_one_page(renderer, engine, page_number, probe)
```

**The checkpoint is inside the worker, not the loop that consumes results.**
`ThreadPoolExecutor.map` submits every page at once, so pausing the consumer
would leave the workers grinding through all 165 pages regardless. Checking
before each page starts means a pause lands within one page per worker, and a
cancel drains the queue almost immediately because each remaining page raises
at its own checkpoint instead of doing any OCR.

There are also checkpoints between the sequential stages that follow the page
loop — assembly, tables, charts, extraction — so a document stopped after OCR
does not have to finish the whole analysis first.

### Pause

`request_pause()` clears an `threading.Event`. The next checkpoint records
where it stopped, publishes `PAUSED` with a note (`"Paused at page 41 of
165"`), and blocks on `Event.wait()`. The worker thread is held on purpose:
nothing is serialised to disk, so resuming continues from exactly where it
stopped and cannot duplicate work.

`request_resume()` sets the event, clears the note, and immediately writes an
active status back — waiting for the next stage write left the UI claiming
`PAUSED` for seconds after the run had carried on, which reads as a control
that did not work.

### Cancel

`request_cancel()` sets a flag and wakes any paused waiter, so a paused run is
never unstoppable. The next checkpoint raises `ProcessingCancelled`, which
carries the page and stage it stopped at.

**Policy: a cancelled run persists no extraction.** This is not a clean-up
step; it falls out of the architecture. Persistence happens once, after the
pipeline returns, so a run that unwinds has written nothing. The document is
left exactly as it was before the run, with `CANCELLED` and a note saying where
it stopped. Nothing half-built is ever presented as a whole result.

What *is* kept is the rendered pages and the OCR output, which live in a
content-hash-keyed cache on disk rather than against the document id. So
re-queueing a cancelled document is fast — it re-reads only the pages it never
reached.

`ProcessingCancelled` is deliberately allowed to escape `Pipeline.run()`
rather than being converted into a `FAILED` result: a document the user
stopped must not be recorded as one that broke.

---

## Deletion

`DELETE /documents/{id}` removes the document and everything derived from it.

If a run is in flight it is cancelled first, and the request waits for the run
to unwind (bounded by `delete_cancel_timeout_s`, default 30s — in practice one
page of OCR). If it has not stopped in time the request returns **409** and
says so, rather than deleting the row out from under a worker still writing to
it.

Cleanup then happens in two parts:

* **Database.** `session.delete(doc)` cascades. ORM relationships cover pages
  and runs (and stage rows through the run); the intelligence tables —
  entities, table semantics, derived metrics, relations, risks, insights,
  anomalies — carry `ondelete="CASCADE"` at the schema level, enforced because
  the SQLite connection sets `PRAGMA foreign_keys=ON`.
* **Disk.** `storage.delete_document_files` removes the document's directory:
  the uploaded PDF, every rendered page image and its thumbnail.

`tests/test_lifecycle.py::TestDeletion` asserts that twelve child tables all
reach zero and that the directory is gone.

---

## Progress, and why it survives a refresh

Progress is not held in the browser. Each stage writes a `StageRun` row as it
reports, and the per-page loop republishes its counts as pages complete, so
`GET /documents/{id}/status` is answerable at any moment by anyone.

The frontend polls: the document row every 2s while it is active (it carries
the status *and* the control flags), the status endpoint every 1.5s, and the
listing every 2.5s while anything is moving — stopping entirely when nothing
is. Polling was chosen over SSE or WebSockets because the state is already
durable and queryable; a push channel would add a second source of truth and a
reconnection problem in exchange for a second of latency.

The one derived number is the estimate, and it is derived from throughput
measured on the run being watched — pages completed against elapsed time. It
does not appear until enough pages have finished to measure a rate, because an
estimate made from two data points is a guess with a progress bar attached.

---

## Restart

Processing state lives partly in memory: the worker threads and the control
objects. A restart destroys them while the database keeps whatever the last
write said, so any document recorded as active is a document nothing is working
on.

`reconcile_interrupted_runs()` runs at startup and marks each one
`INTERRUPTED`, with a note explaining what happened and what to do. Its runs
are closed out and any stage still claiming `RUNNING` is marked cancelled, so
no progress view is left waiting for a number that will never change.

---

## Known limitations

* **A paused run holds a worker.** State is kept in memory rather than
  serialised, which is what makes resume exact — but the queue is sized for it
  (`worker_threads`, default 3). Pause more documents than that and nothing new
  starts until one is resumed or cancelled. Serialising mid-pipeline state
  would remove the constraint at the cost of the guarantee that a resume cannot
  duplicate work; that trade was not made.
* **Pause and cancel are page-granular.** A request issued mid-page waits for
  that page's OCR, which on a dense page is a few seconds.
* **A paused run does not survive a restart.** It becomes `INTERRUPTED` like
  any other in-flight run and must be re-queued; the OCR cache makes that
  cheap, but it is a re-queue, not a resume.
