# Phase 15 — layout-aware financial extraction

Every number below was measured on this machine during this phase.

---

## 1. Architecture, before and after

### Before

One route from a page to a fact, and it required a grid.

```
PDF ─► render ─► OCR ─► layout ─► assemble ─► tables ─► charts
                                                  │
                                                  ▼
                                        ┌──────────────────┐
                                        │  extract_from_   │
                                        │  table           │  ← the only
                                        └────────┬─────────┘    route for a
                                                 │              number
                              regions ──► narrative (sentences)
                                                 ▼
                          reconcile ─► validate ─► confidence ─► persist ─► UI
```

A figure stated as a tile — a label with its number beneath it — reached
nothing. `RegionType.KPI_BLOCK` existed and was routed to the *sentence*
extractor; `FactKind.KPI` existed and was never used.

### After

The page's structure decides which strategy reads it.

```
PDF ─► render ─► OCR ─► layout ─► assemble ─► tables ─► charts
                          │                      │
                          │  (OCR geometry)      │  (boxes of tables that
                          │                      │   actually yielded facts)
                          ▼                      ▼
   ┌───────────────────────────────────────────────────────────┐
   │                      EXTRACTION                            │
   │                                                            │
   │   tables ──────────────► extract_from_table                │
   │                                                            │
   │   OCR elements ────────► detect_cards ──► extract_from_    │
   │   minus ground a table    (geometry)      cards            │
   │   already explained                                        │
   │                                                            │
   │   regions ─────────────► extract_narrative_facts           │
   └───────────────────────────┬───────────────────────────────┘
                               │  one canonical FinancialFact
                               ▼
             reconcile ─► validate ─► confidence ─► persist ─► UI
```

Three new modules, no change to how a fact is scored, validated, stored or
shown:

| Module | Responsibility |
|---|---|
| `spatial/geometry.py` | column clustering, vertical runs, gap statistics |
| `spatial/cards.py` | `detect_cards` → tile candidates carrying their evidence |
| `spatial/extract.py` | candidates → canonical facts, through the existing taxonomy, normaliser and period resolver |

**Nothing was duplicated.** Metric matching, number normalisation, period
resolution, entity attribution, confidence scoring and provenance are the same
code the table strategy uses. A tile is a different *shape* of evidence, not a
different kind of financial figure.

---

## 2. The evidence the design rests on

Page 2 of the IDFC deck holds the document's headline figures and produced
**zero facts**. Read from the OCR geometry already stored:

```
 column 0 (cx~330)     column 1 (cx~1020)     column 2 (cx~1710)     column 3 (cx~2400)
 Loans & Advances 291  Total Deposits    290  CASA Deposits     290  CASA ratio     286
 US$ 31,447 Mn.   410  US$ 32,931 Mn.    407  US$ 16,734 Mn.    407  50.8%          407
 (20% YoY|5% QoQ) 465  (18% YoY|6% QoQ)  460  (25% YoY|8% QoQ)  459  (283 bps YoY)  459

 GNPA ratio       643  NNPA ratio        643  Net Interest Marg 621  Risk Adjusted  621
                                             (Q1-FY27)         666  (Q1-FY27)      659
 1.51%            772  0.44%             773  5.96%             772  4.75%          765
```

Three measurable properties, and each became a rule:

* **Tiles align on a centre.** Card parts share an x-centre; they rarely share
  a left edge, because each is a different length. Clustering on the left edge
  splits one tile into three.
* **A tile is a vertical run.** Gaps inside a tile ran 0–92 px; gaps between
  tiles ran 120–156 px, against a median of 60. The split threshold is a
  multiple of the layout's own median gap, not a pixel count, because a slide
  and a statement have different spacing and neither is wrong.
* **The layout repeats.** Four columns with the same internal shape at the same
  heights. A tile is only accepted if another run starts level with it — one
  label above one number is a guess; four across a line is a layout.

---

## 3. Extraction strategies

| Strategy | Reads | Status |
|---|---|---|
| **Table** | reconstructed grids: row label × period column | unchanged |
| **Spatial KPI** | tiles: label, optional period, figure, optional change note | **new** |
| **Narrative** | sentences in the document's prose | unchanged |
| **Chart** | chart structure and metadata | metadata only — no values read from pixels |

### How a tile becomes a fact

```
┌──────────────────────┐
│ Profit After Tax     │  label   → taxonomy.match_metric  → pat
│ (Q1-FY27)            │  period  → parse_period           → Q1 FY27, basis=stated
│ US$ 114 Mn.          │  value   → normalize_number       → 114.0 USD million
│ (132.4% YoY|237% QoQ)│  change  → kept as context, never as the figure
└──────────────────────┘
```

### What is refused, and why

Each refusal is counted and named in the diagnostics rather than being silent:

| Refusal | Reason |
|---|---|
| `tile_not_repeated_across_the_layout` | one label above one number is not a grid |
| `no_scale_or_currency_on_a_monetary_tile` | nothing on a tile declares thousands or millions; a table's caption would |
| `value_is_a_multiple_not_an_amount` | "2.5X" beside Total Deposits is growth, not a balance |
| `percentage_value_for_a_monetary_metric` | a chart legend's "Retail Deposits 9%" is a share of mix, not a deposit total |
| `label_not_a_known_metric` | an unrecognised tile is decoration, not a figure |
| `ambiguous_value_more_than_one_number` | nothing says which number the label names |

---

## 4. Period association (Part 7)

A period reaches a tile by exactly two routes, in order of evidence:

1. **Inside the tile.** `(Q1-FY27)` within the tile's own bounding region
   belongs to that tile's figure. This is the only route to `stated`.
2. **The page said it once.** A banner above a row of tiles is evidence about
   the page, not about this tile. Taken as `inherited`, recording the text it
   came from.

**Proximity is never enough.** A period near a tile but outside it is not used.
That is the rule the misaligned-header defect taught, applied to geometry: a
date that arrives confidently and wrongly is worse than no date.

Measured on IDFC page 2: four tiles printed `(Q1-FY27)` inside themselves and
carry `basis = stated`; seven took the page's `Q1 FY27` banner and carry
`basis = inherited`. A reader can tell them apart.

---

## 5. IDFC results

| | Before Phase 15 | After |
|---|---|---|
| Pages | 75 | 75 |
| Financial facts | **38** | **102** |
| — from tables | 35 | 76 |
| — from KPI tiles | 0 | **13** |
| — from narrative | 3 | 13 |
| Facts with a period | 3 | 24 |
| Marked requires review | 11 | 25 |
| Detected tables | 61 | 61 |

### Facts by strategy, and whether they are right

Page 2's eleven tiles, all verified by hand against the slide:

| Metric | Value | Period | Basis |
|---|---|---|---|
| Loans & Advances | US$ 31,447 m | Q1 FY27 | inherited |
| Total Deposits | US$ 32,931 m | Q1 FY27 | inherited |
| CASA Deposits | US$ 16,734 m | Q1 FY27 | inherited |
| Profit After Tax | US$ 114 m | Q1 FY27 | **stated** |
| Core Operating Profit | US$ 250 m | Q1 FY27 | **stated** |
| Net Interest Margin | 5.96% | Q1 FY27 | **stated** |
| Cost of Funds | 5.96% | Q1 FY27 | **stated** |
| Return on Assets | 1.06% | Q1 FY27 | **stated** |
| GNPA ratio | 1.51% | Q1 FY27 | inherited |
| NNPA ratio | 0.44% | Q1 FY27 | inherited |
| CASA ratio | 50.8% | Q1 FY27 | inherited |

Two further tiles come from page 74, a since-merger comparison slide:
CASA Deposits 603 (As at 31 March 2018) and Net Interest Margin 2.1% (H1 FY19).

**The extraction count was not optimised.** An intermediate build reached 110
facts; inspecting them showed chart legends being read as balances and sentence
fragments as labels, so four guards were added and the count settled at 102.

---

## 6. Benchmarks

Re-run after every extraction change, at the final code state:

| Document | Baseline F1 | Phase 15 F1 | Facts | Provenance |
|---|---|---|---|---|
| RIL media release | 0.9882 | **0.9882** | 432 / 431 | 100% |
| RIL image-only | 0.9877 | **0.9877** | 255 | 100% |
| RIL scanner-degraded | 0.695 | **0.695** | 251 | 100% |
| Apple FY25 Q3 | no answer key | no answer key | 52 | 100% |

Apple's known-incorrect current/non-current matches remain absent.

### A regression I introduced and fixed

The first working spatial build **cost the degraded scan a match**: F1 0.695 →
0.6809, 49 → 48. Bisecting the four Phase 15 changes isolated the spatial
strategy: on a degraded *statement* scan it assembled `Revenue = 2` and
`EBITDA = 4` out of stray fragments, and those stood beside the real figures as
alternatives to them.

The fix is the routing rule: **a figure a table has already explained is not
explained again from its position on the page.** The first attempt excluded all
detected-table ground and was too blunt — IDFC's KPI grid is *also* covered by
low-quality detected tables, so it removed all thirteen good facts. The correct
rule excludes only tables that **actually produced facts**: a detected table
that yielded nothing has explained nothing, so its area stays available.

---

## 7. Other defects found and fixed

All generic; none specific to IDFC.

| Defect | Why it mattered |
|---|---|
| **No banking vocabulary in the taxonomy** | GNPA, NNPA, CASA, NIM, Cost of Funds, Advances, Deposits, CAR, Tier 1 were all missing, so a bank's results parsed as a document with almost no recognisable metrics. Fourteen metrics added. |
| **`IT Expenses` → `tax_expense` at 0.87** | Sequence similarity on short labels sharing only a generic word. A fuzzy match must now point at a substantive word both labels use. Reporting a bank's technology spend as its tax charge is not catchable downstream. |
| **`2.5X` read as an amount** | A growth multiple beside "Total Deposits" is not a deposit balance. |
| **Document currency overriding a label's own** | `Net Worth (US$ Mn.)` was reported in INR, because OCR lost the `$` and the document default was rupees. A label that declares its own scale now blocks the currency default too; unknown is the truthful answer. |

