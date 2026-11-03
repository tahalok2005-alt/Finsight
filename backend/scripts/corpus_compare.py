"""Source-to-FinSight comparison sheet (Phase 21, section 35).

For each company, a deterministic spread of golden facts -- read from the
source PDF by eye, never from FinSight -- is set beside what FinSight
extracted for the same printed cell, with the verdict of every dimension.
Reads a scored tag (``scripts/corpus_score.py``) and the run records; nothing
is reprocessed.

    python scripts/corpus_compare.py --tag after6 --per-company 20
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.corpus_runs import CORPUS_DIR, load_record  # noqa: E402

COMPANIES = ["ICICI", "WIPRO", "AIRTEL", "TATASTEEL", "HUL", "SAP", "ETERNAL",
             "SBI", "INDIANBANK", "KOTAK"]


def _spread(items: list, n: int) -> list:
    """n items evenly spaced through the list, first and last included."""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--per-company", type=int, default=20)
    ap.add_argument("--companies", nargs="*", default=COMPANIES)
    args = ap.parse_args()

    scores = json.loads((CORPUS_DIR / "scores" / f"{args.tag}.json").read_text(
        encoding="utf-8"))
    by_doc = {d["key"]: d for d in scores["documents"]}
    lines = [f"# Source-to-FinSight comparison — `{args.tag}`", "",
             "Each row is one figure as printed in the source (transcribed by eye "
             "into `data/golden/`, never from FinSight) beside the fact FinSight "
             "extracted for it. Verdict: **correct** = every stated dimension "
             "right; **incomplete** = nothing asserted wrongly, something left "
             "unknown; **wrong** = a dimension asserted wrongly; **missing** = no "
             "fact.", ""]
    totals: dict[str, int] = defaultdict(int)
    for company in args.companies:
        doc = by_doc.get(company)
        if doc is None:
            continue
        record = load_record(company, args.tag)
        facts = {f.fact_id: f for f in record.facts}
        verdicts = [(t["file"], t["page"], v) for t in doc.get("tables", [])
                    for v in t["verdicts"]]
        verdicts.sort(key=lambda x: (x[0], x[2]["row"], x[2]["column"]))
        chosen = _spread(verdicts, args.per_company)
        lines += [f"## {doc['company']} ({company})", "",
                  "| # | Page | Row (as printed) | Col | Source value | Period | "
                  "FinSight value | FinSight period | Unit / cur. | Entity | Verdict |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, (file, page, v) in enumerate(chosen, 1):
            f = facts.get(v.get("fact_id") or "")
            fin_value = f"{f.value:,}" if f and f.value is not None else "—"
            fin_period = (f.period or "(none)") if f else "—"
            fin_uc = f"{f.unit} / {f.currency}" if f else "—"
            fin_entity = (f.entity or "—") if f else "—"
            wrong = [k for k, s in v["dims"].items() if s == "wrong"]
            unknown = [k for k, s in v["dims"].items() if s == "unknown"]
            verdict = v["status"]
            if wrong:
                verdict += f" ({', '.join(wrong)})"
            elif unknown and v["status"] == "incomplete":
                verdict += f" ({', '.join(unknown)} unknown)"
            totals[v["status"]] += 1
            lines.append(
                f"| {i} | {page} | {v['row'][:55]} | {v['column']} | "
                f"{v['value']:,} | {v['period_end']} {v['period_type']} | "
                f"{fin_value} | {fin_period} | {fin_uc} | {fin_entity} | {verdict} |")
        lines.append("")
    n = sum(totals.values())
    lines[4:4] = [f"**{n} comparisons:** " + ", ".join(
        f"{k} {v} ({v / n:.0%})" for k, v in sorted(totals.items())) + "", ""]
    out = CORPUS_DIR / f"COMPARISON_{args.tag}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out, dict(totals))


if __name__ == "__main__":
    main()
