# Phase 16 — visual structure understanding

Everything numbered here was measured on this machine. Where a measurement is
noisy the spread is given rather than the best of it, and where something does
not work it is in §12 rather than absent.

The document under test throughout is `IDFC_Q12027.pdf` — 75 pages, an
investor presentation, reported in US dollars.

---

## 1. Architecture changes

The phase began with an audit, `docs/VISUAL_STRUCTURE_ARCHITECTURE.md`, written
against the running Phase 15 system before any code changed. It is the reason
several things below are *narrowings* rather than additions: the audit found
two of the thirteen Phase 15 KPI facts to be wrong, and one of them wrong in a
way no unit test would have caught.

New modules:

| Module | What it is |
|---|---|
| `app/spatial/fragments.py` | The four questions both spatial readers ask of a piece of recognised text — is this a figure, the name of a measure, a period, a movement note. Lifted out of `cards.py` so the two readers cannot drift apart. |
| `app/spatial/rows.py` | The horizontal reader: a label with its figure beside it, repeated down a panel. |
| `app/layout/furniture.py` | Which recurring graphic regions belong to the page template rather than to the document. |

Changed:

| Module | Change |
|---|---|
| `spatial/geometry.py` | `lattice_of` / `Lattice` — whether a set of boxes fills the grid their positions imply. |
| `spatial/cards.py` | The grid-completeness gate; `_headings_over`, which decides what may head a page. |
| `spatial/extract.py` | `read_label_and_value`, shared verbatim by both readers; `extract_from_rows`. |
| `services/pipeline.py` | `_extract_cards` now returns the ground it claimed; `_extract_rows` added after it; `_mark_page_furniture` before the chart stage. |
| `charts/detector.py`, `layout/cv_signals.py` | `distinct_quantised_colours` replaces an `np.unique(axis=0)` over millions of RGB triples. |
| `extraction/taxonomy.py` | Five metrics added: book value per share, shares outstanding, pre-provision operating profit, risk-adjusted NII, and the PPOP/EBIT separation. |
| `extraction/facts.py`, `spatial/extract.py` | Per-share figures no longer inherit a document- or table-level magnitude. |
| `normalization/numbers.py`, `extraction/narrative.py` | Word boundaries on the unit-declaration patterns. See §11. |
| `api/routes_documents.py` | Region counts by type, facts by strategy, and the page-furniture decisions on `/diagnostics`. |
| `frontend/src/utils/period.ts` | `spatial_row` in the strategy vocabulary; `basisPhrase` no longer claims a table. |

The routing order is now explicit and is the only place precedence lives:

```
table extraction that produced facts   ── claims its area
        │
        ▼
vertical KPI tiles                     ── claim every tile they were offered
        │
        ▼
horizontal metric rows                 ── see what is left
        │
        ▼
narrative                              ── sentences the other three refuse
```

---

## 2. Visual region types supported

| Structure | Reader | Status on real pages |
|---|---|---|
| Reconstructed table | `extraction/facts.py` | 80 of 104 IDFC facts |
| Vertical KPI grid | `spatial/cards.py` | 12 facts, all of page 2's tiles |
| Horizontal metric block | `spatial/rows.py` | 3 facts, page 62 |
| Period heading over a block | `spatial/extract.py` | working; nothing in IDFC used it (see §12) |
| Narrative sentence | `extraction/narrative.py` | 9 facts |
| Chart / figure | `charts/detector.py` | detected and stored; **produces no facts by design** |
| Page furniture | `layout/furniture.py` | 123 regions identified and set aside |

---

## 3. Horizontal layouts

**Supported.** A block of rows where each row is one label, a gutter, and one
figure, and where the labels share a left edge and the figures share theirs.
Three or more such rows are required.

**Refused, with the reason recorded.** Each of these is a real page:

| Page | Shape | Refusal |
|---|---|---|
| 64 | `CRISIL │ AAA`, five perfect repetitions | no financial value in any row |
| 7 | `Loan Book │ 2,850 │ 5,809 │ 8,954 │ 31,447` | `several_figures_on_the_baseline_no_column_heads` |
| 8 | `Retail │ Grew 6.7x │ from US$ 2,838 Mn. │ to US$ 18,920 Mn.` | same |
| 27 | `strong deposit franchise │ 32,243` | `label_not_a_known_metric` |
| 21, 35, 36 | `%SO'T │ %96'0` — rotated axis text | `label_not_a_known_metric` |

Page 64 is the reason this reader leans on the taxonomy rather than on
geometry: it has the strongest horizontal geometry in the document and contains
nothing financial at all.

