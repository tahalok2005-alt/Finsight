# Evidence reconciliation: what the system does today

Written before any Phase 17 code, by instrumenting the running system against
four already-processed documents:

| | facts | pages |
|---|---:|---:|
| `IDFC_Q12027.pdf` | 104 | 75 |
| RIL media release | 431 | 34 |
| RIL scanner-degraded | 251 | 7 |
| Apple statements | 52 | 3 |

Every count below was measured on those documents. Where a mechanism turns out
to be unreachable, that is stated as a measurement rather than as a reading of
the code.

---

## 1. How two candidate facts currently become one

One function: `app/analysis/reconcile.py::reconcile`, called once at the end of
extraction, over the flat list of every fact from every strategy.

```
facts ──▶ bucket by _scope_key(f)
      ──▶ within a bucket: sort by (confidence, kind == "table_cell")
      ──▶ anchor = the first
      ──▶ agreeing   = those whose value is within 0.5 % of the anchor
      ──▶ disagreeing = the rest
      ──▶ agreeing collapse into the anchor (duplicate_count, duplicate_pages)
      ──▶ disagreeing are classified, kept, and linked
```

Nothing is deleted. `agreeing` facts are removed from the returned list but
their ids and pages are recorded on the anchor and a `duplicate_of` relation is
written for each.

**The comparison is on `value`, not on `base_value`.** `_same_base` exists and
handles magnitude differences, but it is only reachable from inside `classify`,
which is only reached inside a bucket — and unit and currency are part of the
bucket key. So two facts that differ in scale can never be compared. On these
four documents that gap is latent rather than active: **0 pairs** share a
metric, entity and period while agreeing on `base_value` and differing in
unit or currency. It is a real hole, but not one these documents fall into.

---

## 2. How contradictions are currently detected

`classify(a, b)` is documented as returning one of eight verdicts. Measured
across all four documents, by running it over every pair inside every bucket:

| Verdict | IDFC | RIL | Degraded | Apple |
|---|---:|---:|---:|---:|
| `genuine_conflict` | 2 | 6 | 76 | 0 |
| `identical_repeat` | 0 | 0 | 2 | 0 |
| `different_entity` | 0 | 0 | 0 | 0 |
| `different_period` | 0 | 0 | 0 | 0 |
| `different_definition` | 0 | 0 | 0 | 0 |
| `different_unit` | 0 | 0 | 0 | 0 |
| `different_scope` | 0 | 0 | 0 | 0 |
| `uncertain` | 0 | 0 | 0 | 0 |

**Six of the eight verdicts are unreachable, and it is structural rather than
accidental.** Every discriminator `classify` tests — entity, period, printed
label, value type, unit, currency — is already part of `_scope_key`, so inside
a bucket they are equal by construction. The only two questions left to ask are
"is the value the same" and "is it not".

The classification vocabulary the module advertises therefore describes
distinctions the module cannot draw.

---

## 3. What constitutes the identity of a financial fact

```python
def _scope_key(f):
    if not f.period:
        return ("__unattributed__", f.fact_id)      # its own bucket, always
    return (f.canonical_metric or f.metric.lower(),
            _normalised_label(f),                   # the label as printed
            f.entity_id or f.entity or "",
            f.period,
            str(f.value_type), str(f.unit), str(f.currency))
```

Three properties of this key decide almost everything about the system's
behaviour, and two of them are wrong.

### 3a. A fact with no period is its own bucket

Deliberate, and defensible in isolation: several columns of one table routinely
fail period resolution together, and bucketing them would declare this
quarter's EBITDA to contradict last quarter's.

But it means such facts can neither corroborate nor contradict **anything**:

| Document | facts with no period | share excluded from reconciliation |
|---|---:|---:|
| IDFC | 81 of 104 | **78 %** |
| RIL | 26 of 431 | 6 % |
| Degraded | 0 of 251 | 0 % |
| Apple | 0 of 52 | 0 % |

On the document this phase is judged against, reconciliation sees 23 facts.

### 3b. The label *as printed* is part of the identity

The intent is documented and sound: "Total Throughput" and "Production meant
for sale" both match the taxonomy's `throughput` but are separate line items
carrying different numbers.

The cost is that two strategies reading the same figure under two printed names
never meet. Measured — groups sharing canonical metric, entity **and** period
while landing in different buckets:

| Document | such groups |
|---|---:|
| RIL | 36 |
| Degraded | 14 |
| IDFC | 2 |

Real examples:

```
RIL   depreciation  Q1 FY27   'Depreciation'                  15,100 crore
                              'Depreciation / Amortisation'   15,100 crore
```

The same number, the same period, the same entity, from two rows of the same
statement — recorded as two independent facts with no link between them. This
is corroboration the system had in hand and discarded.

```
Degraded  registered_customers  Q1 FY27
          table_cell         'Registered CustomerBase'        396.0 million
          narrative_sentence 'The registered customer base'   396.0 million
```

Cross-strategy corroboration — the thing Phase 17 exists to produce — already
occurs in the data and is invisible to the reconciler.

And the same mechanism hides contradictions:

```
Degraded  pat  Q1 FY27   'ProfitAfterTax'   7,764 crore
                         'ProfitAfterTax'   2,806 crore     ← same label, caught
Degraded  ebitda Q1 FY27 'EBITDA'  6,309 / 5,935 / 6,309 / 20.865 crore
Degraded  gross_revenue  'GrossRevenue' 90.408 / 45,961 ; 'gross revenue' 90,408
```

`90.408` and `90,408` are the same figure with a misplaced decimal point. They
agree on neither `value` nor bucket, so nothing notices.

### 3c. Unit and currency are part of the identity

See §1 — this makes `_same_base` unreachable. Latent on these documents.

---

## 4. Are entity, metric, period, unit and source region all considered?

Entity, metric, period, unit and currency: yes, all in the key. **Source region
is not considered at all** — neither in the identity nor in the classification.
Two facts read from the *same* pixels by two strategies are indistinguishable
from two facts read from different parts of the page. That is precisely the
distinction Phase 17 needs.

More seriously, **the entity in the key is frequently wrong.**

`entity_by_region` is built by walking regions in reading order and carrying
`current_entity_id` forward until another entity heading appears. There is no
terminator. On RIL this works well, because each segment has its own heading
page and the next heading ends the previous:

| RIL entity | pages | facts |
|---|---|---:|
| Consolidated | 1–22 | 143 |
| Standalone | 28–29 | 82 |
| JPL | 4–6 | 62 |
| RRVL | 7–8 | 60 |
| JIOSTAR | 18 | 38 |
| O2C | 11–14 | 26 |

On IDFC, one segment heading appears on page 23 and nothing resets it:

| IDFC entity | pages | facts |
|---|---|---:|
| **RAM** | **41–73** | **67** |
| RETAIL BANKING | 8–18 | 24 |
| Consolidated | 2 | 13 |

`RAM` is the deck's Retail/Agri/MSME lending book. It is attributed to page 45
(*"Operating Income (excluding trading gain) grew 21.5 % YoY"* — the bank's own
profit and loss), page 59 (risk-weighted-asset computation — capital adequacy
for the whole bank), page 62 (the shareholding pattern) and page 73 (the merger
background).

**64 % of IDFC's facts carry a segment name that does not own them.** This is
worse than the brief's framing of Defect B: it is not that spatial facts pick
`regions[0]`, it is that the section carry-forward has no end and the spatial
shortcut then reads from an arbitrary index into an unordered list.

On page 62, `art.regions[0]` is not even reading-order 0. The detector appends
tables first, then graphics, then text, and `assign_reading_order` sets the
`reading_order` field without re-sorting the list. So `regions[0]` is the first
*table* the detector happened to build.

---

## 5. Where competing candidates can overwrite each other

Nothing overwrites. Every fact either survives, or collapses into an anchor
that records where its copies were. That part of the design holds.

What the design does *not* do is notice competition in the first place. The
places a competing candidate is silently lost:

* **Different printed labels** (§3b) — 36 groups on RIL, 14 on the degraded
  scan. Neither corroboration nor contradiction is recorded.
* **No period** (§3a) — 78 % of IDFC.
* **Different scale** (§3c) — latent.
* **Value with `value=None`** — 2 on IDFC, 4 on RIL, 4 on the degraded scan.
  `_same_value` treats two `None`s as equal, so two unparsed facts in one
  bucket would "corroborate" each other. Not observed, but reachable.

---

## 6. Where confidence can incorrectly hide a contradiction

Three mechanisms, and the first is active on every document.

### 6a. The anchor of a conflict loses its review flag

