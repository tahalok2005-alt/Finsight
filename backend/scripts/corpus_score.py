"""Score recorded corpus runs against the golden facts. Never reprocesses.

    cd backend
    ../.venv/Scripts/python.exe scripts/corpus_score.py --tag p22a
    ../.venv/Scripts/python.exe scripts/corpus_score.py --tag p22b --compare p22a

Writes
  data/corpus/scores/<tag>.json          everything, per fact
  data/corpus/DASHBOARD_<tag>.md         the developer dashboard
  data/corpus/QUALITY_<tag>.md           document and company quality reports
  data/corpus/evaluation_manifest.json   what was scored, from what (latest)
  data/corpus/evaluations/<tag>.scored.json   the same, kept per tag

Exit status is the acceptance gate (Phase 22, section 35): 0 when no wrong
fact goes unflagged in any gated dimension, 2 when one does, 3 when the tag's
records do not all come from one run.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.corpus_runs import (  # noqa: E402
    CORPUS_DIR, EVALUATIONS_DIR, MANIFEST, load_manifest, load_record, record_path,
)
from app.evaluation.golden import load_all  # noqa: E402
from app.evaluation.provenance import (  # noqa: E402
    file_sha, golden_fingerprint, now,
)
from app.evaluation.scoring import (  # noqa: E402
    DIMENSIONS, GATED, STATUSES, classify, ocr_texts, score_table,
)

GOLDEN_DIR = CORPUS_DIR.parent / "golden"
SCORES_DIR = CORPUS_DIR / "scores"
SCORER_FILES = [Path(__file__), Path(__file__).resolve().parents[1] / "app" / "evaluation" / "scoring.py",
                Path(__file__).resolve().parents[1] / "app" / "evaluation" / "golden.py"]


def _pct(n, d):
    return f"{n / d:.1%}" if d else "—"


def _validation_audit(rec, verdicts_by_fact: dict[str, str]) -> dict:
    """Accounting checks the run made, and whether each alarm was real.

    An alarm over figures the golden facts say are all correct is a FALSE
    alarm: the rule did not fit the page (a third tax component, a gain
    printed unsigned) or rounding crossed its tolerance. An alarm touching a
    wrong figure is a TRUE catch -- the reason the engine exists.
    """
    status = Counter(str(v.status) for v in rec.validations)
    alarms, false_alarms, true_catches, unjudged = 0, [], [], 0
    for v in rec.validations:
        if str(v.status) not in ("FAILED", "WARNING"):
            continue
        if v.rule in ("facts_without_period",):
            continue
        alarms += 1
        states = [verdicts_by_fact.get(fid) for fid in v.fact_ids]
        if states and all(s == "correct" for s in states):
            false_alarms.append({"rule": v.rule, "message": v.message})
        elif any(s == "wrong" for s in states):
            true_catches.append({"rule": v.rule, "message": v.message})
        else:
            unjudged += 1
    return {"status": dict(status), "alarms": alarms,
            "false_alarms": false_alarms, "true_catches": true_catches,
            "unjudged": unjudged}


def _provenance(tag: str, keys: list[str]) -> tuple[dict, list[str]]:
    """The run behind a tag, and every reason it cannot be trusted as one."""
    problems = []
    path = EVALUATIONS_DIR / f"{tag}.json"
    evaluation = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    run_ids = Counter()
    fingerprints = Counter()
    for key in keys:
        raw = json.loads(record_path(key, tag).read_text(encoding="utf-8"))
        prov = raw.get("provenance") or {}
        run_ids[prov.get("run_id")] += 1
        fingerprints[prov.get("code_fingerprint")] += 1
    if evaluation is None:
        problems.append("no run manifest: an unversioned (pre-Phase 22) run")
    if len(run_ids) > 1 or None in run_ids:
        problems.append(f"records from {len(run_ids)} runs: {dict(run_ids)}")
    if len(fingerprints) > 1:
        problems.append(f"records from {len(fingerprints)} code states")
    if evaluation and run_ids and next(iter(run_ids)) != evaluation["run_id"]:
        problems.append("records do not belong to the tag's run manifest")
    return evaluation or {}, problems


def _doc_quality(d: dict) -> dict:
    """Section 17: a document is judged on attribution, not on volume."""
    dims = defaultdict(Counter)
    found = golden = wrong = review = 0
    for t in d["tables"]:
        golden += t["golden"]
        found += t["found"]
        wrong += t["wrong_facts"]
        review += t["review"]
        for name, c in t["dims"].items():
            dims[name].update(c)
    def acc(name):
        c = dims[name]
        a = c.get("ok", 0) + c.get("wrong", 0)
        return round(c.get("ok", 0) / a, 4) if a else None
    val = d["validation"]["status"]
    checks = sum(val.values())
    conflicts = d.get("false_conflicts", 0)
    return {"golden": golden, "found": found, "wrong_facts": wrong,
            "coverage": round(found / golden, 4) if golden else None,
            **{f"{n}_accuracy": acc(n) for n in ("metric", "value", *[x for x in DIMENSIONS if x != "metric"])},
            "accounting_pass_rate": round(val.get("PASSED", 0) / checks, 4) if checks else None,
            "review_rate": round(review / found, 4) if found else None,
            "false_conflict_rate": round(conflicts / found, 4) if found else None}


def _evidence_quality(rec) -> dict:
    """Section 17 for EVERY document, golden or not: what the facts themselves
    say about how well each dimension was established -- never a claim of
    accuracy, which only a golden table can support."""
    facts = list(rec.facts)
    n = len(facts)
    if not n:
        return {"facts": 0}

    def share(pred):
        return round(sum(1 for f in facts if pred(f)) / n, 4)

    def ptype(f):
        p = getattr(f, "period_normalized", None)
        return str(getattr(p, "period_type", "") or "") if p is not None else ""

    monetary = [f for f in facts if str(getattr(f, "value_type", "")) == "monetary"]
    states = Counter(str((f.meta or {}).get("fact_state", "") or "unset") for f in facts)
    reasons = Counter(str(f.review_reason) for f in facts if f.requires_review and f.review_reason)
    sr = Counter()
    for f in facts:
        agrees = ((f.meta or {}).get("second_read") or {}).get("agrees", "absent")
        sr["agreed" if agrees is True else "disputed" if agrees is False
           else "unread" if agrees is None else "not_read"] += 1
    return {
        "facts": n,
        "period_end_known": share(lambda f: getattr(getattr(f, "period_normalized", None), "end_date", None)),
        "duration_known": share(lambda f: ptype(f) not in ("", "unknown")),
        "basis_stated": share(lambda f: str(getattr(f, "entity_type", "")) not in ("unknown", "", "company")),
        "unit_known_of_monetary": (round(sum(1 for f in monetary if str(getattr(f, "unit", "")) not in ("none", "")) / len(monetary), 4)
                                   if monetary else None),
        "review_rate": share(lambda f: f.requires_review),
        "review_reasons": dict(reasons.most_common()),
        "fact_states": dict(states.most_common()),
        "second_read": dict(sr),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--compare")
    args = ap.parse_args()

    manifest = {d.key: d for d in load_manifest()}
    goldens = [g for g in load_all(GOLDEN_DIR) if g.doc_key in manifest]
    by_doc: dict[str, list] = defaultdict(list)
    for g in goldens:
        by_doc[g.doc_key].append(g)
    keys = [k for k in manifest if record_path(k, args.tag).exists()]
    evaluation, problems = _provenance(args.tag, keys)
    mixed = any("runs" in p or "code states" in p or "do not belong" in p for p in problems)

    docs_out, all_tables = [], []
    for key in keys:
        doc = manifest[key]
        rec = load_record(key, args.tag, expect_sha256=doc.sha256)
        tables = []
        for g in by_doc.get(key, []):
            ts = score_table(g, rec.facts)
            ocr = ocr_texts(doc.sha256, g.page)
            for v in ts.verdicts:
                classify(v, g, tables=rec.tables, facts=rec.facts, ocr=ocr)
            tables.append(ts)
        verdicts_by_fact = {v.fact_id: v.status for t in tables
                            for v in t.verdicts if v.fact_id}
        facts_by_id = {f.fact_id: f for f in rec.facts}
        false_conflicts = sum(
            1 for fid, s in verdicts_by_fact.items()
            if s == "correct" and str(getattr(facts_by_id.get(fid), "review_reason", "") or "")
            == "conflicting_sources")
        # The second read's disputes on figures the golden table says are right
        # (review noise), and on figures it says are wrong (catches).
        def _sr(fid):
            return ((getattr(facts_by_id.get(fid), "meta", None) or {}).get("second_read") or {}).get("agrees")
        wrong_value_ids = {v.fact_id for t in tables for v in t.verdicts
                           if v.fact_id and v.status == "wrong" and "value" in v.wrong_dims}
        second_read_audit = {
            "false_disputes": sum(1 for fid, st in verdicts_by_fact.items()
                                  if st in ("correct", "incomplete") and _sr(fid) is False),
            "disputes_on_wrong_values": sum(1 for fid in wrong_value_ids if _sr(fid) is False),
            "wrong_values": len(wrong_value_ids),
            "golden_facts_read": sum(1 for fid in verdicts_by_fact if _sr(fid) is not None)}
        audit = _validation_audit(rec, verdicts_by_fact)
        facts_total = len(rec.facts)
        review_total = sum(1 for f in rec.facts if f.requires_review)
        out = {
            "key": key, "company": doc.company, "sector": doc.sector,
            "pages": rec.pages, "facts_extracted": facts_total,
            "tables_extracted": len(rec.tables),
            "review_rate_all": round(review_total / facts_total, 4) if facts_total else None,
            "wall_seconds": rec.wall_seconds, "stage_seconds": rec.stage_seconds(),
            "validation": audit, "false_conflicts": false_conflicts,
            "second_read_audit": second_read_audit,
            "provenance": rec.provenance,
            "tables": [t.as_dict() for t in tables],
        }
        out["quality"] = _doc_quality(out)
        out["evidence_quality"] = _evidence_quality(rec)
        docs_out.append(out)
        all_tables.extend(tables)

    # ---- corpus totals ----------------------------------------------------
    tot = Counter()
    dims = {d: Counter() for d in DIMENSIONS + ("value",)}
    cats, status_detail, unflagged, extras_kinds = Counter(), Counter(), Counter(), Counter()
    for t in all_tables:
        d = t.as_dict()
        for k in ("golden", "found", "correct", "incomplete", "wrong", "missing",
                  "wrong_metric", "wrong_value_extra", "wrong_unflagged", "review",
                  "wrong_facts", "wrong_facts_unflagged"):
            tot[k] += d[k]
        for name, c in t.dim_counts().items():
            dims[name].update(c)
        cats.update(Counter(str(v.category) for v in t.verdicts if v.category))
        status_detail.update(t.status_counts())
        unflagged.update(t.unflagged_wrong())
        extras_kinds.update(e.kind for e in t.extras)
    val_alarms = sum(d["validation"]["alarms"] for d in docs_out)
    val_false = sum(len(d["validation"]["false_alarms"]) for d in docs_out)
    val_true = sum(len(d["validation"]["true_catches"]) for d in docs_out)
    false_conflicts = sum(d["false_conflicts"] for d in docs_out)
    sr_audit = Counter()
    for d in docs_out:
        sr_audit.update(d["second_read_audit"])
    val_status = Counter()
    for d in docs_out:
        val_status.update(d["validation"]["status"])
    gate = {dim: unflagged.get(dim, 0) for dim in GATED}
    gate_failed = [dim for dim, n in gate.items() if n]

    summary = {
        "tag": args.tag, "documents": len(docs_out),
        "companies": len({d["company"] for d in docs_out}),
        "pages": sum(d["pages"] for d in docs_out),
        "golden_tables": len(all_tables), **tot,
        "status_detail": {s: status_detail.get(s, 0) for s in STATUSES},
        "extras": dict(extras_kinds),
        "unflagged_wrong_by_dim": gate, "gate_failed": gate_failed,
        "false_conflicts": false_conflicts,
        "second_read": dict(sr_audit),
        "dims": {k: dict(v) for k, v in dims.items()},
        "categories": dict(cats),
        "validation": {"status": dict(val_status), "alarms": val_alarms,
                       "false_alarms": val_false, "true_catches": val_true},
        "provenance_problems": problems,
    }
    scored = {
        "tag": args.tag, "run_id": evaluation.get("run_id"),
        "parser_version": (evaluation.get("code") or {}).get("fingerprint"),
        "code": evaluation.get("code"), "configuration": evaluation.get("config"),
        "git": evaluation.get("git"),
        "corpus_manifest_sha256": evaluation.get("manifest_sha256"),
        "corpus_manifest_sha256_now": file_sha(MANIFEST),
        "expected_facts_version": golden_fingerprint(GOLDEN_DIR),
        "scorer_version": file_sha(SCORER_FILES[1])[:16],
        "run_started_at": evaluation.get("started_at"),
        "run_finished_at": evaluation.get("finished_at"),
        "scored_at": now(), "provenance_problems": problems,
        "documents": [{"key": d["key"], "file": manifest[d["key"]].path.name,
                       "sha256": manifest[d["key"]].sha256,
                       "record_sha256": file_sha(record_path(d["key"], args.tag)),
                       "run_id": (d["provenance"] or {}).get("run_id")} for d in docs_out],
        "results": {k: summary[k] for k in ("golden", "found", "correct", "incomplete",
                                              "wrong_facts", "wrong_facts_unflagged",
                                              "missing", "status_detail",
                                              "unflagged_wrong_by_dim", "gate_failed")},
    }
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    EVALUATIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SCORES_DIR / f"{args.tag}.json").write_text(
        json.dumps({"summary": summary, "evaluation": scored, "documents": docs_out},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    text = json.dumps(scored, indent=1, ensure_ascii=False)
    (EVALUATIONS_DIR / f"{args.tag}.scored.json").write_text(text, encoding="utf-8")
    if not mixed:
        (CORPUS_DIR / "evaluation_manifest.json").write_text(text, encoding="utf-8")

    # ---- dashboard ----------------------------------------------------------
    def acc(name):
        c = dims[name]
        asserted = c.get("ok", 0) + c.get("wrong", 0)
        return f"{_pct(c.get('ok', 0), asserted)} ({c.get('wrong', 0)} wrong, {c.get('unknown', 0)} unknown)"

    lines = [
        f"# Corpus dashboard — `{args.tag}`", "",
        "Developer-only. Generated by `backend/scripts/corpus_score.py` from recorded",
        "runs; nothing here reprocesses a document. Golden facts are transcribed from",
        "the source documents (see each file under `data/golden/`), never from FinSight.",
        "",
        f"Run `{scored['run_id']}` · parser `{scored['parser_version']}` · config "
        f"`{(scored['configuration'] or {}).get('hash')}` · expected facts "
        f"`{scored['expected_facts_version']}` · scorer `{scored['scorer_version']}`"
        + (f" · **PROVENANCE: {'; '.join(problems)}**" if problems else ""),
        "", "## Corpus", "",
        "| Companies | Documents | Pages | Golden tables | Golden facts |",
        "|---|---|---|---|---|",
        f"| {summary['companies']} | {summary['documents']} | {summary['pages']} | "
        f"{summary['golden_tables']} | {tot['golden']} |",
        "", "## Quality", "",
        "| Measure | Value |", "|---|---|",
        f"| Extracted (found) | {tot['found']} / {tot['golden']} = **{_pct(tot['found'], tot['golden'])}** |",
        f"| **Correct** (every stated dimension right, metric included) | {tot['correct']} = **{_pct(tot['correct'], tot['golden'])}** of golden |",
        f"| Ambiguous (nothing wrong, something unknown) | {tot['incomplete']} |",
        f"| **Wrong facts** (golden verdicts + wrong extras) | **{tot['wrong_facts']}** |",
        f"| — with no review flag | **{tot['wrong_facts_unflagged']}** |",
        f"| Missing | {tot['missing']} |",
        f"| Accuracy of what was found | {_pct(tot['found'] - tot['wrong'], tot['found'] + tot['wrong_metric'] + tot['wrong_value_extra'])} |",
        f"| Metric accuracy | {acc('metric')} |",
        f"| Period accuracy (end date) | {acc('period')} |",
        f"| Duration accuracy | {acc('duration')} |",
        f"| Entity / basis accuracy | {acc('entity')} |",
        f"| Unit accuracy | {acc('unit')} |",
        f"| Currency accuracy | {acc('currency')} |",
        f"| Value accuracy | {acc('value')} |",
        f"| Review rate (golden facts found) | {_pct(tot['review'], tot['found'])} |",
        f"| False conflicts (a correct fact flagged as conflicting) | {false_conflicts} |",
        f"| Second read: golden-found facts read | {sr_audit.get('golden_facts_read', 0)} |",
        f"| Second read: false disputes (a correct figure disputed) | {sr_audit.get('false_disputes', 0)} |",
        f"| Second read: wrong values it disputed | {sr_audit.get('disputes_on_wrong_values', 0)} of {sr_audit.get('wrong_values', 0)} |",
        f"| Accounting checks run | {sum(val_status.values())} ({dict(val_status)}) |",
        f"| Accounting alarms | {val_alarms}: **{val_false} false** (all figures correct), {val_true} true catches |",
        "", "## Status of every golden fact", "", "| Status | Facts |", "|---|---|"]
    for s in STATUSES:
        lines.append(f"| {s} | {summary['status_detail'][s]} |")
    lines += ["", f"Extras (region facts no golden fact paired): {dict(extras_kinds)}",
              "", "## Acceptance gate — wrong AND unflagged", "",
              "| Dimension | Wrong facts with no review flag |", "|---|---|"]
    for dim in GATED:
        lines.append(f"| {dim} | {'**' + str(gate[dim]) + '**' if gate[dim] else 0} |")
    lines.append("")
    lines.append(f"**Gate: {'FAILED on ' + ', '.join(gate_failed) if gate_failed else 'PASSED'}**")
    lines += ["", "## Failure taxonomy (every missing, wrong or incomplete golden fact)", "",
              "| Category | Facts |", "|---|---|"]
    for c, n in sorted(cats.items()):
        lines.append(f"| {c} | {n} |")
    lines += ["", "## Per document", "",
              "| Doc | Company | Pages | Facts out | Golden | Found | Correct | Wrong facts | Unflagged | Missing | Review (all) | Seconds | OCR s | Extract s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in docs_out:
        g = sum(t["golden"] for t in d["tables"])
        fnd = sum(t["found"] for t in d["tables"])
        cor = sum(t["correct"] for t in d["tables"])
        wr = sum(t["wrong_facts"] for t in d["tables"])
        wu = sum(t["wrong_facts_unflagged"] for t in d["tables"])
        mi = sum(t["missing"] for t in d["tables"])
        st = d["stage_seconds"]
        lines.append(
            f"| {d['key']} | {d['company'][:28]} | {d['pages']} | {d['facts_extracted']} | "
            f"{g or '—'} | {fnd if g else '—'} | {cor if g else '—'} | {wr if g else '—'} | "
            f"{wu if g else '—'} | {mi if g else '—'} | "
            f"{_pct(d['review_rate_all'] or 0, 1) if d['review_rate_all'] is not None else '—'} | "
            f"{d['wall_seconds']:.0f} | {st.get('ocr', 0):.0f} | {st.get('extraction', 0):.0f} |")
    lines += ["", "## Per golden table", "",
              "| Table | Golden | Correct | Ambiguous | Wrong | Missing | Positions | Top cause |",
              "|---|---|---|---|---|---|---|---|"]
    for t in all_tables:
        d = t.as_dict()
        top = max(d["categories"].items(), key=lambda kv: kv[1])[0] if d["categories"] else "—"
        lines.append(f"| {d['file']} | {d['golden']} | {d['correct']} | {d['incomplete']} | "
                     f"{d['wrong_facts']} | {d['missing']} | {d['positional'][:28]} | {top} |")
    if args.compare and (SCORES_DIR / f"{args.compare}.json").exists():
        prev = json.loads((SCORES_DIR / f"{args.compare}.json").read_text(encoding="utf-8"))["summary"]
        lines += ["", f"## Against `{args.compare}`", "", "| Measure | Before | After |", "|---|---|---|"]
        for k in ("found", "correct", "incomplete", "wrong_facts", "wrong_facts_unflagged",
                  "missing"):
            lines.append(f"| {k} | {prev.get(k, '—')} | {summary.get(k, 0)} |")
        lines.append(f"| false accounting alarms | {prev['validation']['false_alarms']} | {val_false} |")
    (CORPUS_DIR / f"DASHBOARD_{args.tag}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- document and company quality (sections 17-18) ----------------------
    q = [f"# Document and company quality — `{args.tag}`", "",
         "Accuracy is over facts that assert the dimension (right + wrong); unknown",
         "is reported by the dashboard, never scored as right.", "",
         "## Documents", "",
         "| Doc | Coverage | Metric | Value | Period | Duration | Entity | Unit | Currency | Accounting pass | Review | False conflicts |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    fmt = lambda x: "—" if x is None else f"{x:.1%}"  # noqa: E731
    for d in docs_out:
        qq = d["quality"]
        if not qq["golden"]:
            continue
        q.append(f"| {d['key']} | {fmt(qq['coverage'])} | {fmt(qq['metric_accuracy'])} | "
                 f"{fmt(qq['value_accuracy'])} | {fmt(qq['period_accuracy'])} | "
                 f"{fmt(qq['duration_accuracy'])} | {fmt(qq['entity_accuracy'])} | "
                 f"{fmt(qq['unit_accuracy'])} | {fmt(qq['currency_accuracy'])} | "
                 f"{fmt(qq['accounting_pass_rate'])} | {fmt(qq['review_rate'])} | "
                 f"{fmt(qq['false_conflict_rate'])} |")
    q += ["", "## Every document -- what its facts establish", "",
          "Evidence, not accuracy: shares of the document's own facts whose period end,",
          "span, basis and unit are established; how many went to review and why; the",
          "second read's outcome; accounting checks. Golden or not.", "",
          "| Doc | Golden | Facts | Period end | Span | Basis stated | Unit (money) | Review | Second read disputed | Accounting pass | Top review reason |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in docs_out:
        e = d["evidence_quality"]
        if not e.get("facts"):
            q.append(f"| {d['key']} | {'yes' if d['quality']['golden'] else 'no'} | 0 | — | — | — | — | — | — | — | — |")
            continue
        top = next(iter(e["review_reasons"]), "—")
        q.append(f"| {d['key']} | {'yes' if d['quality']['golden'] else 'no'} | {e['facts']} | "
                 f"{fmt(e['period_end_known'])} | {fmt(e['duration_known'])} | {fmt(e['basis_stated'])} | "
                 f"{fmt(e['unit_known_of_monetary'])} | {fmt(e['review_rate'])} | "
                 f"{e['second_read'].get('disputed', 0)} | {fmt(d['quality']['accounting_pass_rate'])} | {top} |")
    comp = defaultdict(lambda: {"docs": 0, "pages": 0, "golden": 0, "facts": 0, "found": 0,
                                "correct": 0, "wrong": 0, "missing": 0,
                                "dims": defaultdict(Counter)})
    for d in docs_out:
        c = comp[d["company"]]
        c["docs"] += 1
        c["pages"] += d["pages"]
        c["facts"] += d["facts_extracted"]
        for t in d["tables"]:
            c["golden"] += t["golden"]
            c["found"] += t["found"]
            c["correct"] += t["correct"]
            c["wrong"] += t["wrong_facts"]
            c["missing"] += t["missing"]
            for name, cc in t["dims"].items():
                c["dims"][name].update(cc)
    q += ["", "## Companies", "",
          "| Company | Docs | Pages | Golden | Extracted | Correct | Wrong | Missing | Coverage | Accuracy | Metric | Period | Duration | Entity | Unit |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in sorted(comp.items()):
        def a(dim):
            cc = c["dims"][dim]
            n = cc.get("ok", 0) + cc.get("wrong", 0)
            return fmt(cc.get("ok", 0) / n) if n else "—"
        g = c["golden"]
        q.append(f"| {name[:34]} | {c['docs']} | {c['pages']} | {g or '—'} | {c['facts']} | "
                 f"{c['correct'] if g else '—'} | {c['wrong'] if g else '—'} | "
                 f"{c['missing'] if g else '—'} | {fmt(c['found'] / g) if g else '—'} | "
                 f"{fmt((c['found'] - c['wrong']) / c['found']) if c['found'] else '—'} | "
                 f"{a('metric')} | {a('period')} | {a('duration')} | {a('entity')} | {a('unit')} |")
    (CORPUS_DIR / f"QUALITY_{args.tag}.md").write_text("\n".join(q) + "\n", encoding="utf-8")

    print("\n".join(lines[:60]))
    if mixed:
        print(f"\nREFUSED AS A SCORECARD: {'; '.join(problems)}")
        return 3
    if gate_failed:
        print(f"\nGATE FAILED: unflagged wrong facts in {', '.join(gate_failed)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
