# Final correctness and robustness patch

Two demonstrated weaknesses from final validation, fixed or honestly closed.
No new subsystem, no architectural redesign, no guarantee weakened.

---

## 1. What was wrong, and what changed

### Patch A — hierarchical spatial period resolution

The period parser read a column head as a *line*. A statement writes it as a
*stack*, and often prints the stack above the band of figures that layout
analysis cuts as the table. Five distinct defects followed from that, each
fixed generically:

| # | Defect | Fix |
|---|---|---|
| A1 | The existing `TableColumn.header_path` already modelled a multi-level head; nothing consumed it geometrically. | Reused it. **No duplicate table model was created.** |
| A2 | A head spread over rows only parsed when the fragments happened to land in a parseable order. | Each level takes the fragment whose box overlaps that column's band most; a fragment reaching across two or more bands is a banner shared by all of them; a year alone on a row rejoins the date above it. **No fragment is required to share a text line.** |
| A2 | The head was frequently *outside* the table region. | The reconstruction walks upward from the top of the band, taking lines within the table's horizontal extent that read as header fragments, stopping at the first that does not. A line beginning inside the row-label column is a page title, not a banner. |
| A3 | Nothing consumed the complete hierarchy; a balance-sheet date under no span word was given a quarter from page context. | `parse_period_levels` consumes every level. A date with no span anywhere in its stack resolves to an instant. The page-context span guess was **removed**. |
| A4 | (Preserved) `June 28,` read the day as a fiscal year → fabricated `9M FY29`. | The existing guard is intact and now pinned by a named test. **June 28 → FY28 cannot recur.** |
| A5 | An unestablished year end silently became a March convention. | `FiscalCalendar.is_known` is honoured everywhere, including the flat parser that the document-level repair pass calls. |

Two further generic causes were found while fixing the above and are worth
naming, because both produced *confidently wrong* answers:

- **Cross-table harmonisation conflated a stock with a flow.** Comparing header
  texts with their span words stripped made a balance sheet's `June 28, 2025`
  identical to an income statement's `Three Months Ended June 28, 2025`. It now
  borrows a meaning only when the *whole* header row is recognised: a statement
  continued repeats all of its heads, a different statement merely shares one
  date. Table-local evidence is also settled first, since a sibling column
  under the same banner says more than a same-dated column on another page.
- **A banner said what a group of columns shared; a leaf said what one column
  was — and the banner won.** A page titled "results for the quarter ended 30th
  June, 2026" collapsed columns headed `1Q FY27` and `1Q FY26` onto one period.
  The leaf now wins, and among the readings that parse the shortest span wins,
  so a stacked `1Q` over `FY27` still reads as `Q1 FY27` rather than `FY27`.

### Patch B — degraded scan robustness

| # | Item | Outcome |
|---|---|---|
| B1 | Image-quality signals, no ML model | **Shipped.** Skew, noise, ink-to-paper contrast, illumination drift, sharpness, effective text height — statistics over pixels, each with a stated meaning. |
| B2 | Selective preprocessing | Built (deskew, denoise, CLAHE, unsharp, upscale — each gated on its own measurement). **Not shipped**, see §5. |
| B3 | OCR candidate selection with provenance | Built (per-defect repair, invertible affine, boxes mapped back, 3% margin, `ocr_image` recorded). **Not shipped**, see §5. |
| B4 | Table-specific preservation | Held by construction: ruling lines, whitespace valleys and column geometry are read from the *original* image, never from a candidate. Now moot, and still true — extraction only ever reads the rendered page. |
| B5 | Degraded-scan evaluation | **Measured. Preprocessing did not help.** See §5. |
| B6 | Clean-document regression | **None.** See §3. |

---

## 2. Apple — before and after

The generalization filer: USD, millions, September year end, US GAAP.

| | Before | After |
|---|---|---|
| Facts | 56 | 56 |
| Tables / cells | 5 / 363 | 5 / 363 |
| **Facts with a period** | **10 / 56** | **56 / 56** |
| **Periods correct** | **0 of 10** (`9M FY26`, `Q1 FY25`…) | **all, hand-checked** |
| Entities | 2 | 2 |
| Currency USD | 52 / 56 | 52 / 56 |
| Provenance | 100% | 100% |

