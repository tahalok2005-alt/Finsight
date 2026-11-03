# Visual structure: what the pipeline sees today

Written before any Phase 16 code, from the running system and one real
document: `IDFC_Q12027.pdf`, 75 pages, run `4802d78a` of 8 September 2026
(690.8 s cold, 102 facts). Every number below was measured, not estimated.
Where a measurement contradicts what the code's comments claim, the
measurement is what is written down.

---

## 1. Visual region types that exist

`RegionType` (`backend/app/models/enums.py:97`) declares fifteen. On the IDFC
document the detector emitted twelve:

| Region type | Count | Produced by | Consumed by |
|---|---:|---|---|
| `section_heading` | 276 | `_classify_line` | table captioning, entity assembly |
| `chart` | 197 | `classify_graphic` | `charts/detector.py` |
| `paragraph` | 167 | `_classify_line` | narrative extraction, risks |
| `footer` | 74 | `_classify_line` | nothing |
| `header` | 64 | `_classify_line` | nothing |
| `table` | 61 | `detect_table_bands` | table reconstruction, then facts |
| `caption` | 24 | `_classify_line` | chart titling |
| `footnote` | 21 | `_classify_line` | narrative extraction |
| `title` | 13 | `_classify_line` | entity assembly |
| `list` | 8 | `BULLET_RE` | narrative extraction |
| `figure` | 8 | `classify_graphic` | `charts/detector.py` |
| `kpi_block` | **1** | `_kpi_blocks` | narrative extraction only |

`misc` is emitted and dropped. `page_number` did not appear.

### Which are actually load-bearing

Only three region types can lead to a `FinancialFact`:

* **`table`** - 61 detected, and 76 of the document's 102 facts came from
  them. But those 76 came from **six** pages (14, 18, 41, 44, 45, 59). The
  other 55 table regions produced nothing at all.
* **`paragraph` / `list` / `footnote` / `kpi_block`** - narrative extraction,
  13 facts.
* **nothing at all** - the 13 KPI facts did not come from a region. See section 3.

`chart` and `figure` reach `charts/detector.py`, which by design never emits a
`FinancialFact`; charts are stored as their own records. In this run they
produced **zero** analytical output and consumed **145.9 s, 21.1 % of the
run**. Section 5 is entirely about that.

`kpi_block` is the interesting one: the layout detector has had a KPI concept
since the first milestone, and on a 75-page investor deck built almost
entirely out of KPI tiles it fired **once**. `_kpi_blocks` requires a line
whose font is at least 1.25x the body font *and* a smaller neighbouring line
within 3.5 line-heights *and* no more than 4 elements on the line. On a slide
deck the "body font" is itself large, so the font-ratio gate almost never
opens. The region type is effectively dead, and the Phase 15 work routed
around it rather than through it.

---

## 2. What the geometry layer actually offers

`OCRElement` carries `id`, `text`, `bbox`, `confidence`. `TextLine` groups
elements into baselines. `Region` groups lines. Three levels, and the
important thing for Phase 16 is that **they are progressively lossier**:

```
OCRElement   exact box per recognised fragment      <- spatial/ works here
TextLine     baseline grouping, x-order kept
Region       union box + concatenated text          <- the "keyword soup" lives here
```

A `Region`'s `text` is `" ".join(line.text)`. For a chart region that is how
`"Mortgages Vehicles MSME Consumer Loa..."` came about in an earlier phase:
the axis category labels of one bar chart, concatenated. Any Phase 16 work
that reasons about *arrangement* must work at the `OCRElement` level, as
`spatial/` already does. Reading `Region.text` and hoping to recover layout is
what produced that defect.

`BBox` (`app/core/geometry.py`) supplies `contains_ratio`, `h_overlap_ratio`,
`intersection`, `padded`, `union_all`, `cx`, `cy`. That is enough for
everything Phase 16 needs; no new geometric primitive is required.

---

## 3. The Phase 15 spatial layer, as built

`spatial/geometry.py` to `spatial/cards.py` to `spatial/extract.py`, called
from `pipeline._extract_cards`.