Refusal counts across all 75 pages:

| Reason | Count |
|---|---:|
| `nothing_on_the_row_naming_a_figure` | 258 |
| `several_figures_on_the_baseline_no_column_heads` | 107 |
| `figure_too_far_from_the_label_to_belong_to_it` | 19 |
| `block_not_repeated_enough_to_trust` | 19 |

---

## 4. Chart-detection audit

Full working in `docs/VISUAL_STRUCTURE_ARCHITECTURE.md` §5. The headline:

**123 of the 205 detections (60 %) were page furniture** — the same box, at
pixel-identical geometry, on five or more pages. Verified by cropping and
looking at them:

| Pages | Size | Classified as | What it actually is |
|---:|---|---|---|
| 57 | 854×62 | `unknown` | the IDFC FIRST Bank logo |
| 48 | 2667×166 | **`pie`, confidence 0.9** | the footer strip |
| 12 | 2667×56 | `unknown` | the rule under the page title |
| 6 | 547×1500 | **`pie`** | a solid red side panel |
| 4 | 1757×164 | `pie` | a header band |

Of the 82 that remain, a sampled inspection found genuine charts (p10, p14,
p33, p35, p51, p63), charts fragmented into four or five blobs (p36, p37), KPI
tiles misread as charts (p2 — `Risk Adjusted NII (Q1-FY27) / 4.75 %`), rating
badges, and award photographs.

**Estimated precision for "a genuine, whole, distinct financial chart": about
12 %** (roughly 25 of 205, after merging fragments). Recall is decent; the real
charts are found, but arrive fragmented and mislabelled. `pie` was returned 92
times out of 205, including for a line chart and a column chart.

**Where the time went.** Replaying the stage over the cached renders:

| | |
|---|---:|
| loading 73 page images | 2.6 s |
| `analyse_region` × 205 | **122.0 s** |

and inside `analyse_region`, over a 30-region sample:

| Operation | Share |
|---|---:|
| **`np.unique(q, axis=0)` — distinct colour count** | **85.6 %** |
| `cv2.HoughCircles` | 12.3 % |
| everything else | 2.1 % |

Six-sevenths of chart-processing time was a lexicographic sort of several
million RGB triples, answering a question with only 64 possible answers.
`np.bincount` over a packed 6-bit index returns **the identical number** —
verified bit-for-bit on synthetic arrays and on real pages — 42–94× faster.

**Decision taken:** fix the cost, suppress the furniture, keep the feature.
Chart detection was not disabled and no signal was removed. Chart value
extraction was not attempted; the audit's page-74 finding is what happens when
that is done casually.

Furniture is identified on two tests together, and both are needed: the box
must **recur** on three or more pages, and it must **hold no data** — measured
as the median recognised-element count across its instances, with each instance
also checked individually so the one page where a chart overlaps the footer
band keeps its content. Pages 35–37 run the same 2372×857 bar chart three times
with different data in it; it is correctly kept.

---

## 5. IDFC before and after

| | Phase 15 | Phase 16 |
|---|---:|---:|
| Facts | 102 | **104** |
| — statement table | 76 | **80** |
| — KPI tile | 13 | **12** |
| — horizontal row | 0 | **3** |
| — narrative | 13 | **9** |
| Facts with a period | 26 | 23 |
| Requiring review | 23 | 32 |
| Chart regions analysed | 205 | **82** |
| Pages producing nothing | 57 | 60 |

Three of the counts moved *down*, and each is an improvement rather than a
loss: one KPI fact and four narrative facts were removed because they were
wrong, and the pages they came from now produce nothing. §11 has the detail.

The KPI count **fell by one and that is the most important line in the table.**
Phase 15's thirteen included two facts assembled from a six-panel chart page:

* `net_interest_margin = 2.1 %, H1 FY19` — the chart's FY17 bar, with a period
  lifted out of a page footnote about a one-time provision;
* `casa_deposits = 603, As at 2018-03-31` — a chart title, an axis tick from
  the chart *above* it, and a bar label from the chart *below*, stitched into
  one "tile". Right by coincidence of column position.

Both are gone. In their place page 2's `Risk Adjusted NII` is now read (the
taxonomy had no entry for it), giving 12.

The twelve tiles of page 2, checked against the rendered page one by one:

| Metric | Value | Period | Basis |
|---|---|---|---|
| Loans & Advances | US$ 31,447 Mn. | Q1 FY27 | inherited from the page heading |
| Total Deposits | US$ 32,931 Mn. | Q1 FY27 | inherited |
| CASA Deposits | US$ 16,734 Mn. | Q1 FY27 | inherited |
| CASA ratio | 50.8 % | Q1 FY27 | inherited |
| GNPA ratio | 1.51 % | Q1 FY27 | inherited |
| NNPA ratio | 0.44 % | Q1 FY27 | inherited |
| Net Interest Margin | 5.96 % | Q1 FY27 | stated in the tile |
| Risk Adjusted NII | 4.75 % | Q1 FY27 | stated |
| Cost of Funds | 5.96 % | Q1 FY27 | stated |
| Core Operating Profit | US$ 250 Mn. | Q1 FY27 | stated |
| Profit After Tax | US$ 114 Mn. | Q1 FY27 | stated |
| Return on Assets | 1.06 % | Q1 FY27 | stated |

All twelve correct. Cost of Funds and NIM both reading 5.96 % is not an error —
the document prints both.

Page 62's three rows, likewise checked:

| Metric | Value | Period |
|---|---|---|
| Total No. of shares | 861.47 Cr | none — refused, see below |
| Book Value per Share | Rs. 56.47 | As at 30 June 2026, stated in the label |
| Basic EPS | US$ 0.05 | Q1 FY27, stated in the label |

---

## 6. Facts by strategy

| Strategy | Facts | Confidence range |
|---|---:|---|
| `table_cell` | 80 | up to 0.89 |
| `spatial_kpi` | 12 | 0.85 – 0.91 |
| `narrative_sentence` | 9 | 0.64 – 0.82 |
| `spatial_row` | 3 | 0.75 – 0.76 |

A row is deliberately trusted less than a tile. A tile is enclosed by
whitespace on four sides; a row is separated along one axis only, which is also
the shape of a table row, a chart legend and a credit rating grid.

Tile refusals across the document, now that the grid gate exists:

| Reason | Count |
|---|---:|
| `figure_with_nothing_naming_it` | 562 |
| `run_too_short` | 276 |
| `tile_not_repeated_across_the_layout` | 204 |
| `no_label_in_tile` | 86 |
| **`tiles_do_not_form_a_grid`** | **53** |
| `no_repeated_layout` | 26 |
| `label_not_a_known_metric` | 3 |

`label_not_a_known_metric` fell from 51 to 3: the grid gate refuses most of
those candidates on geometry before the taxonomy ever sees them.

---

## 7. Processing time

Clean cold runs, nothing else running, OCR cache cleared each time.

| Stage | Phase 15 | Phase 16 (shipped code) |
|---|---:|---:|
| rendering + OCR + layout (one parallel loop) | 524.3 s | **288.7 s** |
| charts | **145.9 s** | **15.2 s** |
| extraction | 19.7 s | 22.0 s |
| tables | 0.2 s | 0.2 s |
| everything else | < 1 s | < 1 s |
| **wall clock** | **690.8 s** | **326.4 s** |

The final figure is one clean cold run of the shipped code. Seven clean cold
runs were taken across the phase — 323.3, 338.3, 289.4, 370.2, 378.7, 293.9 and
326.4 s — so the spread on this machine is ±14 % around a mean of 331 s.
**The Phase 15 figure of 690.8 s was a single measurement with no spread beside
it**, so the honest statement is that the run is roughly half as long. Claiming
2.4× from the best pairing would be picking numbers.

Two changes account for it and only one is an optimisation:

* the colour count, which is 85.6 % of chart time and also runs per graphic
  blob inside the **layout** stage — which is why rendering/OCR/layout fell as
  well as charts;
* furniture suppression, which removed 123 of 205 regions from chart analysis.

Extraction grew by ~4 s: that is the horizontal reader, running on geometry the
pipeline already had. Nothing is rendered or recognised twice.

Per-page distribution:

| | Phase 15 | Phase 16 |
|---|---:|---:|
| mean | 34.3 s | **15.2 s** |
| median | 34.1 s | **15.6 s** |
| p95 | 57.9 s | **26.8 s** |
| max | 65.6 s | **32.2 s** |
| pages over the 45 s threshold | 12 | **0** |

These are from the same run as the wall clock above; on the six other cold runs
the mean ranged 15.2–21.7 s and at most one page crossed the threshold.

The 45 s threshold was set in Phase 14 against a warm run and was too tight for
a cold one — it flagged 12 of 75 pages. It now flags none on this run and one
on the slower runs, which is what a slow-page threshold is for. It has been
left where it is rather than retuned, because a threshold that fires on nothing
is not evidence that it is set correctly.

---

## 8. Benchmarks

Every benchmark re-run after every change that could touch extraction, against
the Phase 15 baselines:

| Benchmark | Baseline F1 | Phase 16 F1 | Matched | Verdict |
|---|---:|---:|---:|---|
| RIL media release | 0.9882 | 0.9882 | 84 / 84 | unchanged |
| RIL image-only | 0.9877 | 0.9877 | 80 / 80 | unchanged |
| RIL degraded scan | 0.695 | 0.695 | 49 / 49 | unchanged |
| Apple statements | 52 facts | 52 facts | — | unchanged |

No regressions. RIL's raw fact count moved from 432 to 431 while F1 and the
matched count stayed identical: the fact that disappeared was one the truth key
never matched — a figure the narrative extractor had fabricated from prose (see
§11). Removing a wrong answer that was never scored is a precision gain the F1
cannot show.

The benchmark suite was run four separate times during the phase — after the
routing changes, after the per-share and heading fixes, after the currency and
narrative fixes, and after the count-currency fix — because each of those could
have moved a benchmark and three of them touched the table path.

---

## 9. Test suite

| | Phase 15 | Phase 16 |
|---|---:|---:|
| Backend | 473 passed, 1 skipped | **526 passed, 1 skipped** |
| Frontend typecheck | clean | clean |
| Production build | succeeds | succeeds |
| Lint | 6 warnings | 6 warnings, none new |

53 tests added, in `tests/test_visual_structure.py` plus additions to
`test_extraction.py`. Every refusal they assert is a defect that actually
occurred on a real page; the page numbers are in the docstrings.

---

## 10. Regressions encountered

| # | What broke | How it surfaced | Fix |
|---|---|---|---|
| 1 | `test_two_numbers_in_one_tile_become_two_tiles` failed under the grid gate | unit test | the fixture's tiles overlapped each other; rebuilt as a real 2×2 grid |
| 2 | `Core Operating Profit` stopped mapping to `ebit` | two unit tests | intended — for a bank, provisions are an operating cost, so PPOP and EBIT measure different things. Tests updated, `distinct_from` set |
| 3 | The row reader lost two real facts on page 62 | real-document run | it removed table-claimed text *before* measuring the block, so a table reading one row of three destroyed the quorum for the other two. Routing moved after structure measurement |
| 4 | Page 2's twelve tiles lost their period | real-document run | the footnote fix was right, but the page's actual heading was excluded by a 30 %-width gate meant for a different purpose. `_headings_over` added |
| 5 | `shares_outstanding` took its period from a corner date stamp | browser inspection | the row path had no heading discipline at all. The page-level fallback was removed; only a heading that spans the block may lend it a period |
| 6 | Per-share figures took the document's `million` scale | browser inspection | `Rs. 56.47` became 56.47 million rupees. Fixed in both the spatial and the table paths |

None shipped. Every one was found by a test or by looking at the real document.

---

## 11. Pre-existing defects found and fixed

These were not introduced by this phase. All of them were found by inspecting
the running system rather than by any test, and each was doing real damage.

**A word boundary, missing in two places.** The pattern `rs\.?` with no leading
`\b` matches the letters "rs" inside "custome**rs**", "numbe**rs**" and
"quarte**rs**".

* In `infer_document_units` this cast **80 phantom INR votes against 38 genuine
  USD ones**, so a deck reporting entirely in US dollars had
  `document_currency = INR`. Every figure that fell back to the document's
  currency was then wrong by an exchange rate — and `currency_inferred` was
  false, so it was asserted rather than flagged.
* In the narrative extractor the same fragment licensed a bare number as a
  financial figure. The phrase *"Product-wise GNPA trend analysis (5
  quarters)"* was recorded as **`gross_npa_ratio = 5.0 %`, medium confidence,
  no review flag**. The document's GNPA is 1.51 %. The same happened for NNPA
  on the next page.

Fixing the first also removed a spurious duplicate: page 2's `Loans & Advances`
now reconciles with the KPI tile as corroboration instead of standing beside it
as a second, rupee-denominated row.

**Two control characters written into source by an earlier phase.**

* `spatial/cards.py` held literal backspace bytes where `\b` was intended, so
  `_LABEL_WITH_ITS_OWN_FIGURE` had never matched anything but `\d\s*%` — the
  guard against a label carrying its own figure was inert.
* `frontend/src/utils/period.ts` held a NUL byte as a sentinel. Replaced with
  an explicit null check.

A scan of every `.py`, `.ts` and `.tsx` file in the repository now finds no
control bytes.

**A share count rendered as a rupee amount.** `861.47 Cr` against "Total No.
of shares" was given `currency = INR` by the rule that crore and lakh imply
rupees. That rule is right for money and exists because OCR cannot emit the
rupee glyph, so the word is the only evidence available -- but a count of
things has no currency at any scale. `normalize_number` now clears the
currency for `ValueType.COUNT`.

