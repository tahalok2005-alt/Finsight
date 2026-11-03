# FinSight — Milestone 2 Engineering Report
## Financial Intelligence & Semantic Analysis

**Date:** 31 August 2026
**Scope:** Semantic layer built on top of the Milestone 1 / 1.5 visual-first extraction pipeline.
**Constraints honoured:** no paid API, no LLM at runtime, visual-only mode intact, native-text boundary unweakened, no company-specific extraction logic.

---

## 1. What was built

Milestone 1 answered *what does this page say*. Milestone 2 answers *what does it mean*.

Fourteen new modules sit **on top of** the existing pipeline. Nothing in the extraction path was redesigned; the analysis stage runs after validation and consumes facts that already exist.

| Module | Purpose | Lines |
|---|---|---|
| `app/document/entities.py` | Hierarchical entity registry (who reported this) | 307 |
| `app/tables/semantics.py` | Table type, column roles, period relationships | 252 |
| `app/tables/periods.py` | Document-level column-period repair | 190 |
| `app/analysis/derived.py` | Ratio computation with formula + inputs | 452 |
| `app/analysis/relations.py` | Typed fact relationship graph | 206 |
| `app/analysis/reconcile.py` | Duplicate classification, conflict retention | 264 |
| `app/analysis/risks.py` | Cautionary statement extraction | 200 |
| `app/analysis/insights.py` | Deterministic, source-linked observations | 296 |
| `app/analysis/anomalies.py` | Explainable anomaly detection | 359 |
| `app/analysis/explain.py` | Per-fact "why do you believe this" | 337 |
| `app/database/models_intelligence.py` | 7 new tables | 199 |
| `app/database/migrations.py` | Additive, non-destructive migration | 105 |
| `app/api/routes_intelligence.py` | 10 new endpoints | 414 |
| `tests/test_intelligence*.py` | 60 new tests | 858 |

**≈4,400 new lines.** Plus extensions to `taxonomy.py`, `facts.py`, `narrative.py`, `assembly.py`, `pipeline.py`, `repository.py`, `validation/engine.py`, `export/exporters.py`, and the React frontend.

---

## 2. Structured financial taxonomy (§2, §3)

**78 metrics**, each placed in a statement and a category:

| Statement | Metrics | | Category | Metrics |
|---|---|---|---|---|
| income_statement | 28 | | operational | 14 |
| balance_sheet | 14 | | earnings | 9 |
| operational | 14 | | costs | 9 |
| ratio | 12 | | revenue | 8 |
| segment | 5 | | balance_sheet | 8 |
| cash_flow | 5 | | leverage | 6 |

Operational KPIs (subscribers, ARPU, stores, throughput, footfalls, occupancy) are **not** forced into a financial statement — they carry `statement = operational` and `is_operational = True`.

Original terminology is preserved on every fact: `metric` holds the label exactly as printed, `canonical_metric` the taxonomy key, `metric_display` the canonical display name. `PAT` and `PAT including share of associates and JVs` remain separate metrics, linked by a `distinct_from` declaration that the reconciler and the explanation service both consult.

---

## 3. Entity model (§4)

Entities are inferred from the document's own headings and page furniture — no company name is hardcoded anywhere. On the RIL filing the pipeline discovers:

```
Reliance Industries Limited                [company]
├─ Standalone                              [standalone]   ← from the page header of pages 28+
└─ Consolidated                            [consolidated]
   ├─ JPL                                  [segment]
   ├─ RRVL                                 [segment]
   ├─ O2C                                  [segment]
   ├─ OIL AND GAS (EXPLORATION AND PRODUCTION)  [segment]
   └─ JIOSTAR                              [segment]
```

Every fact carries `entity_id`, `entity_type` and `parent_entity_id`. **100% of extracted facts are attributed to an entity.**

Three defects found and fixed here:

1. **A narrative sentence naming two units attributed to the wrong one.** "Consolidated Revenue at ₹340,257 crore … JPL EBITDA up 23.9%" arrives as one block of bullets. Ranking by alias length gave the group's revenue to JPL. `find_in_text` now ranks candidates by **proximity to the metric**, so each figure goes to the unit named nearest it.

2. **Standalone results were merged into Consolidated.** The "UNAUDITED STANDALONE FINANCIAL RESULTS" banner repeats on every page of that statement block, so chrome detection classified it as a running header and section building never saw it. Consequence: page 21's consolidated revenue (₹340,257 cr) and page 28's standalone revenue (₹173,151 cr) looked like a contradiction. Page chrome is now scanned for a declared reporting basis, and `register_basis()` creates the standalone entity.