Every distinct column head, checked against the source PDF:

| Statement | Head, as printed | Resolved | Correct |
|---|---|---|---|
| Operations | `Three Months Ended` / `June 28,` / `2025` | `Q3 FY25` | yes |
| Operations | `Three Months Ended` / `June 29,` / `2024` | `Q3 FY24` | yes |
| Operations | `Nine Months Ended` / `June 28,` / `2025` | `9M FY25` | yes |
| Operations | `Nine Months Ended` / `June 29,` / `2024` | `9M FY24` | yes |
| Balance sheet | `June 28,` / `2025` | `As at 2025-06-28` | yes |
| Balance sheet | `September 28,` / `2024` | `As at 2024-09-28` | yes |
| Cash flows | `Nine Months Ended` / `June 28,` / `2025` | `9M FY25` | yes |
| Cash flows | `June 29,` / `2024` | `9M FY24` | yes |

Independently verified against Apple's published figures: net income Q3 FY25
$23,434M, Q3 FY24 $21,448M, 9M FY25 $84,544M; total assets $331,495M at 28 June
2025 and $364,980M at the FY24 close of 28 September 2024. The fiscal labels and
the balance-sheet instants are both right.

**The September year end is never written in the filing.** It is derived from
the statement's own arithmetic: a nine-month column closing on the same date as
a three-month column is the year to date, so the year began nine months before
28 June and ends in September. The shorter column closing on the same date is
what makes this an observation rather than an assumption; contradictory
evidence yields no calendar at all.

Why the flat approach could not work, from the source's own text order:

```
Three Months Ended / Nine Months Ended / June 28, / 2025 / June 29, / 2024 /
June 28, / 2025 / June 29, / 2024
```

Both banners precede all four dates. No proximity rule over that sequence can
pair them; only geometry can.

---

## 3. Regression — the documents that already worked

| Document | Mode | Before | After |
|---|---|---|---|
| RIL media release (85-fact key) | HYBRID | F1 0.9765 | **F1 0.9882** (84/85) |
| RIL media release | VISUAL_ONLY | F1 0.9765 | **F1 0.9882** (84/85) |
| RIL image-only (81-fact key) | HYBRID | F1 0.9877 | **F1 0.9877** (80/81) |
| RIL image-only | VISUAL_ONLY | F1 0.9877 | **F1 0.9877** (80/81) |

RIL improved by one fact; image-only is unchanged. Facts with a period rose
from 402 to 406 on RIL. Nothing regressed.

---

## 4. Degraded scan — before and after

| | Before | After |
|---|---|---|
| Facts | 251 | 251 |
| **F1 vs 81-fact key** | **0.667** (47/81) | **0.695** (49/81) |
| Precision / recall | 0.783 / 0.580 | 0.817 / 0.605 |
| Provenance | 100% | 100% |
| Flagged for review | 72 | 45 |
| Pages measured as degraded | not measured | **7 / 7** |

**The gain is from Patch A, not from Patch B.** Every preprocessing candidate
was rejected on every page.

The degraded document turns out to be the best demonstration of requirement 8
in the wild. OCR damages the text that states the year end, so the calendar
**cannot be established — and the system declines to assume March, even though
March is in fact correct for this filing.** What it does instead:

- fiscal labels the document writes out and OCR still reads survive:
  `Q1 FY27`, `Q4 FY26`, `Q1 FY26`, `FY26`;
- every other column keeps its exact date and drops the fiscal label:
  `3M ended 2026-06-30`, `3M ended 2026-03-31`, `12M ended 2026-03-31`.

The same clean pages produce `Q1 FY27 … As at 2026-06-30` throughout, with no
date-only durations, because there the year end is established.

This has a real cost worth naming: the same underlying quarter can appear under
two labels in one degraded document, which fragments the period space and will
cause a comparison to miss a match. That is the price of not guessing, and it
is the right side to err on — a missed match is visible, a wrong fiscal year is
not.

---

## 5. Does preprocessing help? No — measured, then removed

Twelve variants through the same OCR engine on the two worst degraded pages,
scored by total confidence mass:

| Variant | Page 3 | Page 5 |
|---|---|---|
| unsharp ×0.6 | −0.1% | +0.5% |
| unsharp ×1.2 | +0.7% | +0.8% |
| upscale ×1.5 | −0.2% | **+1.3%** |
| upscale ×2.0 | −0.3% | +0.9% |
| upscale ×1.5 + unsharp | +0.8% | +0.1% |
| upscale ×2.0 + unsharp | +0.0% | +0.7% |
| CLAHE | −0.8% | +0.9% |
| CLAHE + unsharp | −0.4% | +0.4% |
| Otsu | +0.1% | −0.4% |
| adaptive threshold | −0.4% | +0.6% |
| upscale ×1.5 + adaptive | **−2.0%** | **+1.3%** |

Best +1.3%, inside run-to-run noise; several negative. Wired in behind a
selective per-defect repair and a 3% margin, **every candidate on every page
was rejected**, at 134.7 s against 76.6 s — 1.8× the wall clock for nothing.

Element counts explain it: 80 → 80 on page 3, 85 → 85–86 on page 5. The
detector finds the *same text regions* either way. What 150 dpi and JPEG
quality 55 destroyed is stroke detail inside those boxes, and no filter puts
back information that is gone.

**So the repair pass was deleted and the simpler architecture kept**, exactly
as B5 requires. What remains is the measurement, which earns its place on its
own terms:

| Document | Pages | Flagged | Defects |
|---|---|---|---|
| RIL media release | 34 | **0** | — |
| RIL image-only | 7 | **0** | — |
| Apple statements | 3 | **0** | — |
| RIL scanner-degraded | 7 | **7** | blur ×7, low contrast ×7, skew ×3 |

Zero false positives across 44 clean pages of three documents; every degraded
page caught, with the defects it actually has. It is now persisted per page and
served by the API, so a bad result can be explained rather than guessed at.

Two thresholds are set from measurement rather than theory, and both cut
against the obvious choice:

- **Text height** fires below 16px, not the recogniser's nominal 48px training
  height. The benchmark's dense statement pages measure 19–21px and read at
  F1 0.988, so type that size is demonstrably fine; a 22px threshold flagged a
  third of a clean document.
- **Uneven illumination** is measured and reported but deliberately **not
  actionable**. On this benchmark it cannot separate a lighting gradient from a
  printed design — the media release's coloured masthead measures 0.15–0.16,
  *higher* than the deliberately vignetted scan at 0.12–0.13, and a plane fit
  reverses the ordering rather than separating them. A signal that cannot tell
  the defect from the design must not drive a transformation.

This is a result about this fixture and this recogniser, not a general claim
that preprocessing never helps.

---

## 6. The ten absolute safety requirements

| # | Requirement | Verified by |
|---|---|---|
| 1 | VISUAL_ONLY must not use native text | All 8 runs: `extraction_fed_by_native_text=False`, `corroboration_accesses=0` in VISUAL_ONLY. Boundary tests pass, including inside worker threads. |
| 2 | No company-specific logic | AST audit of every `app/**/*.py`: every occurrence of a filer token is a comment, docstring or attribute docstring. **Zero in executable code.** |
| 3 | No Apple-specific logic | Same audit — the string "apple"/"aapl" does not appear in `app/` at all. Every Apple fix is geometric or arithmetic. |
| 4 | No filename-specific logic | No production path reads the filename. (`_title_stem` in `assembly.py` strips "(continued)" from a *table title*; renamed for clarity.) |
| 5 | No page-number-specific logic | Every `page_number` comparison is relational — adjacency, bounds, query filters. No branch on a specific page. |
| 6 | No hardcoded benchmark answers | Grep for every benchmark figure: all occurrences are illustrative text in comments or docstrings. |
| 7 | Provenance on every fact | 100% across all 8 document/mode runs. `ocr_image` is `"original"` on every page — extraction only ever reads the rendered page, so every box addresses pixels the reader can be shown. |
| 8 | Wrong information never preferred over UNKNOWN | An unestablished year end yields dated-but-unlabelled periods. An explicit `FY26` survives but its span dates stay unset. Contradictory calendar evidence yields no calendar. Ambiguous span evidence yields no span. Partial header matches no longer harmonise. |
| 9 | Existing tests remain passing | **324 passed, 1 skipped** (was 294 + 1; 30 tests added, none removed or weakened). |
| 10 | Fix generic causes, not symptoms | Every fix is stated as a rule about geometry or arithmetic. The named regression tests are built from synthetic geometry, not from either benchmark document. |

