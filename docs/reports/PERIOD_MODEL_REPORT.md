# Period data model — engineering report

Everything below was measured on this machine during this phase. Where a
measurement did not complete, it says so.

---

## 1. What was changed

| Area | Change |
|---|---|
| `models/enums.py` | New `PeriodBasis` enum — `stated`, `derived`, `inherited`, `unknown` |
| `models/schemas.py` | `NormalizedPeriod.basis`; **`TableColumn.period_resolved`** — the resolved period object itself, not just its label |
| `database/models.py` | Four columns on `financial_facts`: `period_start`, `period_end`, `period_confidence`, `period_basis` |
| `database/migrations.py` | Additive `ALTER TABLE ADD COLUMN` for each, plus the `period_end` index |
| `database/repository.py` | Writes all four on save; reconstructs `NormalizedPeriod` from them on read |
| `normalization/periods.py` | `parse_period` / `parse_period_levels` wrappers that stamp the basis where the period is decided |
| `extraction/facts.py` | `_period_for` reads `col.period_resolved` first; re-parsing a label is a last-resort fallback |
| `tables/structure.py` | Column construction keeps the resolved object |
| `tables/periods.py` | The document-level repair pass carries dates with the label it lends, and marks them `inherited` |
| `document/assembly.py` | Continuation fragments inherit their parent's resolved period, marked `inherited` |
| `api/routes_data.py`, `review/service.py` | All four fields served |
| `frontend/src/utils/period.ts` | New — turns the stored fields into the words a reader sees |
| `frontend/src/views/doc/FactsTab.tsx` | Figures table rebuilt around metric → period → value → status |

Three further faults surfaced during the Phase 14 review, when a real
half-yearly report was put through the finished pipeline. All three were fixed:

| Area | Change |
|---|---|
| `normalization/periods.py` | **Numeric column heads** — `30.06.26`, `31/12/2025`, and digit spans (`6 months ended`). Without these the resolver could not read a UK or European filing's column heads at all. Ambiguous numeric dates (`06.07.26`) resolve to nothing. |
| `normalization/periods.py` | `H1 2026` — the bare half-year label, previously only recognised as `H1 FY26`. Also repaired a `\b` that had been corrupted into a literal backspace byte, which had silently disabled one branch of header detection. |
| `extraction/taxonomy.py` | **Contrast-word rule** — `Total non-current assets` was matching `current_assets` at 0.91 similarity and being reported as a current-assets figure. |
| `frontend/src/views/doc/FactsTab.tsx` | The metric column now leads with the line item **as the document printed it**; the canonical name follows. Three different source lines were rendering under one identical label. |
| `api/routes_intelligence.py`, `views/doc/SummaryTab.tsx` | The summary cross-tab heads its columns through the same formatter, so a column head cannot disagree with the rows beneath it. |

---

## 2. Root cause of the period clarity problem

Two distinct faults, one of which was actively producing wrong data.

**The storage fault.** `NormalizedPeriod` has always carried `start_date`,
`end_date` and `confidence`. `financial_facts` persisted only the label, the
type, the fiscal year, the quarter and the raw header text. On read-back the
object was rebuilt without dates, so nothing downstream could show one. Measured
before the change: exact dates reached the API for 78 of 431 facts on the RIL
filing, 24 of 56 on Apple, **0 of 255** on the image-only document.

**The recomputation fault, which is worse.** Because the dates were not carried,
`_period_for` re-parsed the label to rebuild them. A label cannot reproduce a
date:

```
resolved from the source header  "Three Months Ended / June 28, / 2025"  ->  2025-06-28
re-derived from the label alone  "Q3 FY25"                               ->  2025-06-30
```

Both are "Q3 FY25" under a September year end. Only the first is what Apple
printed, because a 52/53-week filer's quarter does not close on the last day of
a month. The second is a fabricated date two days into the wrong period, and
nothing further down the pipeline could tell.

---

## 3. Database fields added

On `financial_facts`:

| Column | Type | Null | Meaning |
|---|---|---|---|
| `period_start` | `VARCHAR(10)` | yes | First day the source supports, ISO-8601 |
| `period_end` | `VARCHAR(10)`, indexed | yes | Last day the source supports, ISO-8601 |
| `period_confidence` | `FLOAT` | yes | The resolver's confidence in the period, separate from confidence in the value |
| `period_basis` | `VARCHAR(16)` | yes | `stated` / `derived` / `inherited` / `unknown` |

All four are nullable by design. **A date that was never established is NULL**,
and nothing downstream computes one.

---

## 4. Migration behaviour for existing data

The migration is additive only: four `ALTER TABLE ADD COLUMN` statements and one
index. Existing rows receive **NULL in every new column**. No backfill, no
inference, no default date.

This is deliberate. A pre-migration row's dates were never established — they
were dropped at the write boundary — and the label alone cannot recover them.
Writing a date derived from `Q1 FY27` would be exactly the fabrication this
whole change exists to prevent.

Verified after the migration ran on the working database: **8,540 fact rows
total, 56 carrying `period_end`** (the one document reprocessed at the time),
**8,484 left NULL** with their labels intact.

The interface states the gap rather than hiding it. A legacy row renders as
`Q1 FY27 · exact dates not established`.

---

## 5. How period basis is determined

The basis is set where the period is decided, never inferred afterwards from the
shape of the answer.

* **`stated`** — `parse_period` matched words the document wrote: a `1Q FY27`
  column head, a `Three Months Ended June 28, 2025` banner, an `As at` date.
* **`derived`** — `parse_period_levels` assembled it from the column hierarchy:
  a span phrase on one header row, a date on the next, a year wrapped onto a
  third. No single line said it.