It takes `art.elements` - the raw OCR elements for the page - and **never
looks at regions at all**. Its only input from another strategy is
`explained`, the bounding boxes of tables that produced at least one fact.

```
elements --> drop text inside `explained` tables
         --> set aside anything wider than 30% of the page  (-> "banners")
         --> cluster remaining elements into columns by x-centre
         --> refuse unless column centres are well separated
         --> split each column into runs at gap > 1.7 x the page's median gap
         --> refuse a run with no sibling run starting at the same height
         --> segment each run at every figure  -> tiles
         --> refuse unless at least 3 tiles survive on the page
```

This is a **vertical**-only reader by construction: `cluster_columns` groups on
x-centre and `split_into_runs` walks down y. A label sitting *beside* its value
is invisible to it - the two land in different columns and neither run
contains a label-value pair.

Measured on IDFC: 16 pages produced tiles, 13 facts survived extraction.
Rejections across all 75 pages:

| Stage | Reason | Count |
|---|---|---:|
| detection | `figure_with_nothing_naming_it` | 562 |
| detection | `run_too_short` | 276 |
| detection | `tile_not_repeated_across_the_layout` | 204 |
| detection | `no_label_in_tile` | 86 |
| detection | `no_repeated_layout` | 26 |
| extraction | `label_not_a_known_metric` | 51 |
| extraction | `percentage_value_for_a_monetary_metric` | 3 |
| extraction | `value_is_a_multiple_not_an_amount` | 1 |
| detection | `tile_too_long_to_read_confidently` | 1 |

### Two confirmed defects in the 13 accepted facts

Both are on page 74, a **six-panel bar chart page**, and both were verified
against the rendered page.

**(a) `net_interest_margin = 2.1 %`, period `H1 FY19`.** The page's NIM panel
plots four bars: FY16 2.0 %, FY17 **2.1 %**, FY18 1.7 %, H1FY19 1.9 %. The
figure read is FY17's. The period came from here:

```
period_inherited_from: "*The bank took one-time provisions relating to
  stressed infrastructure loans. Without such one-time charge off, the PAT
  for H1 FY19 would be US$ 9 Mn."
```

That is the page's **footnote**. `_extract_cards` calls anything wider than
30 % of the page a "banner" and offers the first parseable period in it to
every tile on the page. A footnote is wide. So a period was lifted out of a
sentence about a different metric and attached to a chart bar. Two failures
compounding: the tile should not have been read, and the period should never
have been inheritable from prose.

**(b) `casa_deposits = 603`, period `As at 2018-03-31`.** The run that
produced it was:

```
'Mar-18'                    (557, 730)   <- x-axis tick of the chart ABOVE
'CASA Deposits (USs Mn.)'   (312, 849)   <- title of the chart BELOW
'603'                       (583, 934)   <- a bar data label
```

Three fragments from three different chart elements, stitched into one "tile".
The value happens to be correct for Mar-18 by coincidence of column position.
A right answer for no reason is not a right answer.

### The obvious guard does not work

The tempting fix - refuse tiles inside detected chart regions - was tested
against the data before being proposed, and it is wrong in both directions:

| Fact | Correct? | Inside a `chart`/`figure` region? |
|---|---|---|
| p74 `net_interest_margin` 2.1 % | **no** | 0.00 |
| p74 `casa_deposits` 603 | coincidence | 0.00 |
| p2 `cost_of_funds` 5.96 % | yes | **1.00** |
| p2 `roa` 1.06 % | yes | **0.69** |

The blob detector missed page 74's bars entirely, and drew "charts" over two
of page 2's perfectly good KPI tiles. Region type is not evidence here.

---

## 4. Where horizontal structures actually are

Probed directly: for every page, group OCR elements onto shared baselines and
count adjacent `label | value` pairs. Ten pages carry three or more:
7, 8, 21, 27, 30, 35, 36, 62, 68 and 75.

**Genuine, and worth extracting:**

* **p62** `Total No. of shares | 861.47 Cr`, `Book Value per Share (June 30,
  2026) | Rs. 56.47 / US$ 0.60`, `Basic EPS (Annualized Q1 FY27) | Rs. 5.01 /
  US$ 0.05`. Label left, value right, stacked, with a period-heading block
  `Shareholding (June 30, 2026)`. Exactly the Part 3 and Part 4 target shapes.
  Note the second and third rows carry **two** values (rupees and dollars) -
  the same quantity through an exchange rate, not a scale conversion.