**A UI claim that was simply false.** `basisPhrase` rendered every inherited
period as *"inherited from the table structure"*, including on KPI tiles, which
have no table. It now names the actual evidence: *"inherited from the page
heading 'Bank at Glance, Q1 FY27 results'"*.

---

## 12. What still does not work

**Charts produce no facts.** 82 regions and ~15 s per run buy chart
*metadata* and nothing else. The remaining unread information in this document
is overwhelmingly in charts: pages 17, 25, 32, 33, 50 and about twenty more
plot a labelled time series with the values printed on the bars and the periods
on the axis. Reading those is a phase of its own, and the page-74 defect is a
preview of what doing it carelessly costs.

**Chart type classification is unreliable and was not fixed.** `pie` was
returned 92 times in 205, including for the footer strip 48 times, a share
price line chart and a bar chart. `HoughCircles` fires on rounded corners.
Nothing downstream uses `chart_type`, so this is stored misinformation rather
than acted-upon misinformation — but it is stored, and a reader of the Charts
tab sees it.

**Chart regions are over-segmented.** One bar chart on page 37 arrives as five
separate blobs. The furniture rule does not help here; these are content.

**The table path does not apply a rule the spatial path does.** On page 14 a
table cell gives `casa_deposits = 50.80 %` — a percentage recorded against a
monetary metric. `spatial/extract.py` refuses exactly this
(`percentage_value_for_a_monetary_metric`); `extraction/facts.py` does not. It
is the same rule and the table has the information to apply it. It was left
alone because changing table extraction this late in the phase could not have
been validated properly, and shipping an unvalidated change to the strategy
that produces 80 of 104 facts is worse than shipping a documented gap.

**Spatial facts take their entity from the first region on the page.** Page
62's three rows are attributed to `RAM` -- the deck's Retail/Agri/MSME segment
-- because `_extract_rows` and `_extract_cards` both resolve their entity from
`art.regions[0]`, whatever that region happens to be. The shareholding panel
belongs to the bank as a whole. The values, units, periods and provenance are
right; the entity is not. Fixing it properly means attributing a spatial fact
to the entity of the region that *contains* it, which changes the reconciliation
scope key for every spatial fact and so needs its own benchmark cycle. It was
inherited from Phase 15 and is not made worse here, but it is wrong.

**Narrative extraction is the weakest strategy by a wide margin.** The
fabrication fix removed four of its thirteen facts on this document, including
`advances = US$ -26 million`. What remains still includes `total_deposits` five
times with five different values from five different pages, two facts whose
value could not be parsed at all (correctly flagged `value_unparsed` and shown
as an em dash rather than a number), and one flagged `conflicting_sources`
against the KPI tile. Its facts are correctly the lowest-confidence ones and
most are correctly flagged, but the extractor is still finding metric names in
prose that does not state a metric.

**The horizontal reader found exactly one block in a 75-page document.** That
is the correct answer for this document — the audit's probe found ten pages
with three or more label-and-figure pairs, and nine of those are tables,
legends or prose. But one block is thin evidence that the reader generalises,
and it has not been tried on a document built out of such panels.

**Period-heading blocks are implemented and unexercised.** `heading_over_block`
requires a heading above the block spanning at least 60 % of its width. Nothing
in IDFC satisfies it; page 62's `Shareholding (June 30, 2026)` heads the pie
chart on the other side of the page, and is correctly not applied. Covered by
tests, not by a real page.

**Transposed tables are still unread.** Page 7 (`Progress since merger`) is a
genuine table — eleven row labels against five column heads, three of them
dated — drawn with rounded rectangles instead of ruling lines. Table detection
finds one band and reads nothing; the row reader correctly refuses it rather
than reporting Capital First's 2018 loan book as the bank's own. It remains
unread, and it is the largest single block of unread tabular data in the
document.

**60 of 75 pages produce nothing**, up from 57. Four of those pages previously
produced a fabricated figure and now correctly produce none; page 62 newly
produces three. Most of the rest are chart pages, photograph collages or
narrative slides.

**The cold-run timing spread is wide.** ±13 % across five runs on an otherwise
idle machine. The improvement is large enough to survive that, but a 5 % change
measured this way would not mean anything.

**`kpi_block` as a region type remains dead.** It fired once in 75 pages. The
spatial readers work on OCR elements and ignore it entirely. It should either
be rebuilt against what the spatial layer learned or removed; it was left in
place because removing a region type touches persistence.