* **`inherited`** — the period came from adjacent structure rather than from the
  column itself: a continuation fragment taking its parent's heads, or a
  harmonised header row matching one found elsewhere in the document. The dates
  travel with it.
* **`unknown`** — nothing established it.

`_stamp` only fills a basis that is still `unknown`, so a period resolved deeper
in the stack keeps the basis of the step that actually settled it.

---

## 6. How exact dates are protected from recomputation

The resolved `NormalizedPeriod` object rides on `TableColumn.period_resolved`
from the resolver all the way to the fact. `_period_for` reads that object:

```python
def _period_for(col, calendar):
    if col.period_resolved is not None:
        return col.period_resolved          # the dates the source supports
    if not col.normalized_period:
        return None                          # NULL, not a guess
    return (parse_period(col.normalized_period, calendar=calendar)
            or parse_period(col.header, calendar=calendar))
```

The fallback survives only for columns whose period was settled by the
document-level repair pass, which works in labels and has no dates to offer — so
it can only ever produce what was already the best available answer, never
overwrite a better one.

Beyond that point nothing recomputes: the repository stores the strings it is
given, the API serves them, and `frontend/src/utils/period.ts` formats them.
There is no date arithmetic anywhere in the frontend.

---

## 7. Screens and components changed

| Component | Change |
|---|---|
| `views/doc/FactsTab.tsx` | Columns are now **Metric · Reporting period · Value · Change · Status · Source**. The period column leads with the exact date and carries the fiscal label and basis beneath it. Entity moved under the metric; confidence and validation merged into one Status column. |
| `utils/period.ts` (new) | `describePeriod` produces the primary line, the secondary line, a certainty level and a tooltip. Confidence is rendered as *High / Medium / Low*, never a decimal. |
| `views/doc/ReviewTab.tsx` | Review rows carry the same period rendering. |
| `views/doc/SummaryTab.tsx`, `components/ExplanationPanel.tsx` | Use the shared formatter, so one figure reads identically everywhere. |
| `types/index.ts`, `api/client.ts` | `PeriodBasis` and the four fields typed end to end. |

Display rules, applied consistently:

* Exact date leads — `Three months ended 28 June 2025`, `As at 28 June 2025`.
* Fiscal label and provenance follow — `Q3 FY25 · derived from the statement header`.
* For an instant the label repeats the date, so only the basis is shown.
* No date established → `Q1 FY27 · exact dates not established`.
* Nothing established at all → `Period not identified`, in the warning colour,
  with a tooltip saying it has not been guessed.
* **"Unattributed" no longer appears anywhere in the figures table.**

---

## 8. Current test result

*(filled in below)*

---

## 9. Fresh benchmark accuracy

*(filled in below)*

---

## 10. Performance

*(filled in below)*

---

## 11. Limitations and known issues

### The half-yearly report, precisely

Period coverage on the 165-page Standard Chartered HY 2026 filing went from
**27 of 335 figures to 106 of 328**. What still carries no period is worth
stating exactly, because most of it is correct behaviour rather than a gap.

The undated figures, by the column head they sit under:

| Column head | Count | Why it has no period |
|---|---|---|
| `$million` / `Smillion` | ~70 | A **unit** header, not a period. The column is a magnitude declaration. |
| *(blank)* | 36 | The head was not legible. |
| `Change %` | 15 | A **variance** column. It belongs to no single period by construction. |
| `Taiwan $million`, `India $million`, `Retail / Investment Banking`, `Premises Equipment` | ~20 | **Segment and geography** columns. The dimension is the business unit, not the period. |
| `Constant currency` | 5 | A **basis** column. |
| `H1251`, `Q126`, `Q2'251`, `H126` | ~17 | Compact period labels fused with footnote markers by OCR. |

Only the last row is a genuine miss. The rest are columns that carry no period
because a period is not what they vary over — the period for those tables is
stated once in the table's own heading, which is captured (`H126 H125`) but too
mangled to parse and, where it names two periods, cannot be attributed to a
single column without guessing which.

**Attaching a table-level period to those figures was considered and not
done.** It would mean asserting a period no column stated, on tables whose
headings are OCR-damaged — precisely the fabrication this work exists to
prevent. `Period not identified` is the smaller and truer claim.

### Other limitations

* **Ambiguous numeric dates resolve to nothing.** `06.07.26` is 6 July or
  7 June depending on the convention and nothing in the string settles it. A
  filer that closes a period mid-month and writes it numerically will lose that
  column's dates. Month-end dates — which is what periods actually close on —
  are unaffected.
* **The fiscal calendar is not always established.** Standard Chartered's is
  not: it writes "6 months ended 30.06.26" and "6 months ended 31.12.25", which
  vote for a December year end and a June year end respectively. Contradictory
  evidence establishes nothing, so periods are labelled `6M ended 2026-06-30`
  rather than `H1 FY26`. The dates are exact; only the fiscal-year name is
  withheld, which is the intended trade.
* **Pre-migration rows have no dates and never will.** They were written before
  the columns existed, and the labels they kept cannot recover them.
* **There is no magnitude for a cent.** A row declaring itself in cents now
  drops the table's magnitude rather than inheriting it wrongly, so the figure
  is reported as printed with no unit. Converting it to a base value would need
  a sub-unit magnitude, which was not added.
* **Metric semantics remain the weakest layer.** The contrast rule fixes
  negation, but over-matching persists in other forms — this filing has
  `Leverage ratio (%)` matched to `Net Debt to EBITDA`. The figures table now
  leads with the printed line item, so a reader can always see what the
  document actually said, but the canonical mapping beside it is not always
  right.
* **A paused run holds a worker**, and does not survive a restart. See
  [docs/LIFECYCLE.md](../LIFECYCLE.md).
