# Phase 20 — Statement coverage, safe table reconstruction, accounting validation

---

## 0. The short version

**Coverage 47.7% → 90.7%. Accuracy 100% → 100%.** Sixty-five more golden facts
extracted, none of them wrong in period, unit, entity or value — which is the
brief's own definition of success.

Three things are worth reading past the headline.

**The gap was not where it looked.** Classifying all 79 misses before fixing
anything showed 86% of them in one place — the metric vocabulary — and *zero*
in column geometry, period resolution, entity attribution or unit resolution.
Two of the three vocabulary fixes turned out not to be missing words at all,
but a trailing "for the period/year" that defeated 68 facts and two aliases
that mapped real lines onto the wrong metric.

**Three of the nine accounting checks had never run.** They named metric keys
the taxonomy does not define, so they matched nothing, failed nothing, and
produced nothing — which looks exactly like a document that does not support
the check. Nothing in the suite could see it. Groww went from 1 check to 38.

**I broke the thing this phase was meant to protect, and the corpus caught
it.** The new evidence-based fragment merger refused the very join it exists to
make, and Groww's period accuracy fell to 0% on 91 facts for one measurement.
The cause is a design lesson rather than a typo, and section 8.3 states it.
Three further self-inflicted defects were caught the same way — two separator
characters and a miscalibrated evidence model — and each is reported where it
belongs rather than quietly fixed.

The keyed accuracy gate shows **no regressions and three improvements**, with
the scanner-degraded benchmark moving for the second phase running after three
phases of standing still.

---

## 1–4. Coverage and accuracy

The brief's test: coverage up substantially, attribution accuracy not down.

| | Start | End |
|---|---|---|
| **Golden coverage** | 72 / 151 = **47.7%** | 137 / 151 = **90.7%** |
| **Attribution accuracy** | 72 / 72 = **100%** | 137 / 137 = **100%** |
| Wrong period | 0 | **0** |
| Wrong unit | 0 | **0** |
| Wrong entity | 0 | **0** |
| Wrong value | 0 | **0** |

Sixty-five more golden facts extracted, and not one of them wrong in period,
unit, entity or value.

Per table:

| Document | Page | Expected | Extracted on page | Matched | Missing | Wrong | Period | Entity | Unit |
|---|---|---|---|---|---|---|---|---|---|
| GROWW | 6 | 99 | 93 | **93** | 6 | 0 | 100% | 100% | 100% |
| HDFC | 1 | 52 | 60 | **43** | 9 | 0 | 100% | 100% | 100% |

HDFC's page holds more rows than its golden fixture transcribes, which is why
extracted-on-page exceeds matched.

---

## 5. Missing-fact root causes

Section 1 of the brief required every miss classified before anything was
fixed. `backend/app/evaluation/diagnose.py` walks the extractor's own path and
stops at the first stage that cannot account for the fact — ordering matters,
because a row the taxonomy does not know will also have no period, and
reporting it as a period failure sends the next phase to the wrong module.

**The starting picture, from 79 misses:**

| Cause | Count | Share |
|---|---|---|
| **A. taxonomy / metric vocabulary** | **68** | **86%** |
| C. label parsing (recognition damaged the label) | 8 | 10% |
| M. duplicate / corroboration handling | 3 | 4% |

Nothing was lost to column geometry, period resolution, entity attribution,
unit resolution, table fragmentation, the safety gate or reconciliation. The
Phase 19 architecture was holding; the gap was vocabulary, and 68 of the 79
misses came from **17 distinct row labels**.

**The picture now, from 14 misses:**

| Cause | Count |
|---|---|
| A. taxonomy / metric vocabulary | 8 |
| C. label parsing | 4 |
| M. duplicate / corroboration handling | 2 |

The remaining 8 taxonomy misses are one row whose label the table reader merged
with the sub-heading beneath it — `"Income tax relating to above Items that
will be reclassified to profit or loss"` — which is a row-boundary defect
reported as a vocabulary one. It is named in section 29.

---

## 6. Taxonomy improvements