* **p29** `Loan Book: June 30, 2026 / US$ 32,243 Mn.` - a period heading
  governing one value.
* **p7** `Progress since merger`: row labels down the left (Loan Book,
  Customer Deposit, CASA Ratio, Net Worth, NIM %, Cost to Income %, Gross NPA,
  Net NPA) against five column heads, three of them dated. A table by intent,
  drawn with rounded rectangles instead of rules, so `detect_table_bands`
  found one band and reconstruction read nothing from it.

**Traps that must be refused:**

* **p8** `De-grew | 5%`, `from | US$ 7,152 Mn.`, `Grew | 6.7x`. The real
  labels are Corporate / Retail / Rural / SME; "from" and "Grew" are
  connectives. Each row carries three figures (a multiple, a start value, an
  end value) under two dated heads. Any reader that takes the first adjacent
  number produces `Retail = 6.7`.
* **p27** `strong deposit franchise | 32,243` - the tail of a sentence beside
  a chart's data label.
* **p21, p35, p36, p75** - rotated axis labels recognised as `%SO'T`,
  `T-en`, `81-qe`. Garbage that a permissive reader would pair up happily.
* **p64** a credit-rating grid: `CRISIL | AAA`, `ICRA | AA+ (stable)`. Perfect
  horizontal geometry, five repetitions, and **no financial value anywhere**.
  The best possible regression test for "layout evidence is not enough".

The conclusion for Part 3 is that horizontal detection cannot lean on geometry
alone. p64 has better repetition evidence than p62 does.

---

## 5. Chart detection: audit of the 205

### What they are

Clustering the 205 detections by geometry (20 px buckets) and counting the
distinct pages each cluster appears on:

| Cluster | Pages | Regions | Classified as | What it is |
|---|---:|---:|---|---|
| (1813, 0) 854x62 | 57 | 57 | `unknown` | the header ribbon and logo |
| (0, 1334) 2667x166 | 48 | 48 | **`pie`, conf 0.9** | the footer strip |
| (0, 117) 2667x56 | 12 | 12 | `unknown` | the rule under the page title |
| (0, 0) 547x1500 | 6 | 6 | **`pie`** | a full-height coloured side panel |

**123 of 205 (60 %) are page-template furniture** - the same box, on five or
more pages, at pixel-identical geometry. Verified by cropping and looking:
the first is the IDFC FIRST Bank logo; the fourth is a solid red panel with
watermark logos and no data of any kind.

Of the remaining 82, sampled and inspected by eye:

| Class | Examples seen |
|---|---|
| genuine financial chart | p10, p14 (column), p35, p63 (line), p33, p51 |
| **chart fragment** - one chart split into 4-5 blobs | p36 x4, p37 x4 |
| **KPI visual** - a tile, not a chart | p2 x3 (`Risk Adjusted NII (Q1-FY27) / 4.75 %`) |
| decorative | p64 rating badges, p68 award photographs, p1 logo |
| false positive | p1 540x524 empty panel, p72 background |

The type classifier is separately unreliable: **92 of 205 came back `pie`**,
including the footer strip 48 times, a genuine *line* chart (p63 share price)
and a genuine *column* chart (p35). `HoughCircles` fires on rounded corners
and circular icons, and `classify` weights `radial_score > 0.4` above
everything else.

Precision estimate, stated honestly: **about 12 % of detections are a genuine,
whole, distinct financial chart** (roughly 25 of 205 after merging fragments).
Recall is decent - the real charts are found - but they arrive fragmented,
mislabelled and buried in furniture.

### What it costs, and where the cost is

Measured by replaying the stage over the cached page renders:

| | |
|---|---:|
| page images loaded (73) | 2.6 s |
| `analyse_region` x 205 | **122.0 s** |
| accounted for | 124.6 s of the stage's 145.9 s |