3. **A fabricated company name in visual-only mode.** OCR split the letterhead and produced "Media Release Industries Limited". Candidate filer names containing document-type words (media/press release, presentation, annual report…) are now rejected — a generic rule, naming no company.

---

## 4. Period model (§5)

The fiscal calendar is inferred per document; March is never assumed. The same date resolves differently under different year-ends — `Quarter ended 31 Mar 2026` is `Q4 FY26` with a March year-end and `Q1 FY26` with a December one.

**A significant defect was found here.** Statement columns are headed by bare dates (`30th Jun'26`); what they *mean* comes from a group banner ("Quarter Ended" / "Year Ended") that is routinely lost:

* a statement continuing onto the next page repeats the date row without the banner — every column degraded to a balance-sheet date (`As at 2026-06-30`);
* one banner covers three columns and another covers the fourth, but the banner cell binds to a single column — so the annual column inherited "quarter" and **collided with the actual quarter**.

The consequence was severe: 128 facts on the four statement pages carried point-in-time periods, two different columns resolved to the *same* period, and the reconciler duly reported 22 "contradictions" that were nothing of the sort.

`app/tables/periods.py` repairs this at document level with three evidence-based rules — harmonisation (a header the document resolved elsewhere means the same here), collision repair (two value columns cannot be the same period), and sibling-span inheritance (collision-avoiding). On the RIL filing it makes **9 repairs**.

A second bug was hiding behind it: `_period_for()` in fact extraction re-parsed the *raw header* rather than the column's resolved label, discarding every repair. Fixed.

Result: period attribution rose from 93.1% to **94.0%**, and contradictory-duplicate anomalies fell from 22 to 3.

---

## 5. Units and currency (§7)

Unit resolution is strictly layered — row-level UoM cell → the table's own declaration → never loose narrative prose. Currency now follows the same discipline plus two additional rules, both prompted by real failures:

1. **A source naming two currencies decides nothing.** Segment captions print the rupee figure with its dollar equivalent — "Record Quarterly Revenue at ₹201,803 crore ($ 21.3 billion)" — and the rupee glyph is routinely lost in extraction, leaving the `$` as the only symbol. That stamped two rupee segment tables as USD.

2. **An India-only magnitude outranks a borrowed symbol.** "Crore" and "lakh" are used only for rupees. A crore figure whose currency was picked up from surrounding text rather than from the number itself is INR, flagged `currency_inferred`.

Currencies are never converted silently: a derived metric whose monetary inputs disagree on currency is not computed at all.

---

## 6. Derived metrics (§11)

**12 recipes.** Each result carries its formula, the fact ids of its inputs, and — where the document published the same measure — whether the two agree.

Derived metrics live in their own table (`derived_metrics`), never in `financial_facts`. A calculated number can therefore never be mistaken for one management reported, in the API, the UI or the export.

Four correctness rules, each pinned by a test:

* **A flow ratio is not computed against a balance-sheet date.** "Revenue as at 30 June" is not a quarter of revenue; dividing by it invents a rate.
* **A stock over a quarterly flow is annualised.** Net Debt / EBITDA computed on one quarter came out 4× too high (2.27 vs the document's 0.57 — exactly 4×). The denominator is now annualised and the formula says so.
* **A monetary result is compared on the reported magnitude.** Results are computed on normalised base values so crore and million reconcile; comparing that base figure against a printed crore figure reported a discrepancy of ten million to one.
* **The denominator the document used is identified.** RIL computes margins on *gross revenue* ("Value of Sales & Services"), not revenue from operations — a constant 9.1% difference across every period. Rather than flag eight false anomalies, the engine tries the other revenue figures the document printed, finds the one that reconciles, adopts it, and records in the metric's notes *why*.

On the RIL filing: **47 derived metrics, 33 with a published counterpart, 33 agreeing, 0 disagreeing.**

---

## 7. Duplicates and conflicts (§17, §18)

Nothing is deleted. Every near-collision is classified into one of eight outcomes, and only `identical_repeat` collapses — recording the pages its copies were found on, which raises confidence. A `genuine_conflict` keeps **both** facts, flags both for review, links them with a `conflicts_with` relation, and states "unresolved — both retained for human review".

Two rules stop the system inventing contradictions:

* **A fact with no period is its own scope.** Several columns of one table routinely fail period resolution together; bucketing them declared this quarter's EBITDA to contradict last quarter's.
* **The printed label is part of the identity.** "Total Throughput" and "Production meant for sale" both match the taxonomy's `throughput`, but the document printed them as two line items with different numbers. That is a taxonomy limitation, not a contradiction — classified `different_definition`.

---

## 8. Validation (§20)

Accounting identities expanded from 2 to 11, spanning income statement (total income composition, income − expenses = PBT, EBITDA − depreciation = EBIT, EBIT − finance costs = PBT, PBT − tax = PAT), balance sheet (liabilities + equity = assets, debt − cash = net debt), and cash flow (operating − capex = FCF, current assets − current liabilities = working capital).

`NOT_APPLICABLE` was added to `ValidationStatus`: it means the rule was considered and deliberately not run because the document does not publish every figure it needs — distinct from `NOT_CHECKED`, which means no rule looked.

Facts with no period remain excluded from cross-fact bucketing. Comparison now also refuses to pair different **period types** — a full year against a single quarter is a different measurement, not a change.

---

## 9. Insights, anomalies, risks (§15, §23, §24)

**Insights** are arithmetic over the document's own figures. Each carries the fact ids it used and is labelled `reported` or `calculated`. An insight *states a computed relationship*; it never explains one. A defect fixed here: reported and calculated margins were being compared as two points on one series, turning a 332 bps arithmetic difference into a fake "467 bps expansion". Series are now keyed by derivation as well as entity and metric.

**Anomalies** (8 detectors) each name the rule broken, the values, the threshold crossed and the source facts. Thresholds are stated constants, not learned — a single document is far too small a sample to learn a distribution from.

**Risks** are quoted verbatim with page, category and the phrase that triggered the match. The standard safe-harbour disclaimer is excluded as legal boilerplate. **No overall risk score is produced** — the document does not state one, and an invented score could not be checked against the page. The API says so explicitly in its response.

---

## 10. Explanation service (§19, §21)

`GET /documents/{id}/financials/{fact_id}/explanation` answers *why do you believe this?* from stored evidence only: why this metric, why this period, why this entity, why this unit, whether it was reported or calculated, every confidence component with the weight it carried, the validation outcomes, related facts, and the source box.

It is templates over recorded values — deterministic (the same fact always yields byte-identical output, asserted by test) and grounded (a sentence that cannot be backed by a recorded value is not written). The response states `generated_by: "deterministic templates over recorded evidence (no language model, no external API)"`.

---

## 11. Database (§26)

7 new tables — `entities`, `table_semantics`, `derived_metrics`, `fact_relations`, `risks`, `insights`, `anomalies` — plus 14 new columns on `financial_facts`.

Migration is **additive only**: it creates missing tables and indexes and adds missing columns with constant defaults. It never drops or rewrites. Development data survived: the dev database now holds 23 tables and 1,907 facts with no pending changes.

---

## 12. Test results

```
286 passed, 1 skipped
```

60 new tests. Several exist specifically to pin defects found while building:

* a narrative sentence naming two units attributes to the nearer one;
* consolidated and standalone never merge;
* a stock over a quarterly flow is annualised;
* a monetary result is compared on the reported magnitude;
* differently-labelled rows are not a contradiction;
* period-less facts are never compared;
* column periods are repaired across a page break;
* reported and calculated rates are never compared with each other.

---

---

## 13. Evaluation (§31)

Two measurements are reported separately and must not be conflated.

### 13a. Ground-truth spot check

A hand-verified key of high-value figures, checked against what the pipeline extracted. This is the **accuracy** measurement.

| Document | Mode | Precision | Recall | F1 | Matched |
|---|---|---|---|---|---|
| RIL media release (34 pp) | HYBRID | 0.977 | 0.977 | **0.977** | 83 / 85 |
| RIL media release (34 pp) | VISUAL_ONLY | 0.977 | 0.977 | **0.977** | 83 / 85 |
| RIL image-only (7 pp, no text layer) | VISUAL_ONLY | 0.988 | 0.988 | **0.988** | 80 / 81 |

The spot check covers 85 facts on the full document and 81 on the image-only one. It is **not** a measurement over all 432 extracted facts — the remaining facts have no verified key, and no accuracy figure is claimed for them.

### 13b. Semantic coverage

How much of the semantic layer the document supported. These are **coverage counts over what was extracted, not accuracy**.

| | native/HYBRID | native/VISUAL_ONLY | image-only/VISUAL_ONLY |
|---|---|---|---|
| Facts extracted (full document) | 432 | 431 | 255 |
| With a period | 93.1% | 94.0% | 100% |
| With an entity | 100% | 100% | 100% |
| Placed in a statement | 100% | 100% | 100% |
| With a currency | 72.2% | 72.4% | 80.0% |
| With a unit | 86.1% | 86.3% | 94.1% |
| Entities discovered | 8 | 8 | 5 |
| Derived metrics | 47 | 47 | 40 |
| — with a published counterpart | 33 | 33 | 24 |
| — **agreeing** | **33** | **33** | **24** |
| — disagreeing | 0 | 0 | 0 |
| Relations | 900 | 903 | 643 |
| Risk statements | 15 | 15 | 2 |
| Insights | 40 | 40 | 40 |
| Anomalies | 9 | 9 | 4 |

Every ratio the document published reproduces from the document's own figures. The 27.8% of facts without a currency are overwhelmingly operational KPIs and ratios, which correctly have none.

---

## 14. Performance (§35)

Measured on the development machine (Windows 11, CPU-only, no GPU), one document at a time with nothing else running.

| Document | Mode | Pages | Cold wall | Cold ms/page | Warm wall | Warm ms/page |
|---|---|---|---|---|---|---|
| RIL media release | HYBRID | 34 | 373.9 s | 10,998 | 89.4 s | 2,630 |
| RIL media release | VISUAL_ONLY | 34 | 291.3 s | 8,569 | 105.0 s | 3,089 |
| RIL image-only | VISUAL_ONLY | 7 | 54.7 s | 7,808 | 81.4 s * | 11,624 * |

\* The image-only "warm" figure is slower than its cold one because it was measured third in a process that had just handled two 34-page documents; it is a contended measurement, not a cache effect. The cold figure is the reliable one for that document.

**Cold** is a document seen for the first time; **warm** is a re-run with the content-hash OCR cache populated. The difference is almost entirely RapidOCR: recognition dominates, and everything downstream — layout, tables, extraction, and the whole Milestone 2 analysis stage — is comparatively cheap.

The analysis stage itself costs **64 ms** on a 34-page document carrying 432 facts, 900 relations and 47 derived metrics. Milestone 2 added no meaningful processing cost.

Visual-only is slightly *slower* than hybrid on a text-layer PDF because it cannot use the text layer to corroborate; the difference is within run-to-run noise.

---

## 15. Visual-only regression (§32)

**The Milestone 1.5 guarantees are intact.** Verified by re-running all three configurations after the Milestone 2 work:

| | native/HYBRID | native/VISUAL_ONLY | image-only/VISUAL_ONLY |
|---|---|---|---|
| Native-text accesses allowed | 35 | **1** | **1** |
| — by scope | ingestion 1, corroboration 34 | ingestion 1 | ingestion 1 |
| Blocked (violations) | 0 | 0 | 0 |
| `extraction_fed_by_native_text` | true | **false** | **false** |
| Facts | 432 | 431 | 255 |
| F1 (spot check) | 0.977 | 0.977 | 0.988 |

In visual-only mode the only native-text access is the single ingestion-time probe that decides whether the PDF is scanned — scope `INGESTION_VALIDATION`, which by construction cannot feed extraction. Nothing in the intelligence layer touches the text layer.

The image-only PDF has **no text layer at all**; it is processed to 255 facts at F1 0.988, and the entire Milestone 2 semantic stack — entities, derived metrics, relations, insights, anomalies — is built on those pixel-derived facts.

Visual-only produces 431 facts against hybrid's 432, with identical intelligence output (47 derived metrics, 40 insights, 9 anomalies, 15 risks). **The semantic layer does not depend on the text layer.**

---

## 16. Cross-document comparison (§34)

Two real documents were used: the RIL media release and the genuinely image-only PDF built from its own pages in Milestone 1.5. No financial content was fabricated for either.

This pairing has a useful property: because the underlying figures are identical, every like-for-like match *should* show zero change — so any difference is a genuine extraction inconsistency between the two representations rather than a business movement. The test asserts exactly that, and it passes.

Getting there required fixing a real defect. Without explicit periods, the comparison picked the highest-confidence fact per (metric, entity) regardless of period, so it was pairing FY26 revenue against Q4 FY26 revenue and calling the difference a change. Comparison now:

* refuses pairs whose **period types** differ (a year against a quarter is a different measurement, not a change);
* labels each row `like_for_like`, `period_over_period` or `not_comparable`, and every refusal says why.

---

## 17. What is *not* claimed

Stated plainly, because a system that overstates itself is worse than one that admits its limits:

1. **Accuracy is measured on a spot check, not on every fact.** F1 0.977 covers 85 verified facts, not all 432.
2. **27.8% of facts carry no currency.** Mostly ratios and operational KPIs that correctly have none — but that figure does not distinguish "has no currency" from "currency unknown".
3. **6% of facts have no period.** They are excluded from comparison, validation and ratio computation rather than guessed at.
4. **The rupee glyph is never read from pixels.** PP-OCR's charset cannot emit `₹`. Visual-only infers INR from India-only magnitude words, flagged `currency_inferred`. A scanned rupee document that never writes "crore" or "lakh" leaves currency UNKNOWN.
5. **Chart values are read only when printed.** No value is inferred from bar geometry; charts without data labels are stored with `values_extracted = False`.
6. **No risk score exists.** Only counts of what the document itself says.
7. **Three anomalies on the RIL filing remain unexplained** — an EBITDA figure differing between pages 7 and 8, and two unit-change flags. They are surfaced for review rather than suppressed.
8. **The second document is the same filing in another representation**, not a second company. It validates the comparison machinery; it does not demonstrate cross-company analysis.
9. **Timings are single-machine, single-run.** No distribution was gathered.

---

## 18. Constraints honoured

| Constraint | Status |
|---|---|
| No paid LLM API | Yes — no network call anywhere in the runtime. Insights, anomalies, risks and explanations are deterministic templates over stored evidence. |
| Visual-only mode intact | Yes — 431/432 facts, identical intelligence output, `extraction_fed_by_native_text = false`. |
| Native-text boundary unweakened | Yes — `app/core/modes.py` untouched. 1 allowed access in visual-only, scope `INGESTION_VALIDATION`, 0 blocked. |
| No company-specific extraction | Yes — no filer name, segment name or metric value is hardcoded. Entity discovery, reporting-basis detection and the document-word rejection list all use generic vocabulary. |
| Architecture not redesigned | Yes — analysis runs as a new stage after validation; the extraction path was extended, not replaced. |
| Operational KPIs not forced into financial statements | Yes — `statement = operational`, `is_operational = True`. |
| Never override a table unit with narrative prose | Yes — strict layering, plus two new currency rules. |
| Never silently convert currencies | Yes — mixed-currency inputs produce no derived metric. |
| Calculated never presented as reported | Yes — separate table, separate export section, labelled in the UI. |
| Do not blindly delete duplicates | Yes — eight-way classification; only exact repeats merge. |
| Do not silently choose between conflicts | Yes — both kept, both flagged, linked, marked "unresolved". |
| Never validate incomparable facts | Yes — period, period type, entity, unit and currency all gate comparison. |
| Explanations require no LLM | Yes — deterministic and byte-identical across runs (asserted by test). |
| Do not inflate evaluation numbers | Yes — spot check and coverage reported separately, with counts. |
| Do not destroy development data | Yes — additive migration; 1,907 facts preserved across 23 tables. |
| No deployment / cloud work | Yes — none performed. |

---

## 19. API surface added

```
GET /documents/{id}/entities                              entity list + hierarchy
GET /documents/{id}/financial-summary                     facts by statement → category → metric
GET /documents/{id}/derived-metrics                       ratios with formula + inputs
GET /documents/{id}/relations                             typed fact graph
GET /documents/{id}/risks                                 cautionary statements
GET /documents/{id}/insights                              source-linked observations
GET /documents/{id}/anomalies                             explainable flags
GET /documents/{id}/table-semantics                       table type, column roles, periods
GET /documents/{id}/taxonomy-coverage                     which metrics this document supplied
GET /documents/{id}/financials/{fact_id}/explanation      why the system believes one figure
```

36 endpoints total. Export (`?format=json`) now carries `entities`, `derived_metrics`, `relations`, `risks`, `insights`, `anomalies` and `table_semantics` as separate sections, with a `notice` field stating that reported and calculated figures are never merged. CSV gains 13 semantic columns.

## 20. Frontend

Two new tabs and one new panel, added without redesigning the existing UI:

* **Summary** — facts by statement and category, periods across the row, entity selector driven by the discovered hierarchy. Calculated changes are marked with an asterisk.
* **Insights** — insights, anomalies and risks, each linking back to the facts or page it came from.
* **Explanation panel** — opens on any figure; shows why this metric / period / entity / unit, the confidence components with their weights, validation outcomes, and the source text.

The review queue now states *why* each figure is queued, quoting the anomaly's own explanation rather than showing a bare confidence score.