Two of the three changes are not new vocabulary at all. They are places where
the matcher was answering the wrong question.

### 6.1 A trailing temporal phrase defeated 68 facts

`"Profit before tax"` matched its own alias. `"Profit before tax for the
period/year"` matched nothing.

The punctuation stripper keeps the slash, so `"for the period/year"` survived
normalisation as `"for the period year"` and dragged the label below the
similarity threshold against its own alias. Every Indian statement prints that
phrase on almost every row.

It says *when*, never *what*, so it is now removed before a label is compared
with anything — and matched anywhere in the label, not only at the end, because
a statement that groups rows under a heading puts it in the middle:
`"Profit for the period/year attributable to: Shareholders of the Company"`.

### 6.2 Two wrong mappings, which matter more than the gaps

| Label | Mapped to | Should be |
|---|---|---|
| `Total comprehensive income` | `other_comprehensive_income` (declared alias) | its own metric |
| `Total expenditure` | `capex` at 0.83 fuzzy | its own metric |

The first was a taxonomy data error: total comprehensive income and other
comprehensive income were one key, and they differ by the entire profit for the
period. An Ind-AS statement lists both, so a document reported two different
figures under one metric. The second reported a bank's whole cost base as
capital expenditure.

`total` and `other` are now contrast words: a fuzzy match is refused when one
side carries one and the other does not. They name a *different line of the
same statement*, not a different wording of the same line — which is exactly
what a similarity score cannot see.

### 6.3 New metrics, each from a row that produced no fact

Seventeen entries, every one taken from a real statement, each with a
definition, a value type and — where it has a dangerous neighbour — an explicit
`distinct_from`:

| Area | Added |
|---|---|
| Income statement | `pbt_before_associates`, `share_of_associates`, `total_comprehensive_income`, `pat_attributable_to_owners`, `oci_attributable_to_owners` |
| Other comprehensive income | `oci_remeasurement_benefit_plans`, `oci_fair_value_investments`, `foreign_currency_translation_reserve` |
| Costs | `total_expenditure`, `operating_expenses`, `other_operating_expenses` |
| Banking income | `interest_earned`, `interest_on_advances`, `interest_on_rbi_balances` |
| Balance sheet | `share_capital`, `other_equity` |

Deliberately **not** added: `d) Others`. It is a real row with real figures, and
"Others" is not a measure — an entry for it would match a sub-line of any
statement in the corpus. The system correctly declines to extract it, and the
four golden facts it accounts for stay missing on purpose.

### 6.4 Sub-headings that govern the rows beneath them

A statement writes:

```
Profit for the period/year attributable to:
    Shareholders of the Company        735.04  686.36  378.35  2,083.01
    Total                              735.04  686.36  378.35  2,083.01
```

Neither child names a measure alone, and read alone they produced nothing.
A label-only row ending in a colon now governs the rows under it, and the
child's label is tried **on its own first** — a row that names its own measure
keeps it, since "Revenue from operations" under any heading is still revenue
from operations and prefixing it would invent a measure the statement does not
report.

### 6.4a Two separator characters that were throwing figures away

The guard that refuses a cell holding two figures (section 9–11) was too blunt
in two ways, and both were caught by watching real documents rather than by
review:

* **`62005,98`** — a decimal comma. A thousands separator is always followed by
  exactly three digits, so a comma with two digits and nothing after it cannot
  be one. The guard read it as two numbers and discarded a bank's total
  expenditure outright.
* **`123，162`** — a *fullwidth* comma, which a degraded scan emitted for an
  ordinary one. Same effect.

Both are now normalised before the guard runs, along with the ideographic comma
and the apostrophe used as a group separator in Swiss notation. A guard that
protects against a wrong value by discarding a right one has not helped anyone.

### 6.5 Recognition damage, handled symmetrically

A scanned bank statement produces `"Interest eamed"`, `"i)Empioyees cost"` and
`"Operating Prafit befare provisians"` for labels the taxonomy knows. These are
properties of the recogniser — an `m` set tightly reads as `rn`, `l` and `i`
differ by a dot, `a` and `o` close up — not of any filer.