---

## 8. Validation

| | Result |
|---|---|
| Backend suite | **473 passed, 1 skipped** (10 m 13 s); was 452 |
| New spatial tests | **21** in `tests/test_spatial_extraction.py` |
| Frontend typecheck | clean |
| Production build | succeeds |
| Frontend lint | **6 warnings — unchanged from baseline, none new** |
| Benchmarks | all four at baseline (§6) |

The spatial tests build geometry at explicit coordinates, so each pins one
relationship: label above value, columns not borrowing each other's values, two
figures in one run, the lone-tile refusal, period containment versus proximity,
context inheritance, and the three routing cases.

---

## 9. Performance

The spatial strategy reads the OCR geometry the pipeline already produced. It
renders nothing and recognises nothing again.

### Cold runs, measured

| | Phase 14 | Phase 15 |
|---|---|---|
| IDFC, cold, 75 pages | **843.7 s** | **690.8 s** |
| Facts | 38 | 102 |

The run got **18% faster while producing 2.7x the facts**. The speed is not an
optimisation win and should not be read as one: it is the distinctive-word rule
in the taxonomy letting the fuzzy matcher abandon most candidate aliases
immediately, plus ordinary run-to-run variance in a loop dominated by OCR.

### Cold stage split

| Stage | Time | Share |
|---|---|---|
| Page loop (render / OCR / layout) | 524.3 s | 76% |
| Charts | 145.9 s | 21% |
| **Extraction (incl. spatial)** | **19.7 s** | **3%** |
| Everything else | < 1 s | — |

**The spatial strategy did not multiply the cost of a page.** Extraction is
19.7 s of a 690.8 s run *including* card detection on all 75 pages — against
91.7 s for extraction alone in Phase 14. It reads the OCR geometry the pipeline
already produced: nothing is rendered again and nothing is recognised again,
which was the design constraint.

### Page distribution, cold

| mean | median | p95 | max | over 45 s |
|---|---|---|---|---|
| 34.3 s | 34.1 s | 57.9 s | 65.6 s | 12 |

Twelve pages exceed the 45 s slow-page threshold. That threshold was set in
Phase 14 against a *warm* run and is too tight for a cold one, where every page
pays full OCR; the pages are evenly distributed with no stall (median 34.1 s
against a 65.6 s max), which is the shape a healthy run has. The threshold
wants revisiting rather than the pages.

---

## 10. Diagnostics

`GET /documents/{id}/diagnostics` now answers "why did this page produce
nothing", per page:

```
tables_offered                    3
narrative_regions_offered         5
kpi_elements_covered_by_tables   12
kpi_columns                       6
kpi_cards_detected               12
kpi_detection_rejected  {"figure_with_nothing_naming_it": 4,
                         "run_too_short": 3}
kpi_facts_accepted               11
kpi_extraction_rejected {"label_not_a_known_metric": 1}
facts_from_this_page             11
```

---

## 11. Remaining limitations

Honestly stated; none of this is described as industry-grade.

* **Charts produce no financial facts.** 205 chart detections on IDFC cost
  150.6 s — 36% of the run — and yield metadata only. No value is read from
  pixels, deliberately. Whether those 205 detections are even real charts has
  not been examined, and that stage is the obvious next performance target.
* **Only one spatial layout is implemented.** Vertical tiles — label above
  value — are read. The horizontal forms in the brief (`Revenue PAT EPS` across
  a row with values beneath, or a period heading a block of metrics) are *not*
  yet detected. `detect_cards` refuses them rather than guessing, so nothing is
  wrong, but nothing is gained either.
* **Table reconstruction on slide graphics is still poor.** Grids of 16×44 are
  built from infographics at 18% cell density. Phase 14's alignment guard stops
  those producing wrong dates, and the new routing rule stops them blocking the
  spatial reader, but the grids themselves are not right.
* **The taxonomy remains the ceiling.** Twelve tiles were detected on IDFC
  page 2 and eleven became facts; the twelfth (`Risk Adjusted NII`) is refused
  because no metric covers it. That is the correct behaviour and also the
  limit: extraction cannot exceed the vocabulary.
* **Two IDFC KPI facts are unverified.** The page-74 pair (CASA Deposits 603,
  NIM 2.1%) come from a since-merger comparison slide and look plausible, but I
  have not checked them against the source page.
* **`_MIN_CARDS_FOR_A_GRID = 3` is a judgement, not a measurement.** It was
  chosen because two tiles carry no repetition evidence, and it is what makes
  the lone-tile refusal work; it has not been tuned against a corpus.
* **Reconciliation across strategies is the existing one.** Candidates from
  tables, tiles and narrative go through `analysis/reconcile` unchanged. It was
  not extended for cross-strategy duplicates, and the routing rule is what
  prevents the same figure being read twice — a narrower mechanism than the
  candidate-reconciliation layer the brief describes.
