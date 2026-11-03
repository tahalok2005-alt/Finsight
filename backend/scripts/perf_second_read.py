"""A/B timing of the second-read stage alone (Phase 22).

    cd backend
    ../.venv/Scripts/python.exe scripts/perf_second_read.py --tag p22n HCLTECH_FY26 NTPC_FY26 LT

For each document the recorded facts of ``--tag`` are read a second time with
each strategy in a FRESH PROCESS and an EMPTY second-read cache (a temporary
directory of its own), one run after another: the stage's own wall time,
cells read, agreements and disputes. The OCR stage is not involved, so the
comparison is of the second read and nothing else.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _provenance() -> dict:
    """The code and settings state the timings belong to (as corpus runs record)."""
    from app.evaluation.provenance import code_fingerprint, config_snapshot, git_state, now
    code = code_fingerprint()
    return {"measured_at": now(), "code_fingerprint": code["fingerprint"],
            "engines": code["engines"], "python": code["python"],
            "config_hash": config_snapshot()["hash"], "git": git_state()}


def _child(key: str, tag: str) -> None:
    from app.config.settings import settings
    from app.evaluation.corpus_runs import load_manifest, load_record
    from app.ocr import second_read
    from app.ocr.registry import get_engine

    doc = next(d for d in load_manifest() if d.key == key)
    rec = load_record(key, tag, expect_sha256=doc.sha256)
    facts = [f for f in rec.facts]
    engine = get_engine()
    engine.warmup()
    t0 = time.perf_counter()
    stats = second_read.verify(facts, doc.path, cache_scope=f"ab_{key}", engine=engine)
    wall = time.perf_counter() - t0
    verdicts = {f.fact_id: (f.meta or {}).get("second_read", {}).get("agrees") for f in facts
                if (f.meta or {}).get("second_read")}
    print("AB_RESULT " + json.dumps({
        "key": key, "strategy": settings.second_read_strategy,
        "workers": settings.second_read_workers or settings.resolved_page_workers,
        "wall_s": round(wall, 2), **stats.as_dict(), "verdicts": verdicts}), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.child:
        _child(args.keys[0], args.tag)
        return 0
    results = []
    for key in args.keys:
        for strategy in ("clip", "page"):
            cache = Path(tempfile.mkdtemp(prefix=f"sr_ab_{key}_{strategy}_"))
            env = dict(os.environ, FINSIGHT_CACHE_DIR=str(cache), FINSIGHT_LOG_LEVEL="WARNING",
                       FINSIGHT_SECOND_READ_STRATEGY=strategy)
            proc = subprocess.run([sys.executable, __file__, "--child", "--tag", args.tag, key],
                                  cwd=str(BACKEND), env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("AB_RESULT ")), None)
            if line is None:
                print(key, strategy, "FAILED", (proc.stderr or "")[-800:])
                continue
            r = json.loads(line[len("AB_RESULT "):])
            results.append(r)
            print(json.dumps({k: v for k, v in r.items() if k != "verdicts"}), flush=True)
    # verdict agreement between the strategies, per document
    for key in args.keys:
        pair = [r for r in results if r["key"] == key]
        if len(pair) == 2:
            a, b = pair[0]["verdicts"], pair[1]["verdicts"]
            same = sum(1 for k in a if a.get(k) == b.get(k))
            print(json.dumps({"key": key, "verdicts_compared": len(a), "same_verdict": same,
                              "speedup": round(pair[0]["wall_s"] / max(pair[1]["wall_s"], 1e-9), 2)}))
    if args.out:
        Path(args.out).write_text(json.dumps({"provenance": _provenance(), "results": results},
                                             indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