---

## 7. Validation performed

Full backend suite; all four documents in both modes; cross-document
comparison; VISUAL_ONLY boundary; frontend build and lint; end-to-end API.

| Check | Result |
|---|---|
| Backend suite | 324 passed, 1 skipped (re-run after the baseline fix) |
| RIL — HYBRID / VISUAL_ONLY | F1 0.9882 / 0.9882, provenance 100% / 100% |
| RIL image-only — both modes | F1 0.9877 / 0.9877, provenance 100% |
| Apple — both modes | 56/56 periods, provenance 100% |
| Degraded — both modes | F1 0.695 / 0.695, provenance 100% |
| VISUAL_ONLY isolation | `corroboration_accesses=0` on all four documents |
| Cross-document comparison | clean vs degraded: 58 metrics compared; cross-filer runs clean |
| Frontend | `tsc -b && vite build` exit 0; lint warnings only, one tidied |
| API end-to-end | 16/16 checks — health, documents, financials, tables, pages, validation, entities, JSON and CSV export; all 56 Apple facts served with period and provenance; pages report capture quality |

---

## 8. Tests added

30 new tests, all named for the behaviour they pin:

- **`TestHierarchicalPeriodHeads`** (10) — a head split over three rows; a
  banner read from whichever row states it; a year wrapped onto its own row; a
  bare year alone; a date with no span as an instant; "as at" beneath a span
  word; a stacked label; a leaf beating its banner; day-vs-fiscal-year; an
  inherited span of any length.
- **`TestUnknownFiscalCalendar`** (6) — the default calendar is marked unknown;
  a derived period keeps its dates and drops the fiscal year; an emitted label
  round-trips through the repair pass; an instant stays an instant; an
  explicitly written fiscal label still stands; a calendar year needs no fiscal
  calendar.
- **`TestHierarchicalColumnHeads`** (4) — built from bare geometry: a head
  printed above the band is still found; a banner reaches every column it
  covers; a page title is not mistaken for a banner; an unknown year end dates
  the columns without labelling them.
- **`TestImageQuality`** (8) — a clean page has no defects; skew is measured and
  signed; blur, small text and low contrast are each detected; uneven
  illumination is observed but not acted on; the measurement is reported per
  page; extraction only ever reads the rendered page.
- Two additions to `TestPeriods` covering the unknown-calendar contract.

Five existing tests in `TestPeriods` were updated, not weakened: they called
`parse_period` with no calendar and asserted a March-convention fiscal label.
They now state their calendar explicitly and still assert the same labels, with
the new honesty guarantee asserted alongside.

---

## 9. Two issues found and deliberately *not* fixed

Both are outside this patch's stated scope, both are independent of the period
and quality machinery, and both are surfaced rather than asserted by the system:

1. **`gross_margin` is typed as a percentage on Apple.** Apple's income
   statement has a *dollar* "Gross margin" line; the taxonomy treats the metric
   as a ratio, producing `43,718 percent`. Confidence is 0.64 and the fact is
   flagged for review, so it is not asserted — but it is wrong. A generic guard
   (a percentage-typed value far outside a plausible range is not a percentage)
   would fix it, and would change value typing across every document, which is
   not a change to make at the end of a validation patch.
2. **One `net_income` fact carries `thousand` where the page says "(In
   millions)"** — a 1000× error on 9M FY24. Same reasoning.

Neither is a regression from this patch: unit and value-type assignment share
no code path with period resolution or capture measurement.

---

## 9b. Two things the fresh evaluation caught

Regenerating the published figures from a clean cold+warm run surfaced two
problems that a patch report written from the patch alone would have missed.

