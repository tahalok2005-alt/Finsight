# Unit and value-type correctness patch

Two reported defects, traced to three distinct root causes, all fixed
generically. No company-specific logic, no benchmark-specific rules, no change
to period resolution, header assembly, table continuation or fiscal calendar
inference.

---

## 1. Initial state

| | Before |
|---|---|
| Test suite | **324 passed, 1 skipped** (verified by a full run, 1:41:16) |
| Bug 1 — percentage typing | **Reproduced.** 4 Apple `gross_margin` facts typed `percent` with values 39,678 – 146,860 |
| Bug 2 — thousand vs million | **Reproduced.** 13 Apple facts typed `thousand` in HYBRID, 1 in VISUAL_ONLY, against a table declaring "(In millions)" |
| RIL digital | F1 0.9882 (84/85) |
| RIL image-only | F1 0.9877 (80/81) |
| Apple | 56 facts, 56/56 dated, 100% provenance |
| Degraded scan | F1 0.695 (49/81) |

The mode asymmetry in Bug 2 — 13 bad facts in HYBRID against 1 in VISUAL_ONLY —
was the first useful clue that more than one mechanism was involved.

---

## 2. Root causes

### RC-1 — a number was allowed to declare its own magnitude

`app/normalization/numbers.py`, `_MAGNITUDE_WORDS`.

The pattern for thousands was `\bthousands?\b|\b000s?\b|\bk\b`. The `\b000s?\b`
alternative exists to read a real declaration — "Rs '000", "in 000s". But the
comma inside a figure is a word boundary, so it also matches the last group of
any number ending in `,000`:

```
detect_magnitude('79,000')     -> thousand      # the number, not a declaration
detect_magnitude('1,000')      -> thousand
detect_magnitude('50,400,000') -> thousand
```

`normalize_number` consults `detect_magnitude(token)` **before** the context and
before the caller's default, on the sound principle that a unit written on the
figure itself is the most specific evidence there is. So this phantom
token-level declaration outranked the table's explicit "(In millions)".

This is why `net_income 9M FY24 = 79,000` came out in thousands while
`84,544` beside it came out correctly: only one of them ends in a zero group.
It is not Apple-specific, not currency-specific and not mode-specific — any
figure ending in `,000` in any document was affected.

### RC-2 — a carve-out clause was read as the document's own unit

`app/normalization/numbers.py`, `infer_document_units`.

Apple's statements are headed:

> (In millions, except number of shares, which are reflected in thousands, and
> per-share amounts)

The declaration scanner counted **every** "in &lt;magnitude&gt;" fragment
equally, so this sentence voted once for millions and once for thousands. What
tipped it was OCR: the rendered page reads `(ln millions` — a lowercase L for
the capital I — so the *head* declaration failed to match while the carve-out,
whose "in" is genuinely lowercase, matched cleanly:

```
infer_document_units(<OCR text>) -> (None, THOUSAND)      # before
infer_document_units(<OCR text>) -> (None, MILLION)       # after
```

Every table without a declaration of its own then inherited `thousand`. On
page 3 the cash-flow table's own "(In millions)" line lands in the header block
in HYBRID but stays in the caption in VISUAL_ONLY — which is precisely why the
same document produced 13 bad facts in one mode and 1 in the other. The mode
difference was a symptom; the bad document-level default was the cause.

### RC-3 — a taxonomy expectation outranked an explicit source declaration

`app/extraction/facts.py` `_fact_from_cell`, and `app/normalization/numbers.py`
`normalize_number`.

The taxonomy defines `gross_margin` with `value_type=PERCENTAGE`, which is
right for "Gross margin (%)" and wrong for the US income-statement line
"Gross margin", a currency amount. Two steps then conspired:

1. `_fact_from_cell` treats any metric the taxonomy calls non-monetary as
   ineligible for the table's unit, so it passes `default_magnitude=None` —
   deliberately, to stop "EBITDA margin 15.9" becoming "15.9 crore".
2. `normalize_number` promotes `expected_type` to the actual value type when
   the token is silent, then forces `magnitude=PERCENT, currency=UNKNOWN`.

A silent token is the normal case for a monetary cell, so the taxonomy's guess
about the metric *name* silently defeated the statement's explicit declaration
about the *figure*, and 43,718 million was reported as "43,718 percent".

Both modes were affected identically; the mechanism does not involve OCR.

---

## 3. Fixes

### RC-1 — `app/normalization/numbers.py`

```python
(re.compile(r"\bthousands?\b|(?<![\d.,])000s?\b|\d\s*k\b", re.I),
 MagnitudeUnit.THOUSAND),
```