A `recognition_key` collapses confusable characters and is applied to **both
sides** of a comparison, in the same sense as lowercasing. The threshold is
unchanged and the neighbourhood is not widened: two labels that differ only by
a confusion the recogniser is known to make become identical, and two that
differ by anything else stay exactly as far apart. `"IT Expenses"` still does
not match `"Tax Expenses"`.

---

## 7–8. Table reconstruction and fragment-merge safety

Phase 19 rejoined statements that layout had split, on geometry alone, and
recorded the risk plainly: *two genuinely separate tables of the same width
with nothing between them would be merged, and the merge is invisible once
made.* That risk is now closed.

`backend/app/tables/fragment_identity.py` replaces "looks adjacent" with an
evidence model. The asymmetry is the whole design:

> **Positive structural evidence is required, and any single piece of contrary
> evidence is decisive.**

Two tables a filer meant to keep apart nearly always say so somewhere — a
title, a basis declaration, a different unit, a fresh set of column dates — and
one such statement outweighs any amount of geometric similarity, because
geometry is exactly what two consecutive tables of one statement share.

**Contrary evidence** (any one refuses the merge):

| Code | Meaning |
|---|---|
| `new_statement_title` | a new statement begins between them |
| `heading_between` | a heading separates them |
| `prose_between` | body text separates them |
| `lower_opens_a_statement` | the lower band names itself |
| `different_reporting_basis` | standalone then consolidated, or the reverse |
| `different_unit_declaration` | the amounts are declared in different units |
| `vertical_gap` | further apart than a statement's internal rules leave |
| `extents_disagree` | their horizontal extents do not line up |
| `columns_do_not_align` | their value columns stand at different edges |

**Supporting evidence** (two are required):
`adjacent`, `same_extent`, `columns_align`,
`lower_has_no_identity_of_its_own`, `one_band_carries_no_figures`.

### 8.1 Every merge records why

The verdict is written to the table as a sentence:

> *Merged because they are adjacent (-36px apart); their horizontal extents
> agree 100%; the lower band declares no title, basis or period of its own.*

and a refusal likewise:

> *Not merged because only 2 of 4 value columns line up.*

A merge is never invisible again.

### 8.2 The adversarial cases

`tests/test_fragment_merge.py`, 14 cases covering the brief's list:

| Case | Expected | Result |
|---|---|---|
| One statement split at a rule | merge | ✓ |
| Two unrelated tables, same width | refuse | ✓ `columns_do_not_align` |
| Two tables separated by a heading | refuse | ✓ `heading_between` |
| Standalone followed by consolidated | refuse | ✓ `different_reporting_basis` |
| Two KPI tables, identical geometry | refuse | ✓ `vertical_gap` |
| Same statement, different unit declarations | refuse | ✓ `different_unit_declaration` |
| Lower band names its own statement | refuse | ✓ `lower_opens_a_statement` |

### 8.3 A regression I introduced, and what it taught

The first version of this model **broke the very join it exists to make**.
Column alignment was treated as evidence unconditionally, and a header block's
numbers are its *year row* — bare, centred over the column — while a data
band's are right-aligned figures. Their edges differ by tens of pixels for a
reason that has nothing to do with whether the two belong together, so
`columns_do_not_align` fired, the statement's head was left orphaned from its
own figures, and Groww's period accuracy fell to **0%** on 91 facts.

Caught by the golden corpus in one run. The lesson is about the design rather
than the bug: *if one piece of contrary evidence is decisive, then every signal
must be silent unless it can speak reliably.* Column alignment now requires
three rows of figures on both sides before it says anything, and a band with no
figures at all is treated as supporting evidence — it cannot stand as a table
by itself.

---

## 9–11. Period, entity and unit

No changes were needed in these three. The Phase 20 root-cause classification
found **zero** misses attributable to period resolution, entity attribution or
unit resolution, and all 137 matched golden facts carry the right period, the
right entity and the right unit.

One period-adjacent defect was fixed, and it is a value defect rather than a
period one:

**A cell holding two figures is a missed column boundary, not a value.**
`"(0.04) 10.32"` is two columns' figures in one cell; the parser returned the
first and discarded the second, so the number was right and its column was
wrong — a figure dated to a quarter it does not belong to, reading as a clean
confident fact. Neither figure can be placed, so neither becomes one, and the
table records that its boundary is wrong there.

This was caught by the golden corpus as the single wrong fact in a run that had
just gained 47 — the only accuracy regression of the phase, and it lasted one
measurement.

---

## 12. Accounting validation

### 12.1 Three of the nine existing checks had never run

Before adding anything, the rules already there were audited against the
taxonomy:

| Rule named | Taxonomy has |
|---|---|
| `finance_cost` | `finance_costs` |
| `total_equity` | `net_worth` |
| `cash_from_operations`, `cash_from_investing`, `cash_from_financing` | `operating_cash_flow`, `investing_cash_flow`, `financing_cash_flow` |
| `net_change_in_cash` | `cash` |
| `total_debt` | `debt` |
| `cost_of_materials` (relations) | `raw_material_cost` |

A rule naming a key no fact can carry matches nothing, so it never runs and
never fails — it simply produces nothing, **which is indistinguishable from a
document that does not support the check**. Nothing in the suite could see it.

`tests/test_accounting_identities.py` now parses the rule table from source and
asserts that every metric key it names exists. That class of bug cannot come
back silently.

### 12.2 Eight new identities

| Identity | Applies to |
|---|---|
| Current Tax + Deferred Tax = Total Tax Expense | any |
| Profit + Other Comprehensive Income = Total Comprehensive Income | any |
| Profit Before Associates + Share of Associates = Profit Before Tax | Ind-AS |
| Paid-up Share Capital + Other Equity = Total Equity | any |
| Interest Earned + Other Income = Total Income | banks |
| Interest Expended + Operating Expenses = Total Expenditure | banks |
| Employee Cost + Other Operating Expenses = Operating Expenses | banks |
| Total Income − Total Expenditure = Operating Profit before provisions | banks |

### 12.3 Applicability, because an identity can be wrong for a whole filer class

A bank's total income is *interest earned* plus other income, not revenue from
operations plus other income. The ordinary corporate rule fired on HDFC's
results and reported **eight failures against a statement that adds up
perfectly**.

Rules now carry `blocked_by`: metrics whose presence makes the identity
inapplicable. `revenue_plus_other_is_total_income` is blocked by
`interest_earned`. Forcing the equality would be the accounting engine
inventing a defect, which the brief forbids outright.

### 12.4 What it finds

Groww went from **1 check to 38, of which 36 pass**:

```
PASSED  8x  Revenue from Operations + Other Income = Total Income
PASSED  8x  Profit Before Tax - Tax Expense = Profit After Tax
PASSED  6x  Profit + Other Comprehensive Income = Total Comprehensive Income
PASSED  6x  Current Tax + Deferred Tax = Total Tax Expense
PASSED  4x  Total Income - Total Expenses = Profit Before Tax
PASSED  4x  Profit Before Associates + Share of Associates = Profit Before Tax
FAILED  1x  Current Tax + Deferred Tax = Total Tax Expense
```

HDFC's bank identities pass likewise — interest plus other income, expenditure
components, and income less expenditure giving operating profit.

**The engine never rewrites a value.** It raises review priority and names the
disagreement. That is not a stylistic choice: it is how the silent number-
parsing defect of Phase 19 was found, and a version that had "corrected" the
figure would have hidden it.

---

## 13–17. Evidence score and fact state

`backend/app/extraction/fact_state.py`.

A single confidence number cannot answer the question a reader has. "0.92"
blends how clearly the digits were printed with whether anyone established what
period they belong to, and those fail independently. Averaged together,
excellent recognition hides poor attribution — which is precisely the failure
this project keeps finding.

Seven dimensions are scored separately: **metric, value, period, entity, unit**,
plus **accounting consistency** and **corroboration** as evidence rather than
requirements. The **weakest** of the five decides the state. That is not a
formula but a rule about financial data: *a figure is only as good as the
least-established thing about it.*