Cost tracks region area: the twelve slowest are all at least 0.4 of a page,
and the worst single region (p68, 2393x1071) takes **7.5 s**.

Profiling inside `analyse_region` over a 30-region sample:

| Operation | Time | Share |
|---|---:|---:|
| **`np.unique(q, axis=0)` - distinct colour count** | **34.50 s** | **85.6 %** |
| `cv2.HoughCircles` | 4.94 s | 12.3 % |
| `cv2.HoughLinesP` | 0.50 s | 1.2 % |
| binarize + HSV prep | 0.18 s | 0.4 % |
| bar contour counting | 0.10 s | 0.2 % |
| Canny | 0.08 s | 0.2 % |

Six-sevenths of all chart-processing time is spent counting how many of 64
possible quantised colours appear in a region - via a lexicographic sort of
several million RGB triples. `np.bincount` over a packed 6-bit index returns
**the identical number** 42-94x faster (verified bit-for-bit on synthetic
arrays and on page 35: both give 28). The same statement appears a second time
in `layout/cv_signals.py:detect_graphics`.

This is not a trade-off between speed and correctness. It is one line.

### Decision

The brief offers "reduce its priority" or "preserve the detection layer".
Neither on its own is right, and the evidence points somewhere more specific:

1. **Fix the cost, not the feature.** The colour count is a defect. Charts
   keep every signal they have today.
2. **Suppress template furniture before analysis**, on the document-general
   evidence that the same box recurs at the same place on many pages. This
   removes 60 % of the regions and is not an IDFC rule - it is what a slide
   master is.
3. **Do not disable chart support**, and do not narrow detection to make a
   benchmark faster. The RIL benchmarks contain genuine charts.
4. **Do not attempt chart value extraction in this phase.** The brief is
   explicit and the audit agrees: what is printed on these charts is a
   category axis, a series legend and per-bar data labels, and assigning bars
   to periods is a separate piece of work with its own failure modes - the
   page-74 defect is precisely what happens when it is done casually.

---

## 6. Routing and precedence today

There is one routing decision in the system, added in Phase 15, and it is
implicit:

```python
explained: list[BBox] = []
from_table = extract_from_table(table, ctx)
if from_table:
    explained.append(BBox.from_dict(table.bbox.model_dump()))
...
self._extract_cards(art, model, facts, image_url, currency, unit, explained)
```

Order is table, then cards, then narrative, fixed in code. Narrative runs on
region types the other two never touch, so it does not contend. The only
contention is table-versus-cards and it is resolved by *area a table actually
explained*. That rule is correct and was arrived at the hard way - excluding
all detected tables cost all 13 IDFC KPI facts, because the band detector lays
low-quality grids over KPI bands and reads nothing from them.

What is missing for Phase 16:

* No **page-level structure decision**. Every strategy is offered every page.
* No **region-level record** of which extractor saw what, or why it declined.
  `_page_diagnostics` records counts per page - `kpi_cards_detected`,
  `kpi_detection_rejected` - but never *which* label was refused, so
  "51 x `label_not_a_known_metric`" cannot be turned into a taxonomy fix
  without re-running with a debugger.
* Cards claim ground from tables but **nothing claims ground from cards**. A
  horizontal reader added naively would re-read every page-2 tile from the
  other axis.

Precedence for Phase 16 should therefore be, and the reason for each rung is
the evidence above rather than a general principle:

1. **Table extraction that produced facts** - highest structural evidence, and
   already the anchor of the existing rule.
2. **Vertical KPI tiles** - repetition across columns is strong evidence, and
   this is proven on real pages.
3. **Horizontal KPI rows and period blocks** - weaker: p64 shows perfect
   geometry with no financial content, so this rung must lean on the taxonomy
   and the number normaliser far more than on layout.
4. **Narrative** - sentences, which the other three deliberately refuse.

Each rung claims the ground it *explained*, never the ground it merely
*touched*.

---

## 7. How confidence should differ by strategy

`extraction/confidence.py` scores `ocr`, `layout`, `structure`, `semantic`,
`corroborated`. The strategies differ in what `structure` can honestly mean:

| Strategy | `structure` means | Ceiling |
|---|---|---|
| table cell | row label by period column intersection, reconstructed grid | highest - two independent axes agree |
| vertical KPI tile | tile repeated across N columns at one height | `0.55 + 0.08 x cards_on_page`, today |
| horizontal KPI row | label and value share a baseline, row repeated | **must be lower than vertical**: the same geometry describes p62 (real) and p64 (ratings) |
| period block | a heading spatially contains a group | lower still - containment is one piece of evidence, not two |
| narrative | a sentence pattern matched | lowest |

The asymmetry is not a preference. A vertical tile's parts are enclosed by
whitespace on all four sides; a horizontal pair's are separated only along
one axis, and every table row, every legend and every rating grid has that
shape too.

---

## 8. Where Phase 16 should add code, with least duplication

* **`spatial/geometry.py`** - add row clustering (`cluster_rows`) beside the
  existing `cluster_columns`. Same primitives, other axis.
* **`spatial/` new module for horizontal reading** - must not extend
  `cards.py`; the tile model (`label, period, value, change` in a vertical
  run) does not describe a row.
* **`spatial/extract.py`** - reuse wholesale. Taxonomy match, number
  normalisation, magnitude and currency policy, the multiple-notation refusal
  and the percentage-versus-monetary refusal all apply unchanged to a row.
  Only the period source differs.
* **`pipeline._extract_cards`** - becomes a page-level router. Its "banner"
  concept must be repaired first: a banner is a *heading*, not merely
  something wide.
* **`layout/cv_signals.py`** and **`charts/detector.py`** - the colour count.
* **`_page_diagnostics`** - needs region-level entries, not just counts.

Nothing in the existing table, narrative, validation, confidence, provenance
or persistence layers needs to change. That is the point of routing every
strategy through `FinancialFact`.

---

## 9. Summary of findings

1. `kpi_block` as a region type is dead - 1 hit on a deck made of KPI tiles.
2. The spatial layer is region-blind by design and should stay that way.
3. Two of thirteen KPI facts are defective, both on a chart page; the causes
   are period inheritance from a footnote and a run stitched across three
   chart elements.
4. Refusing tiles inside chart regions would delete correct facts and keep
   the incorrect one. Region type is not usable as a guard here.
5. 60 % of chart detections are page furniture; about 12 % are genuine
   distinct charts; `pie` is over-predicted 92/205.
6. 85.6 % of chart processing time is a distinct-colour count that has an
   exactly equivalent implementation 42-94x faster.
7. Horizontal structures exist and are worth reading, but the strongest
   horizontal geometry in the document (p64 credit ratings) contains no
   financial value at all.

---

## 10. What Phase 17 added

The routing described in section 6 gained a fifth strategy and, more
importantly, a layer beneath all of them.

| Module | Role |
|---|---|
| `spatial/transposed.py` | a drawn grid: figures at repeated x and y, a label axis, a period axis |
| `charts/triage.py` | what a chart could be asked for, gathered by association rather than containment |
| `analysis/identity.py` | what makes two readings the same fact, and whether they are comparable |
| `normalization/evidence.py` | cell > row > table > taxonomy, shared by every path |

The routing order is now:

```
table extraction that produced facts
        │
        ▼
vertical KPI tiles          ── claim every tile offered
        │
        ▼
drawn grids                 ── two axes of evidence
        │
        ▼
horizontal metric rows      ── one axis
        │
        ▼
narrative
        │
        ▼
EVIDENCE RECONCILIATION     ── duplicate · corroboration · contradiction
```

The last stage is what makes the ordering safe rather than merely exclusive.
Where two strategies read the same figure, the earlier design relied on each
extractor's own guards to prevent double counting; now the overlap is
*detected* and becomes corroboration. Measured on one filing: the grid reader
produced 115 candidates on a page the table extractor already read, and the
document's fact count rose by four.

Section 5's chart findings are superseded in one respect. The audit for that
phase reported that chart regions were 60 % page furniture; that remains true.
What it did not measure is that the surviving regions **do not contain their
own data labels** — a region is the ink of the bars, and the labels sit above
them. See [reports/PHASE_17_REPORT.md](reports/PHASE_17_REPORT.md) §7.