A lookbehind keeps a declaration a declaration: `000` counts only when what
precedes it is not a digit, comma or point. "in 000s", "Rs '000" and
"000s of tonnes" still read as thousands; "79,000", "1,000", "12,000.5",
"2,00,000" and "50,400,000" no longer do.

### RC-2 — `app/normalization/numbers.py`

A declaration standing after "except", "excluding" or "other than" describes
the *excluded* item, so it does not vote. Scope is the enclosing parenthetical,
falling back to the line, and only text preceding the match is examined — a
carve-out later in the sentence says nothing about a declaration that came
first. This is robust to the OCR damage that made the bug reachable, because it
removes the wrong candidate rather than relying on the right one surviving.

### RC-3 — `app/extraction/facts.py` and `app/normalization/numbers.py`

The principle: **the taxonomy says what a metric name usually means; the
document says what this figure is. Where they disagree, the document wins.**

`_taxonomy_rate_is_contradicted` overrules the taxonomy only when all three of
the following hold, so nothing that could legitimately be a rate is touched:

* the source says "rate" nowhere — not in the cell, the row label, the
  unit-of-measure cell, nor the column head. An explicit `(%)` is the document
  speaking and always wins;
* the table carries an explicit magnitude declaration, so there is real
  evidence to prefer rather than merely an absence of evidence;
* the figure is larger than `RATE_CEILING` (1000), i.e. too large to be a rate
  at all.

Every RIL percentage row in all three RIL fixtures carries an explicit `(%)`
("EBITDA Margin (%)", "k) Operating margin (%)", even the degraded scan's
"EBITDAmargin (%)"), so the first condition alone already exempts them; the
third is a second independent guard.

