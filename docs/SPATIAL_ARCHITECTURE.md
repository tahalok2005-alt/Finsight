# Phase 15 — architecture audit and spatial extraction design

## 1. The pipeline as it stands

```
PDF
 │
 ├─ _validate            probe_pdf                     → PdfProbe
 │
 ├─ _process_pages       ThreadPoolExecutor, N workers  ── per page ──┐
 │     render_page       PageRenderer                 → RenderedPage  │
 │     _recognize        RapidOCR (cached by content) → OCRElement[]  │
 │     group_lines       ocr/lines.py                 → TextLine[]    │
 │     detect            HeuristicLayoutDetector      → Region[]      │
 │                                                                    │
 ├─ _assemble            document/assembly.py         → DocumentModel │
 │     chrome, sections, entities, calendar, currency, unit           │
 │                                                                    │
 ├─ _build_tables        tables/structure.build_table → ExtractedTable[]
 │     then tables/periods: infer_calendar_from_headers,
 │                          reresolve_column_periods, harmonise_periods
 │
 ├─ _build_charts        charts/detector.detect_charts → ExtractedChart[]
 │
 ├─ _extract             ← THE ONLY PLACE FACTS ARE BORN
 │     for each table  → extraction/facts.extract_from_table
 │     for each region → extraction/narrative.extract_narrative_facts
 │                       (PARAGRAPH, LIST, KPI_BLOCK, FOOTNOTE)
 │     then analysis/reconcile.reconcile  → dedup / conflicts
 │
 ├─ _validate_facts      validation/engine.validate
 ├─ _finalise_confidence confidence + review reason
 ├─ _analyse             entities, derived, relations, risks, insights
 │
 └─ persist              database/repository.save_*
```

### Where a fact acquires its properties

| Property | Source |
|---|---|
| metric | `extraction/taxonomy.match_metric(label)` |
| value / unit / currency | `normalization/numbers.normalize_number` |
| period | `_period_for(column)` → `NormalizedPeriod` with `basis` |
| entity | `assembly.entity_ref_for(model, region_id)` |
| confidence | `extraction/confidence.score(Signals)` |
| provenance | `Provenance(..., extraction_method=...)` |
| review reason | `confidence.review_reason_for` |

**All of these are strategy-independent.** Nothing about them assumes a table.
That is what makes a second extraction strategy cheap.

---

## 2. What is already reusable

| Component | Reused as-is |
|---|---|
| `OCRElement` with `bbox`, `font_size_px`, `confidence` | the spatial primitive — no new OCR needed |
| `core/geometry.BBox` (`h_overlap_ratio`, `union_all`, `cx`, `cy`) | all geometric reasoning |
| `taxonomy.match_metric` | label → canonical metric |
| `numbers.normalize_number` | text → value, unit, currency, value type |
| `periods.parse_period` / `parse_period_levels` | text → `NormalizedPeriod` + basis |
| `PeriodBasis` (stated / derived / inherited / unknown / not_applicable) | period provenance |
| `confidence.score` / `Signals` | the same confidence scale |
| `Provenance` | the same evidence record |
| `analysis/reconcile.reconcile` | candidate deduplication |
| `FactKind.KPI` | **already defined and unused** |
| `RegionType.KPI_BLOCK` | already defined; detector too narrow |

## 3. What is missing

1. **No spatial extractor.** `KPI_BLOCK` regions are routed to
   `extract_narrative_facts`, a sentence-based extractor. A card is not a
   sentence, so it yields nothing.
2. **The KPI detector is far too narrow.** It requires one line, big font,
   ≤4 elements, and an overlapping smaller-font neighbour. On a 75-page
   investor deck it fired **once**.
3. **No page/region content classification.** Every page runs every strategy.
4. **No extraction-strategy provenance.** `extraction_method` exists on
   `Provenance` but only ever says `table_cell` or `narrative`.
5. **No per-page extraction diagnostics.** Timing exists; "why did this page
   produce nothing" does not.

---

## 4. The evidence this design is built on

Page 2 of the IDFC deck, from the OCR geometry already stored:

