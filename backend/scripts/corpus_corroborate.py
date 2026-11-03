"""Cross-source corroboration (Phase 21, sections 21 and 22).

For documents of one company, the facts FinSight extracted from each are
matched on (metric, reporting basis, period end, period kind) and compared.
Agreement between two independently processed documents -- a scanned and a
typeset copy of one statement, a results filing and its press release -- is
evidence the reading is right; disagreement is either an extraction error in
one of them or a restatement, and is listed for a reader to decide. Nothing
is reprocessed.

    python scripts/corpus_corroborate.py --tag after7
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.corpus_runs import CORPUS_DIR, load_record  # noqa: E402

GROUPS = {
    "Kotak Mahindra Bank": ["KOTAK", "KOTAK_PT", "KOTAK_PR"],
    "State Bank of India": ["SBI", "SBI_PPT"],
    "Tata Steel": ["TATASTEEL", "TATASTEEL_PR", "TATASTEEL_PPT"],
}


def _basis(f) -> str:
    t = str(getattr(f, "entity_type", "") or "")
    return t if t in ("standalone", "consolidated") else "other"


def _key(f):
    p = f.period_normalized
    if f.value is None or p is None or not p.end_date:
        return None
    return (f.canonical_metric, _basis(f), p.end_date, str(p.period_type))


def _agree(a: float, b: float) -> bool:
    # Rounding between a statement (two decimals) and a release (whole
    # numbers) is agreement; anything wider is not.
    return abs(a - b) <= max(1.0, 0.005 * max(abs(a), abs(b)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    lines = [f"# Cross-source corroboration — `{args.tag}`", "",
             "Facts from two documents of one company, matched on metric, "
             "reporting basis, period end and period kind. A figure read the "
             "same from two independently processed documents is corroborated; "
             "a disagreement is an extraction error in one of them or a "
             "restatement, and is listed.", ""]
    for company, keys in GROUPS.items():
        facts = {}
        for key in keys:
            try:
                record = load_record(key, args.tag)
            except FileNotFoundError:
                continue
            by = defaultdict(list)
            for f in record.facts:
                k = _key(f)
                if k:
                    by[k].append(f)
            facts[key] = by
        lines += [f"## {company}", "",
                  "| Pair | Shared keys | Agree | Disagree | Only in first | Only in second |",
                  "|---|---|---|---|---|---|"]
        disagreements = []
        for a, b in combinations(facts, 2):
            ka, kb = set(facts[a]), set(facts[b])
            shared = ka & kb
            agree = disagree = 0
            for k in sorted(shared):
                va = {round(f.value, 2) for f in facts[a][k]}
                vb = {round(f.value, 2) for f in facts[b][k]}
                if any(_agree(x, y) for x in va for y in vb):
                    agree += 1
                else:
                    disagree += 1
                    disagreements.append((a, b, k, sorted(va), sorted(vb)))
            lines.append(f"| {a} vs {b} | {len(shared)} | {agree} | {disagree} | "
                         f"{len(ka - kb)} | {len(kb - ka)} |")
        lines.append("")
        if disagreements:
            lines += ["Disagreements:", "",
                      "| Pair | Metric | Basis | Period end | Kind | First | Second |",
                      "|---|---|---|---|---|---|---|"]
            for a, b, (m, basis, end, kind), va, vb in disagreements[:40]:
                lines.append(f"| {a}/{b} | {m} | {basis} | {end} | {kind} | "
                             f"{', '.join(f'{v:,}' for v in va)} | "
                             f"{', '.join(f'{v:,}' for v in vb)} |")
            lines.append("")
    out = CORPUS_DIR / f"CORROBORATION_{args.tag}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