**The academic baseline was being unfairly penalised by my own change.** The
text-layer baseline calls `parse_period` with `DEFAULT_CALENDAR`, which this
patch marked "unknown". Its periods therefore became `3M ended 2026-06-30`
where the answer key says `Q1 FY27`, and its score fell from the published
**F1 0.067 (4/85) to 0.000 (0/85)** — not because it read anything worse, but
because the comparison had quietly stopped being like-for-like. The visual
pipeline establishes its calendar from the document; the baseline now does the
same from the text it reads, which restores it to exactly 0.067 (4/85). Left
alone this would have flattered every "visual versus text" table in the
documentation.

**Review counts moved, including downwards.** Dating Apple's facts dropped its
review queue from 44 to 6, and RIL's from 102 to 64 — facts were previously
flagged for missing periods they now have. On the degraded scan the count fell
from 72 to 45, which weakens a claim the documentation had been making: review
flags "nearly tripled" under degradation is now "nearly double" (25 → 45), with
validation warnings quadrupling (6 → 24). Both docs were corrected downwards
rather than left standing.

Alongside these, every published figure was regenerated and several stale
claims removed — including three separate **"F1 1.00"** assertions and an
"85 / 85 matched" that the current 85-fact key does not support.

---

## 10. Known limitations, restated honestly

- **A fiscal year end the document never evidences stays unknown.** Periods
  keep exact dates and carry no fiscal year. That is the intended behaviour,
  not a gap to be closed by a default.
- **Scanner degradation costs accuracy and cannot be preprocessed away**
  (§5). F1 0.695 against 0.988 on the same pages.
- **Generalization is measured on one additional filer.** Apple is evidence
  that the mechanism is geometric rather than tuned to one document. It is not
  a claim about every statement layout in existence.
- **The answer keys are spot checks** — 85 facts for RIL, 81 for the image-only
  and degraded fixtures. They are not exhaustive coverage of every extracted
  fact, and the reports say so wherever accuracy is quoted.
- All pre-existing limitations (chart values only when printed, heuristic
  layout, OCR dropping currency glyphs, optional commentary summarisation,
  single-node deployment) are unchanged.

---

## 11. Files changed

**Production**

| File | Change |
|---|---|
| `app/normalization/periods.py` | `parse_period_levels` gains explicit-label precedence and shortest-span preference; `parse_period` honours `is_known` and keeps explicit labels without inventing their dates; `looks_like_period_fragment` added. |
| `app/tables/structure.py` | Header assembly rebuilt around levels; banner detection by bands touched; `_header_levels_above` recovers a head printed above the band; page-context span guess removed; header-block span requires an unambiguous block. |
| `app/tables/periods.py` | Table-local evidence settled before cross-table; harmonisation requires a whole-row match; span inheritance covers any span length and reads the hierarchy; calendar inference gains the cumulative-column reading. |
| `app/document/assembly.py` | A repeated table title no longer blocks continuation inheritance. |
| `app/rendering/quality.py` | **New.** Capture-quality measurement. |
| `app/services/pipeline.py` | Measures quality per page and records it; passes page lines to table building. |
| `app/models/schemas.py`, `app/database/models.py`, `app/database/repository.py`, `app/api/routes_documents.py` | `image_quality` and `ocr_image` persisted and served. |
| `app/database/session.py` | `init_db` now runs `migrate()` — the migration module existed but nothing called it, so a database written by an earlier version failed every query after a schema change. |
| `app/evaluation/baseline.py` | The text baseline establishes the document's fiscal calendar the same way the visual pipeline does, so the comparison stays like-for-like. |
| `frontend/src/views/doc/SummaryTab.tsx` | Ternary-as-statement replaced with `if`/`else`. |
| `frontend/src/types/index.ts` | `ImageQuality` type; `PageInfo` gains `image_quality` and `ocr_image`. |

**Documentation** — `README.md`, `docs/ARCHITECTURE.md`, `docs/EVALUATION.md`,
`docs/VISUAL_ONLY.md`, `docs/SETUP.md` updated with measured results; stale test
counts and the superseded Apple and degraded-scan figures corrected.

**Deleted** — `app/rendering/preprocess.py`, the selective-repair pass, after it
was measured and found not to help.

---

## 12. What was not done

No new subsystem. No architectural redesign. No new dependency. The visual-only
pipeline, the native-text boundary, the provenance guarantee, the validation
engine, the review workflow and the financial-intelligence layer are untouched
except where a period label flows through them.