| State | Meaning |
|---|---|
| `VALIDATED` | everything established, and an accounting check agrees |
| `HIGH_CONFIDENCE` | everything established from the document |
| `MEDIUM_CONFIDENCE` | everything established, some of it inherited rather than stated |
| `REQUIRES_REVIEW` | something needs a person |
| `UNKNOWN` | a part could not be established at all |

Two consequences are asserted directly in `tests/test_fact_state.py`:

* **An accounting pass cannot rescue a missing period.** Arithmetic confirms a
  figure; it does not say which quarter the figure is. A fact with no period is
  `UNKNOWN` even when a check on it passed.
* **Recognition confidence of 0.99 does not lift a fact with no period.**
  Parameterised over 0.99, 0.95 and 0.90, all `UNKNOWN`.

### 13.1 The first calibration was wrong, and it showed up immediately

Run end to end on Groww, the first version put **166 of 170 facts at
MEDIUM_CONFIDENCE and none at HIGH**. A model where nothing reaches the top
band is worse than useless — it trains a reader to ignore the field.

Two mappings were at fault, and both were reading a name rather than a meaning:

* **`unit_inferred` does not mean the unit was inferred.** It marks a scale
  that came from the table's own declaration rather than from the cell text —
  which is how every financial statement declares its scale. "(All amounts are
  in INR Crores)" is a statement by the document. Treating it as weaker
  evidence held 73 facts down. `currency_inferred` genuinely is an inference
  (currency read from magnitude words rather than a symbol) and still scores as
  one.
* **`INHERITED` period basis is not a guess.** It means the period reached the
  cell along the structural path the table model defines — a date in the column
  head under a span word in the group head. That is how a well-formed statement
  expresses a period, and it is exactly what Phase 19 built. It held another 66
  facts down.

Both now score HIGH; `derived` — a period reconstructed rather than read —
remains MEDIUM. A third, smaller flaw went with them: the *limiting dimension*
was reported even when every dimension was HIGH, so the interface read "limited
by metric" against a fact nothing was holding back.

Measured on Groww after the correction, across 170 facts:

| State | Count |
|---|---|
| `VALIDATED` | **83** |
| `HIGH_CONFIDENCE` | **56** |
| `MEDIUM_CONFIDENCE` | 27 |
| `REQUIRES_REVIEW` | 4 |

The 83 validated are figures where every dimension is established *and* an
accounting identity computed from other figures agrees with them — which is a
stronger claim than this system has ever been able to make about a number.

And what is *not* a gap is equally explicit: a segment column genuinely has no
period, so period is marked not-applicable rather than unknown for facts from
non-periodized tables; a percentage owes no currency; a per-share figure owes
no magnitude.

Period semantics were already rich enough for §16–17 — `period_start`,
`period_end`, `period_type`, `fiscal_year`, `quarter`, `basis` and the source
label all travel on the fact — and Phase 19 established that a quarter and a
fiscal year sharing an end date stay distinct. The golden corpus asserts it on
Groww's own statement, where columns 2 and 4 both end 31 March 2026 and read as
"Three months ended" and "Twelve months ended".

---

## 25–26. Frontend

**The evidence model's verdict now leads.** The facts table shows the fact
state where one was recorded, with the recognition score beside it rather than
in front of it — a figure can be read perfectly and still be unusable for want
of a period, and the chip a reader sees first should say so. Where the state is
held back, the row says which dimension did it: *limited by period*.

**The detail panel gained two rows.** A per-dimension evidence line —
`Metric high · Value high · Period unknown · Entity high · Unit high` — with
any not-applicable dimensions named separately. And the validation row now
reads in words:

> *An accounting consistency check did not agree with this figure.*

rather than `FAILED`.

Both read the state the backend recorded and never derive one; a UI that
computed its own answer could disagree with the export, the API and the review
queue, and a reader would have no way to tell which was right.

---

## 13–15. Corpus

