"""Does a faster setting change what the pipeline reads? (Phase 23)

    cd backend
    ../.venv/Scripts/python.exe scripts/perf_knobs.py --doc GROWW --out ../data/perf/knobs.json

Each variant runs in a FRESH PROCESS with its OWN empty cache directory, one
at a time, so every run is cold and comparable. For each run the tool records
the wall time *and* a fingerprint of everything the run extracted -- every
fact's metric, value, unit, currency, period, entity and page.

The fingerprint is the point. A setting that makes the pipeline faster is only
an improvement if the document still reads the same; a variant whose
fingerprint differs from the baseline is reported as a change in output, never
as a speed-up, and the figures that moved are listed so the difference can be
judged by eye rather than waved through.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _available_mb() -> int | None:
    """Free memory now, because the pipeline sizes its worker pool from it.

    A variant that reports eight workers on a machine with 1.7 GB free is not
    a faster setting, it is a thrashing one, and the only way to tell the two
    apart afterwards is to have written down what was free at the time.
    """
    try:
        from app.config.settings import _available_memory_mb
        v = _available_memory_mb()
        return int(v) if v else None
    except Exception:
        return None


def _child(pdf: str, label: str) -> None:
    import hashlib as h
    from app.config.settings import settings
    from app.core.modes import ProcessingMode
    from app.ocr.registry import get_engine
    from app.services.pipeline import Pipeline

    get_engine().warmup()
    free_before = _available_mb()
    content_hash = h.sha256(Path(pdf).read_bytes()).hexdigest()
    t0 = time.perf_counter()
    result = Pipeline(document_id=f"knob_{label}", pdf_path=Path(pdf),
                      content_hash=content_hash,
                      mode=ProcessingMode.VISUAL_ONLY).run()
    wall = time.perf_counter() - t0

    facts = sorted(
        (f"{f.canonical_metric}|{f.value}|{f.unit}|{f.currency}|"
         f"{(f.period_normalized.end_date if f.period_normalized else None)}|"
         f"{f.entity_type}|{f.source_page}")
        for f in result.facts)
    # Tables are fingerprinted separately because the fact hash does not cover
    # them: a six-worker run returned the identical facts and ONE TABLE FEWER,
    # and the check reported "same output" because tables were outside it. A
    # measurement that only watches what you thought to watch is not a check.
    tables = sorted(
        (f"p{t.page_number}|{t.n_rows}x{t.n_cols}|{t.detector}|"
         f"{round(t.bbox.x)},{round(t.bbox.y)},{round(t.bbox.width)},{round(t.bbox.height)}|"
         f"{(t.title or '')[:40]}")
        for t in result.tables)
    print("KNOB_RESULT " + json.dumps({
        "label": label,
        "seconds": round(wall, 2),
        "free_mb_before": free_before,
        "free_mb_after": _available_mb(),
        "pages": len(result.pages),
        "facts": len(result.facts),
        "tables": len(result.tables),
        "page_workers": settings.resolved_page_workers,
        "intra_threads": settings.ocr_intra_threads,
        "det_side": settings.ocr_det_limit_side_len,
        "fingerprint": hashlib.sha256("\n".join(facts).encode()).hexdigest()[:16],
        "tables_fingerprint": hashlib.sha256("\n".join(tables).encode()).hexdigest()[:16],
        "facts_detail": facts,
        "tables_detail": tables,
    }), flush=True)


#: name -> settings overridden for that run (as FINSIGHT_* environment).
VARIANTS: dict[str, dict[str, str]] = {
    # The pool is sized from free memory, which varies with whatever else the
    # machine is doing. An unpinned run is therefore NOT a controlled one: the
    # first sweep read 297s at two workers against 240s at four and looked
    # like an OCR result, when all it measured was that more memory happened
    # to be free the second time. Every variant pins the pool, so a difference
    # between two of them is a difference in the setting under test.
    "workers-2": {"PAGE_WORKERS": "2"},
    "workers-3": {"PAGE_WORKERS": "3"},
    "workers-4": {"PAGE_WORKERS": "4"},
    "workers-6": {"PAGE_WORKERS": "6"},
    "workers-8": {"PAGE_WORKERS": "8"},
    # 8 x 2 OCR threads oversubscribes 12 cores; one thread each does not.
    "workers-8-intra-1": {"PAGE_WORKERS": "8", "OCR_INTRA_THREADS": "1"},
    # Detection sweeps the whole canvas and is four fifths of this document's
    # cost, so a smaller canvas is the obvious place to look. It is not a
    # speed knob. At 1280 the Tesla cash-flow pages came back with DIFFERENT
    # FIGURES, not fewer -- capex -2,272 where the page says -2,248, -9,603
    # where it says -8,527 -- because the detector boxes the columns
    # differently and the recogniser then reads across a boundary. Kept here
    # so the result is on the record, and left out of the default sweep.
    "det-1280": {"OCR_DET_LIMIT_SIDE_LEN": "1280"},
    "det-960": {"OCR_DET_LIMIT_SIDE_LEN": "960"},
}

#: The variants worth running unattended: parallelism only, because it is the
#: only knob that cannot change what the document says.
DEFAULT_SWEEP = ("workers-2", "workers-3", "workers-4", "workers-6",
                 "workers-8", "workers-8-intra-1")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child")
    ap.add_argument("--label", default="run")
    ap.add_argument("--doc", action="append", default=[], help="corpus KEY or LABEL=PATH")
    ap.add_argument("--variants", default=",".join(DEFAULT_SWEEP))
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.child:
        _child(args.child, args.label)
        return 0

    from app.evaluation.corpus_runs import load_manifest
    docs = {d.key: str(d.path) for d in load_manifest()}

    root = Path(tempfile.mkdtemp(prefix="finsight_knobs_"))
    results: list[dict] = []
    for spec in args.doc:
        label, pdf = (spec.split("=", 1) if "=" in spec else (spec, docs.get(spec, "")))
        if not pdf:
            raise SystemExit(f"unknown document {spec!r}")
        baseline_fp = None
        for name in [v for v in args.variants.split(",") if v]:
            for rep in range(args.repeat):
                cache = root / f"{label}_{name}_{rep}"
                env = dict(os.environ, FINSIGHT_CACHE_DIR=str(cache),
                           FINSIGHT_RENDER_DIR=str(cache / "renders"),
                           FINSIGHT_LOG_LEVEL="WARNING")
                for k, v in VARIANTS[name].items():
                    env[f"FINSIGHT_{k}"] = v
                proc = subprocess.run(
                    [sys.executable, __file__, "--child", pdf, "--label", f"{label}_{name}"],
                    cwd=str(BACKEND), env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
                line = next((ln for ln in proc.stdout.splitlines()
                             if ln.startswith("KNOB_RESULT ")), None)
                if line is None:
                    print(json.dumps({"doc": label, "variant": name,
                                      "error": (proc.stderr or proc.stdout)[-500:]}))
                    continue
                r = json.loads(line[len("KNOB_RESULT "):])
                r.update({"doc": label, "variant": name, "repeat": rep})
                detail = r.pop("facts_detail")
                t_detail = r.pop("tables_detail")
                if baseline_fp is None and rep == 0:
                    baseline_fp = (r["fingerprint"], detail,
                                   r["tables_fingerprint"], t_detail)
                elif baseline_fp is not None:
                    same = r["fingerprint"] == baseline_fp[0]
                    same_t = r["tables_fingerprint"] == baseline_fp[2]
                    # both must hold: facts alone said "same" on a run that had
                    # lost a table
                    r["same_output_as_baseline"] = same and same_t
                    r["same_facts"] = same
                    r["same_tables"] = same_t
                    if not same:
                        base = set(baseline_fp[1])
                        now = set(detail)
                        r["lost"] = sorted(base - now)[:20]
                        r["gained"] = sorted(now - base)[:20]
                    if not same_t:
                        base_t = set(baseline_fp[3])
                        now_t = set(t_detail)
                        r["tables_lost"] = sorted(base_t - now_t)[:20]
                        r["tables_gained"] = sorted(now_t - base_t)[:20]
                results.append(r)
                print(json.dumps({k: r.get(k) for k in (
                    "doc", "variant", "seconds", "facts", "tables", "page_workers",
                    "intra_threads", "det_side", "free_mb_before",
                    "same_facts", "same_tables")}), flush=True)
                if args.out:
                    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