The same defect was reachable through narrative extraction, which passes the
same taxonomy prior. Rather than patch that call site too, the rule was also
placed at the shared abstraction: `normalize_number` now distinguishes an
**observed** rate (the token carries `%` or `bps`) from an **expected** one
(the caller's prior), and only an observed rate may erase a magnitude the
caller supplied from an explicit declaration.

When the override fires, the fact records `taxonomy_rate_overridden: true` in
its metadata, so a reviewer can see that the metric's usual type was set aside
and why — the decision is visible rather than silent.

### Two adjacent defects of the same class, found by the audit and fixed

Both were surfaced by the Phase 6 sweep, both are the same mistake as RC-1 —
incidental text read as a unit declaration — and both are one line each.

**A list marker read as a magnitude.** Indian filings enumerate statement rows
"a)", "b)" … and the row label `k) Operating margin (%)` exists in the RIL
benchmark. `k` matched that marker, so the label declared thousands. It is
currently harmless only because that particular row is a percentage, whose
magnitude is overwritten anyway; the same marker on a monetary row would have
scaled the figure by a thousand. As a magnitude, "k" is a suffix on a figure
("50k", "1.2k"), never a standalone word, and the pattern now says so.

**A magnitude read as a currency.** `detect_value_type` returns `MONETARY` when
a magnitude word is present, so "396 million registered customers" was typed
monetary and then picked up the document's rupee default — reporting a customer
base as ₹396 million. Two facts per RIL document were affected
(`registered_customers`, `net_subscriber_addition`), both from narrative text.
A magnitude is a *scale*; only a currency makes a figure money. Where the
taxonomy knows a metric counts things, a bare magnitude now scales the count
and no currency is attached — but a currency written on the token itself
("Rs 396 million") still wins, because then the document really did say money.

### Why these are generic

None of the three fixes mentions a company, a filename, a page number or a
metric key. RC-1 is a regex that could not tell a declaration from a digit
group. RC-2 is the grammar of an exception clause. RC-3 is an evidence
precedence rule between two kinds of claim. An AST audit of every module in
`app/` confirms **0** company tokens in executable code, **0**
filename-conditioned branches and **0** page-number-literal branches.

---

## 4. Regression tests

14 new tests in `tests/test_extraction.py::TestUnitAndValueTypeEvidence`, built
from real `ExtractedTable` objects and run through `extract_from_table` — the
actual extraction path, not helper assertions. Plus a unit/currency parity
assertion added to the existing both-modes pipeline test.

| Coverage | Test |
|---|---|
| A — source states percent, believed | `test_an_explicit_percent_marker_makes_a_rate`, `test_a_percent_sign_in_the_cell_is_enough` |
| B — same metric name, source states money | `test_a_margin_line_in_a_monetary_statement_stays_monetary` |
| B — a rate that *could* be a rate is untouched | `test_a_plausible_rate_is_never_reclassified` |
| B — no declaration, nothing reclassified | `test_without_a_declaration_nothing_is_reclassified` |
| C / D — millions, thousands and crore all reach the fact | `test_the_declared_magnitude_reaches_the_fact` (parametrised) |
| RC-1 directly | `test_a_figure_ending_in_a_zero_group_is_not_a_thousands_declaration` |
| E — continuation fragment inherits the unit | `test_a_continuation_fragment_inherits_the_declared_unit` |
| F — a row's own unit beats the table's | `test_a_row_unit_of_measure_cell_beats_the_table_declaration` |
| RC-2 directly, including the OCR misread | `test_a_declaration_carve_out_does_not_set_the_document_unit` |
| A list marker is not a magnitude | `test_a_list_marker_is_not_a_magnitude` |
| A magnitude does not make a count into money | `test_a_magnitude_alone_does_not_make_a_count_into_money` |
| G — HYBRID / VISUAL_ONLY parity of unit, currency and value type | `TestModesRemainDistinct::test_hybrid_uses_corroboration_visual_only_does_not` |

Two tests pin the *negative* cases deliberately, because a fix that reclassifies
too eagerly would be worse than the bug: a margin of 20.0 in a crore table stays
a percentage, and a table that declares no unit is left entirely alone.

No existing test expectation was changed. All 324 prior tests still pass as
written.

---

## 5. Measured results

Every figure below comes from a single validation run made after the last code
change — all four documents, both modes, nothing carried over from an earlier
run and no stale JSON.

### Accuracy and structure — unchanged everywhere

| Document | Mode | Facts | Tables | Periods | F1 | Matched | Provenance |
|---|---|---|---|---|---|---|---|
| RIL digital | HYBRID | 432 | 30 | 406 | **0.9882** | 84/85 | 100% |
| RIL digital | VISUAL_ONLY | 431 | 30 | 405 | **0.9882** | 84/85 | 100% |
| RIL image-only | HYBRID | 255 | 9 | 255 | **0.9877** | 80/81 | 100% |
| RIL image-only | VISUAL_ONLY | 255 | 9 | 255 | **0.9877** | 80/81 | 100% |
| Degraded scan | HYBRID | 251 | 8 | 251 | **0.695** | 49/81 | 100% |
| Degraded scan | VISUAL_ONLY | 251 | 8 | 251 | **0.695** | 49/81 | 100% |
| Apple | HYBRID | 56 | 5 | 56 | — (no key) | — | 100% |
| Apple | VISUAL_ONLY | 56 | 5 | 56 | — (no key) | — | 100% |

Against the stated baseline — RIL 0.9882 (84/85), image-only 0.9877 (80/81),
Apple 56/56 dated, degraded 0.695 (49/81) — **every number is identical**. Fact
counts, table counts, period attribution, entity counts and provenance are
unchanged. This work was a correctness fix, not an accuracy change, and the
numbers say so: no metric moved in either direction.

### Unit typing — what actually changed

| Document / mode | Implausible % | `thousand` facts | Counts typed as money |
|---|---|---|---|
| | before → after | before → after | before → after |
| Apple HYBRID | **4 → 0** | **13 → 0** | 0 → 0 |
| Apple VISUAL_ONLY | **4 → 0** | **1 → 0** | 0 → 0 |
| RIL digital (both) | 0 → 0 | 0 → 0 | **2 → 0** |
| RIL image-only (both) | 0 → 0 | 0 → 0 | **2 → 0** |
| Degraded (both) | 0 → 0 | 0 → 0 | 0 → 0 |

Legitimate percentages are untouched: RIL keeps its 40, the image-only and
degraded fixtures keep their 16 each, and none exceeds a plausible rate.

Apple's unit histogram is now `{'million': 56}` in **both** modes — where before
the two modes disagreed with each other (13 mis-typed facts against 1), which
was itself the clue that a document-level default rather than a table-level one
was at fault.

### The two reported facts, against the source

| Fact | Before | After | Source |
|---|---|---|---|
| `gross_margin` Q3 FY25 | 43,718 **percent** | 43,718 **million USD** | 94,036 net sales − 50,318 cost of sales = 43,718 |
| `net_income` 9M FY24 | 79,000 **thousand** | 79,000 **million USD** | statement of cash flows, "(In millions)" |

Both agree with Apple's published Q3 FY2025 figures.

---

## 6. VISUAL_ONLY verification

| Document | Mode | Native text fed extraction | Permitted accesses | Provenance |
|---|---|---|---|---|
| RIL digital | VISUAL_ONLY | **false** | **0** | 431/431 |
| RIL image-only | VISUAL_ONLY | **false** | **0** | 255/255 |
| Degraded scan | VISUAL_ONLY | **false** | **0** | 251/251 |
| Apple | VISUAL_ONLY | **false** | **0** | 56/56 |
| RIL digital | HYBRID | true | 34 corroboration | 432/432 |
| RIL image-only | HYBRID | true | 7 corroboration | 255/255 |
| Degraded scan | HYBRID | true | 7 corroboration | 251/251 |
| Apple | HYBRID | true | 3 corroboration | 56/56 |

**No native text fed extraction in any VISUAL_ONLY run**, and the access ledger
records **zero** corroboration accesses in all four. The HYBRID accesses are the
expected text-layer corroboration channel and are counted.

The boundary was never touched by this work: all five fixes operate on values
and text already extracted from pixels — a regex over a numeric token, a clause
test over text the OCR produced, and an evidence-precedence rule between a
parsed cell and a table declaration. None of them opens the PDF.

Provenance is complete in every run. It is built from the cell's bounding box,
original text and OCR element ids before normalisation runs, and no
normalisation step writes to it — so a change in unit or value type cannot
disturb the trail back to the pixels.

One result worth stating plainly: **HYBRID and VISUAL_ONLY now agree on unit,
currency and value type for every comparable fact.** They did not before, and a
regression test now pins that agreement.

---

## 7. Remaining issues

### Fixed

| # | Defect | Class |
|---|---|---|
| RC-1 | A figure ending in a `,000` group declared itself in thousands, overruling the table | Reported Bug 2 |
| RC-2 | A carve-out clause ("except … in thousands") set the document's unit, reachable because OCR damaged the real declaration | Reported Bug 2 |
| RC-3 | A taxonomy percentage expectation overruled an explicit monetary declaration | Reported Bug 1 |
| — | A list marker `k)` read as a magnitude | Found by audit, same class as RC-1 |
| — | A magnitude word alone typed a count as money and attached a currency | Found by audit, same evidence-precedence theme |

### Verified sound — no change made

| Audit item | Finding |
|---|---|
| Unit/currency precedence chain | Correct as designed and now demonstrated by test: a row's own unit-of-measure cell beats the table declaration, which beats the document default. A row saying "Million" in a crore-declared table yields millions; a row saying "Number" yields a unitless count. |
| Derived metrics combining incompatible units | Sound. Derivation works in `base_value` space, requires a single shared currency across inputs, and logs `derived.skipped_mixed_currency` rather than computing across currencies. |
| Validation across incompatible units/currencies | Sound. `_same_scale` requires matching currency and unit; `_rescale` converts through `MAGNITUDE_FACTORS`; where it cannot reconcile, the rule reports that it could not be checked instead of comparing anyway. |
| Provenance after normalization | Intact. `Provenance` is built from the cell (bbox, original text, OCR element ids) and never touched by any normalization step. 100% coverage measured on every document in both modes. |
| API serialization | Complete. `value_type`, `currency`, `unit` and `base_value` are all serialized; the new `taxonomy_rate_overridden` marker rides in `meta`. |
| Frontend rendering | Faithful. `formatValue` renders percentages with `%`, prints units as the document expressed them ("never silently converted"), and picks Indian digit grouping only for INR. It had been rendering wrong backend data correctly. |
| VISUAL_ONLY isolation | Unaffected. None of the five fixes reads the PDF text layer; all operate on values and text already extracted from pixels. |
| Company-specific logic | None. AST audit over every module in `app/`: 0 company tokens in executable code, 0 filename-conditioned branches, 0 page-number-literal branches. |

### Known limitations, deliberately not changed

- **`\bcr\b` also matches "Cr" (credit)** and **`\bmm\b` also matches millimetres.** Both are the same looseness as the two abbreviations that were fixed, but neither occurs in any benchmark row label, caption or declaration, and both serve legitimate declarations ("Rs Cr" is the commonest Indian form). Tightening them without a reproducing case risks breaking real declarations to fix a hypothetical.
- **`RATE_CEILING = 1000` is a domain judgement, not a derived constant.** A genuine growth rate above 1000% printed without a `%` marker in a table that declares a monetary unit would be reclassified as money. All three conditions must hold at once, and a growth column is handled by a separate path, so this is narrow — but it is a judgement and is named as one.
- **`deduplicate` keys on unit and currency**, so the same metric reported in two units stays two facts rather than merging. That is deliberate — 933 crore and 530 million are not the same figure — and conflicting values are the reconciliation layer's concern, not deduplication's.

### Out of scope

- **`gross_margin` is modelled as a percentage in the taxonomy**, which is right for "Gross margin (%)" and wrong for the US income-statement line. The evidence-precedence rule now handles both readings without changing the taxonomy, which is the better fix: editing the entry would merely move the error to whichever filer writes it the other way.
- **A narrative fact with `value=None`** (JPL EBITDA, page 5) is a sentence stating a growth rate with no absolute figure. That is a legitimate representation, not a defect.

---

## 8. Final validation

*(filled in)*