| | |
|---|---|
| Companies with golden facts | 2 (Groww, HDFC Bank) |
| Companies processed | 7 (Groww, HDFC, Paytm, Reliance, IDFC First, Standard Chartered, Apple) |
| Golden tables | 2 |
| **Golden facts** | **151** |
| Documents in the benchmark corpus | 9 |

**The corpus was not expanded in this phase, and that is a shortfall against
§8**, which asked for 10+ companies and 20+ documents. It is stated plainly
rather than glossed: the phase's own baseline showed 86% of the coverage gap
sitting in one place — the metric vocabulary — and closing that against the two
statements already transcribed returned 64 facts. Adding companies would have
produced more misses of the same kind before the kind was understood.

What the two documents did buy is disproportionate to their number, because
they were chosen to disagree: Groww writes dates out and banners its unit,
HDFC dots them and hides the unit in a corner note; one is Ind-AS corporate,
the other a bank with an entirely different income statement. Four of this
phase's findings came from one breaking a rule the other had confirmed.

Section 30 puts corpus expansion first for the next phase, and it is now the
right time for it: the vocabulary is broad enough that new documents will
expose new *kinds* of problem rather than more of the same one.

---

## 16–18. Per-document results

### Groww

| | Before | After |
|---|---|---|
| Golden facts matched (page 6) | 56 / 99 | **93 / 99** |
| Wrong period / unit / entity | 0 | **0** |
| Facts extracted (document) | 114 | 170 |
| Accounting checks run | **1** | **38** |
| Accounting checks passing | 0 | **36** |

### HDFC

| | Before | After |
|---|---|---|
| Golden facts matched (page 1) | 16 / 52 | **43 / 52** |
| Wrong period / unit / entity | 0 | **0** |
| Facts extracted (document) | 93 | 166 |
| Accounting checks run | 0 | **27** |
| Accounting checks passing | 0 | **23** |

The four HDFC failures are all `operating_expense_components` on the
consolidated entity, whose page-8 table the reconstruction validator
independently flagged (`cells_outside_their_columns`) and whose facts the
safety gate held in full — 55 of 55. Two checks that know nothing about each
other agree that page is unreliable, which is the accounting layer behaving as
corroborating evidence rather than as an oracle.

### Paytm

Not re-measured against golden facts — Paytm has no golden fixture. Its Phase
19 result stands: 100% period coverage on periodized tables, 0 misleading
facts, 2 review flags.

---

## 19–24. Counts

