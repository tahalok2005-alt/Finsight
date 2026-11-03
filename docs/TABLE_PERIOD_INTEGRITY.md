# Table period integrity — the audit before the rebuild

*Phase 19. Written before any code was changed, from the source document and the
stored evidence.*

---

## 1. The reported failure, reproduced exactly

Groww's unaudited consolidated results, page 6. The source table:

```
                              (All amounts are in INR Crores, unless otherwise stated)
                                    Quarter ended                        Year ended
                          30 June      31 March      30 June      |    31 March
Particulars                  2026          2026         2025      |       2026
                        (Unaudited)    (Audited)    (Audited)     |    (Audited)
                                     (Refer Note 3) (Refer Note 4)|
...
Current tax                  260.40       268.55       133.06     |      770.24
```

What FinSight produced:

| metric | value | period | period_basis | confidence | review |
|---|---|---|---|---|---|
| Current tax | 770.24 | `None` | `not_applicable` | 0.89 **HIGH** | no |
| Current tax | 268.55 | `None` | `not_applicable` | 0.89 **HIGH** | no |
| Current tax | 133.06 | `None` | `not_applicable` | 0.89 **HIGH** | no |
| Current tax | 260.40 | `None` | `not_applicable` | 0.89 **HIGH** | no |

Four figures from four different columns, presented as authoritative, with no
temporal context and no invitation to check. This is the reported bug and it
reproduces exactly.

### 1.1 A second defect found while reproducing: a fabricated period

The orphaned header fragment resolved a period that does not exist in the
document:

```json
{ "label": "As at 2031-06-30",
  "raw_text": "(All amounts are in INR Crores, unless otherwise stated) / Quarterended / 30 June 31 March 30 June",
  "end_date": "2031-06-30", "confidence": 0.75, "basis": "derived" }
```

**2031.** Produced by concatenating three columns' header text into one string
and parsing the result. It did not reach a fact here — the facts have
`period=None` — but it is live in the stored column model, and it is a
fabricated financial attribute, which the standing rules forbid outright. A
missing period is a gap; an invented one is a lie.

---

## 2. Scope — this is systemic, not a Groww quirk

Across the seven corpus documents already processed:

| Document | Tables | Headless tables | Header-only fragments | Fragmented pages |
|---|---|---|---|---|
| GROWW | 22 | 5 | 12 | 5 |
| HDFC | 26 | 4 | 13 | 8 |
| PAYTM | 9 | 0 | 2 | 2 |
| RIL | 30 | 2 | 9 | 7 |
| IDFC | 61 | 15 | 36 | 15 |
| STANCHART | 230 | 26 | 44 | 62 |
| APPLE | 5 | 0 | 0 | 1 |
| **Total** | **383** | **52** | **116** | **100** |

*Headless* = every data column has an empty header. *Header-only fragment* = a
table with four or fewer data cells, i.e. an orphaned header block.

And at the fact level:

| Document | Facts | No period | Labelled `not_applicable` | **`not_applicable` + HIGH + no review** |
|---|---|---|---|---|
| GROWW | 118 | 118 | 89 | **77** |
| HDFC | 70 | 12 | 12 | 0 |
| PAYTM | 25 | 11 | 0 | 0 |
| RIL | 418 | 25 | 25 | 24 |
| IDFC | 107 | 81 | 57 | 26 |
| STANCHART | 524 | 341 | 322 | **133** |
| APPLE | 44 | 0 | 0 | 0 |
| **Total** | **1306** | **588 (45%)** | **505** | **260 (20%)** |

**260 facts — one in five — are shown as HIGH-confidence figures with no period
and no review flag.** That is the size of the problem.

---

## 3. Root cause — a four-link chain

Each link is individually defensible. Together they convert a lost header into a
confident claim.

### Link 1 — layout splits one statement into several table regions

Page 6's single statement became four TABLE regions:

| Region | y range | x range |
|---|---|---|
| `region_c4507e8` | 385–484 | 183–1383 |
| `region_8c095f0` | 450–1042 | 181–1392 |
| `region_afd1a77` | 1009–1521 | 181–1391 |
| `region_96a4558` | 1485–1625 | 181–1395 |

They share a horizontal extent to within 4px and abut (indeed overlap)
vertically. They are visibly one table. The first contains the header block and
almost no data; the rest contain data and no header.

### Link 2 — the table builder trusts the region boundary

`_build_tables` builds one table per TABLE region. Nothing afterwards asks
whether two adjacent tables are really one, so the header block becomes a
4×3 table and the data becomes three headerless tables.

### Link 3 — the orphaned header's own columns are wrong, and it invents a date

Cut off from the data rows, the header fragment has no numeric columns to
align to, so column detection gave it a single band spanning x 869.5→1256.5 —
three data columns wide. Three separate headers collapsed into one string,
`"30 June 31 March 30 June"`, and the period parser turned that into
`As at 2031-06-30`.

### Link 4 — an empty header is read as "this column has no period"

`backend/app/extraction/facts.py`:

```python
unattributed = {
    c.index: (PeriodBasis.UNKNOWN
              if looks_like_period_header(c.header or "")
              else PeriodBasis.NOT_APPLICABLE)
    for c in plain_cols
}
```

The distinction between UNKNOWN and NOT_APPLICABLE is real and worth keeping —
a segment column genuinely has no period, and flagging it would turn every
segment table into a screen of warnings. But it is decided by *reading the
header text*, and an empty header is not a header that says "no period here". It
is a header we lost.