`reconcile` sets `other.duplicate_resolution = GENUINE_CONFLICT` and marks both
facts `requires_review = True`. It **never sets the anchor's
`duplicate_resolution`.**

Afterwards `_finalise_confidence` runs:

```python
f.review_reason = conf_mod.review_reason_for(f, status)
f.requires_review = f.review_reason is not None
```

`review_reason_for` decides `CONFLICTING_SOURCES` from
`fact.duplicate_resolution == GENUINE_CONFLICT`. The anchor's is `None`, so it
gets no reason — and `requires_review` is overwritten back to `False` whenever
its confidence clears the threshold.

Measured, over facts appearing in a `conflicts_with` relation:

| Document | facts in a conflict | **not** flagged `conflicting_sources` |
|---|---:|---:|
| IDFC | 2 | **1** |
| RIL | 6 | **3** |
| Degraded | 70 | **37** |

On IDFC the surviving side of the document's only contradiction is presented at
confidence **0.9103, band HIGH, no review flag**. The reader is shown a clean
number and never told another part of the document disagrees.

### 6b. Corroboration is scored but never fed

`Signals.duplicate_agreements` carries a weight and a note. It is set in exactly
one place — `extraction/facts.py`, from within-table repeats — and never from
reconciliation, because reconciliation runs *after* confidence is scored. A
fact corroborated by three strategies scores identically to one seen once.

### 6c. Corroboration weight is 0.05

Even if it were fed, agreement between two independent strategies could move
the overall score by at most 0.05 — less than the effect of a single OCR
character. Contradiction, meanwhile, is categorical and must not be averaged;
today it is neither averaged nor applied.

---

## 7. Two findings outside the brief's list

### 7a. Chart data labels are geometrically excluded from their charts

`charts/detector.py` collects a data label when
`region.bbox.contains_ratio(line.bbox) > 0.6`. The chart region comes from the
graphic blob, which covers the *ink of the bars* — and printed data labels sit
**above** the bars. Measured on IDFC page 51, a bar chart with seven printed
percentages:

| label | `contains_ratio` | kept? |
|---|---:|---|
| 553.0 % | 0.58 | no |
| 226.5 % | 0.28 | no |
| 196.7 % | 0.00 | no |
| 181.6 %, 171.1 %, 146.1 %, 148.1 % | 0.00 | no |

Zero of seven. Across all 82 analysed charts the detector captured **38 data
labels in total**, and only 5 charts have three or more. The ones it did
capture are correspondingly unreliable — page 10 yields `343397434477.0`, page
55 yields `161173.0` on a chart whose real values are around 114.

Page 33 — the collection-efficiency chart with seventeen printed percentages
and a clean `Q1 FY23 … Q1 FY27` axis, the single most extractable chart in the
document — **has no chart region at all.**

So chart value extraction cannot be built on the present region geometry. Any
Part 7 work must gather evidence by *spatial association to* a chart, not by
containment within it, and must then measure how many charts actually qualify
rather than assuming they do.

### 7b. Validation runs on everything, and says little

All 838 facts across the four documents carry a validation status. Only 6
validation results exist for IDFC. Validation is not a meaningful contradiction
detector today; it checks arithmetic identities that most facts do not
participate in.

---

## 8. What this means for Phase 17

Ordered by measured damage, not by the brief's ordering:

1. **Entity attribution** (§4) — 67 of 104 IDFC facts mislabelled. The fix is
   two-part: a terminator for section carry-forward, and containment-based
   selection instead of `regions[0]`.
2. **The anchor's lost review flag** (§6a) — a contradiction presented as a
   clean HIGH-confidence figure, on every document that has one. Small fix,
   large consequence.
3. **Identity** (§3) — the printed label, the unit and the currency must leave
   the identity, and comparison must move to `base_value`. The label's real job
   is to *explain* a difference, not to prevent one from being seen; it belongs
   in the classification, which is where §2 shows there is nothing to do today.
4. **Undated facts** (§3a) — 78 % of IDFC is outside reconciliation. They
   cannot be bucketed by period, but they can be compared within a page and
   within a metric, which is strictly more than nothing.
5. **Corroboration into confidence** (§6b) — requires reconciliation to run
   before, or to re-score after.
6. **Chart geometry** (§7a) — triage must be built on association, and the
   measurement will probably show that very few charts qualify. That result
   should be reported, not engineered around.