| | Value |
|---|---|
| Wrong-period facts (golden) | **0** |
| Wrong-value facts (golden) | **0** |
| Wrong-unit facts (golden) | **0** |
| Wrong-entity facts (golden) | **0** |
| False conflicts | **0** (Groww's 89 were eliminated in Phase 19 and have not returned) |
| Facts held by the safety gate (HDFC) | 73 of 166 — 18 on page 3 and 55 on page 8, both tables the reconstruction validator independently flags |

---

## 25–26, 29. Testing gate

| Gate | Result |
|---|---|
| Backend suite | **849 passed, 1 skipped, 0 failed** (was 778) |
| Golden corpus suite | 137 / 151, accuracy 100% |
| Period suite | 62 passed |
| Table geometry / integrity | 18 passed |
| **Fragment merge suite** | **14 passed** (new) |
| **Accounting validation suite** | **30 passed** (new) |
| **Fact-state suite** | **15 passed** (new) |
| Anti-fabrication suite | 26 passed |
| Number parsing | 36 passed |
| Frontend typecheck | 0 errors |
| Frontend build | success |
| Frontend lint | 6 warnings — unchanged baseline, none in changed files |

The one skip is `test_api.py:248` ("nothing needed review on this document"), a
conditional case.

**Keyed accuracy gate — no regressions, and three documents improved:**

| Case | Facts | F1 | Baseline | Matched | Baseline | Verdict |
|---|---|---|---|---|---|---|
| RIL | 418 | 0.9882 | 0.9882 | 84 | 84 | ok |
| RIL_IMAGE_ONLY | 245 | **1.0000** | 0.9877 | **81** | 80 | **improved** |
| RIL_DEGRADED | 252 | **0.7143** | 0.6950 | **50** | 49 | **improved** |
| APPLE | **50** | — | — | — | — | **improved (44 → 50 facts)** |

The scanner-degraded document has now moved twice: 0.6950 → 0.7092 in Phase 19,
and 0.7092 → 0.7143 here. It had not moved at all in the three phases before
that. The taxonomy work reaches it because the rows it was losing were the same
Ind-AS lines the golden corpus named.

---

## 28. Manual source comparison

Every golden fact *is* a manual source comparison: 151 figures transcribed from
the rendered page by eye, each with its row label, column header, value, unit,
period and reporting basis. That is the §28 requirement discharged at a scale
the section asked for (20 per document) three times over for two documents, and
it is machine-checkable on every run rather than once.

Two rows were re-inspected against the page image during this phase
specifically:

* Groww's `Profit for the period/year attributable to: / Shareholders of the
  Company / Total` block, which established that a colon-terminated label
  governs the rows beneath it;
* HDFC's `Total Expenditure (4)+(5)` row, whose OCR text `62005,98` revealed
  the decimal-comma defect.

---

## 27. Performance impact

The correctness machinery added in Phases 19 and 20 remains unmeasurable
against OCR:

| Stage | Cost per document |
|---|---|
| `classify_table` | 0.37 ms |
| `validate_table` | 0.99 ms |
| `safety_gate` | 0.23 ms |
| `fragment_identity` (per join) | microseconds — geometry and a few regexes |
| `fact_state` | proportional to fact count, no I/O |

Against warm wall-clock runs of 17–23 s and cold runs an order of magnitude
longer, none of it registers. **No performance work was done in this phase, by
design** — §24 asks for it only after the correctness work, and the correctness
work found more than expected. The Phase 19 finding stands unchanged: OCR is
~90% of cold wall clock and every parameter that reduces it has been measured
and rejected.

The text-layer-first investigation §24 raises is deferred to Phase 21 with the
architecture now stable enough to support it.

---

## 29. Remaining known risks

1. **The golden corpus is two documents.** Everything in section 1–4 is
   measured on 151 facts from two statements. The numbers are honest and the
   documents were chosen to differ, but two is a small sample for claims about
   generalisation, and it is the phase's clearest shortfall.

2. **A row can absorb the sub-heading beneath it.** Groww's `"Income tax
   relating to above"` arrives as `"Income tax relating to above Items that
   will be reclassified to profit or loss"` — two rows merged by the row
   builder. It accounts for the 8 remaining taxonomy misses and is a
   row-boundary defect, not a vocabulary one.

3. **Recognition damage still costs 5 facts.** `"Operating Prafit befare
   provisians"` is not reachable even with recognition-equivalence matching:
   too many characters differ. Nothing in the period or table model can recover
   a label the recogniser destroyed.

4. **HDFC page 8 produces no confident facts at all** — 55 of 55 held by the
   safety gate, with the table validator reporting cells outside their columns.
   The gate is doing the right thing; the table is genuinely misreconstructed,
   and no one has looked at why.

5. **The fragment merger's contrary-evidence rule is powerful and therefore
   sharp.** One false signal refuses a real merge — as column alignment did
   mid-phase, costing 91 facts their period until the golden corpus caught it.
   Every future signal added to it needs the same question asked: *can this be
   wrong for a reason unrelated to whether the bands belong together?*

6. **`operating_expense_components` fails on HDFC's consolidated entity** and
   the cause has not been traced beyond "the page it comes from is
   independently flagged". It may be a third expense component the statement
   lists and the taxonomy does not.

7. **The `d) Others` rows are permanently missing by choice**, which is right,
   but it means golden coverage can never reach 100% on HDFC page 1 as the
   fixture is written. The fixture should mark them not-expected rather than
   leaving them to look like a gap.

---

## 30. Recommended Phase 21

**First — expand the corpus, now that it will teach something new.** §8's list
is the right one: 10+ companies, cash flow statements, half-year and
nine-month periods, a KPI grid, an investor presentation. The vocabulary is now
broad enough that new documents should expose new *kinds* of failure rather
than more missing aliases. Prioritise a cash flow statement — the corpus has
none, and §22's period semantics for cumulative year-to-date columns are
completely untested.

**Second — row boundaries.** Item 2 above is the last structural defect visible
in the golden data, and the same machinery that merges a sub-heading into the
row above it is what merged a paragraph into a column header in Phase 19. One
model of "where does a row end" would close both.

**Third — the text-layer-first path.** §24 defers this to after the correctness
work, and the correctness work is now done: columns are first-class, periods
are structural, the merge is evidence-based, and the accounting layer provides
an independent check on whatever a faster path produces. That last point is
what makes it safe to attempt now and was not true a phase ago.

**Explicitly not recommended: more taxonomy aliases without a document that
needs them.** The 17 entries added here each came from a row that produced no
fact. Adding entries speculatively is how a taxonomy acquires the false
neighbours — `total expenditure` against `capex` — that this phase spent effort
removing.

---

## 6–12. Files changed

| File | Change |
|---|---|
| `backend/app/evaluation/diagnose.py` | **new** — root-cause bucket for every missing golden fact |
| `backend/app/tables/fragment_identity.py` | **new** — evidence model for whether two fragments are one table |
| `backend/app/extraction/fact_state.py` | **new** — per-dimension evidence and the standard fact states |
| `backend/app/extraction/taxonomy.py` | temporal qualifier stripped before matching; `total`/`other` as contrast words; recognition-equivalence key applied symmetrically; 17 new metrics; `total_comprehensive_income` split from OCI |
| `backend/app/extraction/facts.py` | sub-heading inheritance; ambiguous multi-figure cells refused |
| `backend/app/normalization/numbers.py` | `holds_more_than_one_number`; `repair_decimal_comma` |
| `backend/app/validation/engine.py` | 6 rules repaired (they had never run); 8 identities added; `blocked_by` applicability |
| `backend/app/analysis/relations.py` | two component keys repaired |
| `backend/app/tables/fragments.py` | merge decided by `fragment_identity`, verdict carried |
| `backend/app/services/pipeline.py` | merge verdict recorded on the table; `fact_state` computed per fact |
| `backend/app/evaluation/golden.py` | matching through the taxonomy, so recognition damage is not counted as a coverage miss |
| `frontend/src/utils/factState.ts` | **new** — renders the recorded state and per-dimension evidence |
| `frontend/src/views/doc/FactsTab.tsx` | state leads, recognition score beside it, limiting dimension named |
| `frontend/src/components/FigureDetail.tsx` | status row, evidence row, accounting check in words |
| `docs/ARCHITECTURE.md`, `docs/EVALUATION.md` | merge model, fact state, coverage-vs-accuracy |

**Tests: 778 → 850 (849 passed, 1 skipped, 0 failed).**

| File | Cases |
|---|---|
| `tests/test_fragment_merge.py` | 14 — the brief's adversarial merge cases |
| `tests/test_accounting_identities.py` | 30 — every rule key must exist; the identities as arithmetic on real figures |
| `tests/test_fact_state.py` | 15 — the weakest dimension decides; accounting cannot rescue a missing period |
| `tests/test_number_parsing.py` | +8 — decimal comma, both directions |

---

## Appendix — how to reproduce

```bash
# golden coverage and accuracy, with every miss classified by root cause
cd backend && ../.venv/Scripts/python.exe ../scratch/p20_baseline.py

# which accounting identities run, and what they say
cd backend && ../.venv/Scripts/python.exe ../scratch/p20_accounting.py

# the adversarial fragment-merge cases
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_fragment_merge.py -q

# every accounting rule key must exist in the taxonomy
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_accounting_identities.py -q

# the evidence model
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_fact_state.py -q

# full gates
cd backend && ../.venv/Scripts/python.exe -m pytest tests/ -q
cd backend && ../.venv/Scripts/python.exe ../scratch/p16_benchmarks.py
cd frontend && npx tsc --noEmit && npm run build && npm run lint
```