```
 column 0 (cx~330)      column 1 (cx~1020)     column 2 (cx~1710)     column 3 (cx~2400)
 ─────────────────      ──────────────────     ──────────────────     ──────────────────
 Loans & Advances  291  Total Deposits    290  CASA Deposits     290  CASA ratio     286
 US$ 31,447 Mn.    410  US$ 32,931 Mn.    407  US$ 16,734 Mn.    407  50.8%          407
 (20% YoY|5% QoQ)  465  (18% YoY|6% QoQ)  460  (25% YoY|8% QoQ)  459  (283 bps YoY)  459

 GNPA ratio        643  NNPA ratio        643  Net Interest Marg 621  Risk Adjusted  621
                                              (Q1-FY27)         666  (Q1-FY27)      659
 1.51%             772  0.44%             773  5.96%             772  4.75%          765
 (-45 bps YoY)     829  (-11 bps YoY)     827  (25bps YoY)       827  (96bps YoY)    824

 Cost of Funds     988  Core Operating P  991  Profit After Tax  991  Return on Ass  986
 (Q1-FY27)        1024  (Q1-FY27)        1028  (Q1-FY27)        1028  (Q1-FY27)     1019
 5.96%            1128  US$ 250 Mn.      1132  Us$ 114 Mn.      1132  1.06%         1128
```

Three facts about this that the design turns into evidence:

* **Cards align in columns.** Every element of a card shares an x-centre with
  the rest of its card, to within a fraction of the card's width.
* **A card is a vertical run.** Label, then optionally a period, then the
  value, then optionally a change — separated by ordinary line gaps, with a
  much larger gap between bands.
* **The layout repeats.** Four columns with the same internal shape at the
  same y-bands. The repetition is itself the evidence that the reading is
  right; a single isolated label-above-number is far weaker.

Currently page 2 produces **zero facts**, while containing Profit After Tax,
NIM, GNPA, CASA ratio and Return on Assets for Q1 FY27 — the headline figures
of the document.

---

## 5. Minimal new architecture

```
        _build_tables ─────────────────┐
                                       │
        _build_charts ─────────────────┤
                                       │
   NEW  _classify_regions              │   spatial/classify.py
          page → LayoutFinding[]       │   (cheap: reuses OCR geometry)
                                       │
                                       ▼
        _extract  ┌────────────────────────────────────────┐
                  │ tables      → extract_from_table       │  existing
                  │ KPI grids   → extract_from_cards       │  NEW
                  │ regions     → extract_narrative_facts  │  existing
                  └───────────────────┬────────────────────┘
                                      │  all produce FinancialFact
                                      ▼
                            reconcile (existing)
                                      ▼
                      validation → confidence → persist → UI
```

Three new modules, no changes to how a fact is validated, scored, stored or
displayed:

| Module | Responsibility |
|---|---|
| `spatial/geometry.py` | column clustering, vertical runs, gap statistics |
| `spatial/cards.py` | `detect_cards(elements, page) -> CardCandidate[]` with evidence |
| `spatial/extract.py` | `extract_from_cards(cards, ctx) -> FinancialFact[]` |

### Integration point

One new loop inside `_extract`, beside the existing two. Same
`TableFactContext`-equivalent, same taxonomy, same number normalisation, same
period resolver, same confidence scale, same `Provenance`.

### Period rule for cards (Part 7)

A period may only be attached to a card's value when the period text is
**inside that card's own bounding region**. Anything else — a page banner, a
neighbouring card — is `PeriodBasis.INHERITED` and records where it came from,
or stays unknown. Geometric proximity alone never produces a `stated` period.

### What is deliberately *not* built in this phase

* Chart value extraction from pixels. Charts contribute metadata only.
* A learned layout model. The deterministic signals above are sufficient and
  explainable, and the interfaces leave room for a model later.

---

## 6. What Phase 16 added

Superseded in part by `docs/VISUAL_STRUCTURE_ARCHITECTURE.md`, which audits the
same pipeline again after this design was built and measures what it actually
did on a real document. The additions:

| Module | Role |
|---|---|
| `spatial/fragments.py` | the four questions both readers ask of a piece of text: is it a figure, a label, a period, a movement note |
| `spatial/rows.py` | horizontal reading: a label with its figure beside it, repeated down a panel |
| `spatial/geometry.py` | `lattice_of` / `Lattice`: whether a set of boxes fills the grid its positions imply |
| `layout/furniture.py` | which recurring graphic regions belong to the page template rather than to the document |

`spatial/extract.py` gained `read_label_and_value`, which both readers share so
they cannot acquire separate ideas of what a metric, a scale or a percentage
is, and `extract_from_rows`, which differs only in where a period may come
from and in how much confidence the weaker geometry earns.

The period rule above is unchanged for tiles and extended for rows: a period
may be `stated` from the row's own label or its continuation line, `inherited`
from a heading that **spatially contains** the block, or `inherited` from a
page heading. A heading beside the block, or a sentence anywhere on the page,
is never a source.