`NOT_APPLICABLE` then means no review flag, and confidence never required a
period, so the fact keeps 0.89 and reaches the user as HIGH.

**The one-line statement of the root cause:** *absence of evidence was treated
as evidence of absence,* at a point where the evidence had been destroyed two
stages earlier.

---

## 4. What the geometry already gets right

This is the encouraging half of the audit, and it shapes the fix.

The **data** tables' column boundaries are correct: 869.5 / 1000.5 / 1131.5 /
1256.5. Every header cell in the orphaned fragment falls cleanly inside one of
them:

| Header cell | x range | Column | Correct? |
|---|---|---|---|
| `30 June` | 903–970 | 869–1000 | ✓ col 1 |
| `2026` | 914–959 | 869–1000 | ✓ col 1 |
| `(Unaudited)` | 888–983 | 869–1000 | ✓ col 1 |
| `31 March` | 1028–1107 | 1000–1131 | ✓ col 2 |
| `2026` | 1045–1091 | 1000–1131 | ✓ col 2 |
| `30 June` | 1161–1229 | 1131–1256 | ✓ col 3 |
| `2025` | 1173–1219 | 1131–1256 | ✓ col 3 |
| `31 March` | 1285–1364 | 1256–1400 | ✓ col 4 |
| `2026` | 1301–1347 | 1256–1400 | ✓ col 4 |

Nothing needs to be re-detected. The association needs to stop being thrown
away.

### 4.1 The date is split across two OCR lines

`30 June` sits at y≈435 and `2026` at y≈455 — separate elements on separate
lines. Any header model that reads one line at a time cannot see a full date
here. Header text must be assembled **per column, down the header block**, not
per line across it.

### 4.2 Ordering cannot be used to assign columns

Three rows in this one table have missing cells:

| Row | Values and their x positions |
|---|---|
| Remeasurement gains on investment | `11.31`@1080 (col 2), `21.80`@1334 (col 4) |
| Foreign currency translation reserve | `0.03`@954 (col 1), `0.20`@1087 (col 2), `0.20`@1343 (col 4) |
| Paid up equity share capital | `1,247.81`@1312 (col 4 only) |

Assigning by order would place `11.31` in column 1 and `21.80` in column 2 —
both wrong, and wrong by a full year in period terms. **x-geometry is the only
safe basis for column membership**, which is what the brief requires and what
these rows prove independently.

### 4.3 Group headers need a span rule, not containment

`Quarter ended` occupies x 1009–1122, which lies inside column 2 alone — yet it
governs columns 1, 2 and 3. Containment cannot express that.

The rule that works on this table: **group headers partition the columns at
their own left edges.** `Quarter ended` starts at 1009 and `Year ended` at 1278,
so every column whose centre is below 1278 belongs to `Quarter ended` and the
rest to `Year ended`:

| Column | Centre | Group | Correct? |
|---|---|---|---|
| 1 | 935 | Quarter ended | ✓ |
| 2 | 1066 | Quarter ended | ✓ |
| 3 | 1194 | Quarter ended | ✓ |
| 4 | 1328 | Year ended | ✓ |

The midpoint alternative — a boundary at (1122+1278)/2 = 1200 — also works here
but puts column 3's centre 6px from the boundary. The left-edge rule has 84px
of margin. Measured on one table, so it is a starting rule to be tested against
the corpus, not a law.

### 4.4 Row-level unit overrides are present and needed

The same table carries both:

- `Current tax … 260.40` — INR crore, from the table declaration
- `a) Basic earnings per share … 1.19` under the heading
  `Earnings per share in INR (Face Value : INR 2/- per share)` — **INR per
  share, not crore**

A table-level unit applied blindly would report basic EPS as ₹1.19 crore. The
row's own evidence has to win, which is the existing evidence-precedence model
(cell > row > table > taxonomy) applied to a case it currently misses.

---

## 5. What has to change

In dependency order.

1. **Stitch fragments back into one table.** Adjacent table regions sharing a
   horizontal extent and compatible column geometry are one table. This is
   geometric and generic; no document-specific rule.
2. **Assemble header text per column, down the header block**, so a date split
   across lines is recovered, and multi-level headers keep their levels instead
   of being flattened into one string.
3. **Never parse a period from text spanning more than one column.** That is
   what produced 2031.
4. **Separate "no header" from "a header that is not a period".** An empty
   header must be UNKNOWN + review, never NOT_APPLICABLE.
5. **Make period mandatory for periodized statements.** Classify the statement
   shape; if it is periodized and a column's period cannot be established, the
   fact is UNKNOWN + REVIEW_REQUIRED, never HIGH.
6. **Validate the reconstruction before persisting.** Column/value alignment,
   header-value x-overlap, duplicate dates, cell counts.
7. **Confidence must require temporal evidence** on periodized statements.

## 6. What is deliberately *not* changing

- **The layout detector's region splitting.** It is not wrong to see four bands;
  the bands are real. The table layer should be able to recover from them, which
  is a smaller and safer change than re-tuning layout across every document.
- **The UNKNOWN vs NOT_APPLICABLE distinction.** It is correct and worth
  keeping. Only the evidence used to decide between them changes.
- **Existing column detection geometry.** Section 4 shows it is already right.
