"""Controlled performance measurements of the document pipeline (Phase 22).

    cd backend
    ../.venv/Scripts/python.exe scripts/perf_benchmark.py --out ../data/perf/p22.json \
        --doc GROWW --doc HDFC --doc RIL=../data/benchmark/Media_Release_RIL_...pdf
    ../.venv/Scripts/python.exe scripts/perf_benchmark.py --knee GROWW --workers 1,2,4,6,8

Every measurement runs in a FRESH PROCESS, one at a time, with its OWN cache
directory under ``--cache-root``:

* **cold** -- the cache directory is created empty for the run: every page is
  rendered and recognised, every cell read a second time;
* **warm** -- the same document again, in a new process, on the cache the cold
  run left: what a re-run of an unchanged file costs.

The main cache (``data/cache``) is never read, written or deleted. Cold and
warm are reported side by side and never averaged together. Nothing else of
the benchmark's own runs concurrently with a measurement; the machine's other
load is recorded (available memory at start) but cannot be controlled, so the
report says so.

Per run: wall seconds (process start to result, engine load included and
reported separately), each stage's seconds, pages/s, facts/s, peak working set
(the OS's own counter), OCR cache hits and misses, second-read cells read and
cached, and the per-page P50/P90/P95 of page processing time.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
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


# --------------------------------------------------------------------- child
def _peak_working_set_mb() -> float | None:
    """The process's peak working set, from the OS (Windows) or rusage."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            # The pseudo-handle is pointer-sized: without declared types ctypes
            # passes it as a 32-bit int and the call fails (Phase 22).
            kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC),
                                                   wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            h = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return round(pmc.PeakWorkingSetSize / 2**20, 1)
            return None
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001 - a missing counter is reported as missing
        return None


def _child(pdf: str, label: str) -> None:
    """One measured run, in this process. Prints one JSON line."""
    t_start = time.perf_counter()
    from app.config.settings import settings
    from app.core.modes import ProcessingMode
    from app.ocr import cache as ocr_cache
    from app.ocr.registry import get_engine
    from app.services.pipeline import Pipeline
    import app.services.pipeline as pipeline_mod

    hits = {"hit": 0, "miss": 0}
    real_load = ocr_cache.load

    def counting_load(*a, **k):
        r = real_load(*a, **k)
        hits["hit" if r is not None else "miss"] += 1
        return r
    ocr_cache.load = counting_load
    pipeline_mod.ocr_cache.load = counting_load

    page_ms: list[float] = []
    by_page: dict[int, float] = {}
    real_one = Pipeline._process_one_page

    def timed_one(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return real_one(self, *a, **k)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            page_ms.append(ms)
            n = a[2] if len(a) > 2 else k.get("page_number")
            if n is not None:
                by_page[int(n)] = ms
    Pipeline._process_one_page = timed_one

    t_engine = time.perf_counter()
    get_engine().warmup()
    engine_s = time.perf_counter() - t_engine
    import hashlib
    content_hash = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
    t0 = time.perf_counter()
    result = Pipeline(document_id=f"perf_{label}", pdf_path=Path(pdf), content_hash=content_hash,
                      mode=ProcessingMode.VISUAL_ONLY).run()
    run_s = time.perf_counter() - t0
    stages = {}
    for s in result.stages:
        d = s.detail or {}
        sec = d.get("phase_seconds") if isinstance(d, dict) and "phase_seconds" in d else None
        stages[str(s.stage)] = {"wall_ms": s.duration_ms, "phase_seconds": sec}
    sr = next((s.detail for s in result.stages if str(s.stage) == "second_read"), {}) or {}
    q = sorted(page_ms)

    def pct(p):
        if not q:
            return None
        i = min(len(q) - 1, max(0, round(p / 100 * (len(q) - 1))))
        return round(q[i], 1)
    pages = len(result.pages)
    # Pages that yielded nothing -- no table, no fact -- and the page-loop
    # time they took: the size of the page-type-routing opportunity.
    with_output = ({f.provenance.page_number for f in result.facts if f.provenance}
                   | {t.page_number for t in result.tables})
    idle = [n for n in by_page if n not in with_output]
    idle_ms = sum(by_page[n] for n in idle)
    out = {
        "label": label, "pdf": pdf, "pages": pages, "facts": len(result.facts),
        "tables": len(result.tables), "status": str(result.status),
        "page_workers": settings.resolved_page_workers,
        "engine_load_s": round(engine_s, 2), "run_s": round(run_s, 2),
        "process_s": round(time.perf_counter() - t_start, 2),
        "pages_per_s": round(pages / run_s, 3) if run_s else None,
        "facts_per_s": round(len(result.facts) / run_s, 2) if run_s else None,
        "peak_working_set_mb": _peak_working_set_mb(),
        "ocr_cache": hits, "ocr_cache_hit_rate": (round(hits["hit"] / max(1, hits["hit"] + hits["miss"]), 3)),
        "second_read": sr, "stages": stages,
        "page_ms": {"p50": pct(50), "p90": pct(90), "p95": pct(95), "max": round(q[-1], 1) if q else None,
                    "n": len(q)},
        "pages_without_output": len(idle),
        "page_time_share_without_output": round(idle_ms / max(1.0, sum(by_page.values())), 3),
    }
    print("PERF_RESULT " + json.dumps(out, default=str), flush=True)


# -------------------------------------------------------------------- parent
def _resolve(spec: str) -> tuple[str, str]:
    """KEY (a corpus document) or LABEL=PATH."""
    if "=" in spec:
        label, path = spec.split("=", 1)
        return label, str(Path(path).resolve())
    from app.evaluation.corpus_runs import load_manifest
    doc = next((d for d in load_manifest() if d.key == spec), None)
    if doc is None:
        raise SystemExit(f"unknown document {spec!r}")
    return spec, str(doc.path)


def _available_memory_mb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return round(ms.ullAvailPhys / 2**20)
    except Exception:  # noqa: BLE001
        return None
    return None


def measure(pdf: str, label: str, cache_dir: Path, workers: int | None, mode: str) -> dict:
    env = dict(os.environ)
    # The OCR/second-read cache AND the page renders: a run that found last
    # run's renders would not be cold.
    env["FINSIGHT_CACHE_DIR"] = str(cache_dir)
    env["FINSIGHT_RENDER_DIR"] = str(cache_dir / "renders")
    env["FINSIGHT_LOG_LEVEL"] = "WARNING"
    if workers:
        env["FINSIGHT_PAGE_WORKERS"] = str(workers)
    avail = _available_memory_mb()
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, __file__, "--child", pdf, "--label", label],
                          cwd=str(BACKEND), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    wall = time.perf_counter() - t0
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PERF_RESULT ")), None)
    if line is None:
        return {"label": label, "mode": mode, "error": (proc.stderr or proc.stdout)[-2000:]}
    out = json.loads(line[len("PERF_RESULT "):])
    out.update({"mode": mode, "subprocess_wall_s": round(wall, 2),
                "available_memory_mb_at_start": avail, "cache_dir": str(cache_dir)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--label", help=argparse.SUPPRESS)
    ap.add_argument("--doc", action="append", default=[], help="KEY or LABEL=PATH")
    ap.add_argument("--knee", help="document for the parallelism sweep")
    ap.add_argument("--workers", default="", help="page workers (sweep: 1,2,4)")
    ap.add_argument("--modes", default="cold,warm")
    ap.add_argument("--rehome", action="store_true",
                    help="after cold: the same bytes under a NEW document id, with and "
                         "without the content-hash render store")
    ap.add_argument("--resume-after", type=float, default=None,
                    help="kill a cold run after this many seconds, then run again on its cache")
    ap.add_argument("--cache-root", default=None)
    ap.add_argument("--out", required=False)
    args = ap.parse_args()
    if args.child:
        _child(args.child, args.label or "doc")
        return 0

    root = Path(args.cache_root or tempfile.mkdtemp(prefix="finsight_perf_"))
    root.mkdir(parents=True, exist_ok=True)
    results = []
    modes = [m for m in args.modes.split(",") if m]
    prov = _provenance()
    print(json.dumps({"provenance": prov}), flush=True)

    def emit(r):
        results.append(r)
        brief = {k: r.get(k) for k in ("label", "mode", "page_workers", "pages", "facts", "run_s",
                                        "pages_per_s", "peak_working_set_mb", "ocr_cache_hit_rate")}
        print(json.dumps(brief), flush=True)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps({"cache_root": str(root), "provenance": prov,
                                                  "results": results},
                                                 indent=1, default=str), encoding="utf-8")

    if args.resume_after:
        # Checkpoint and resume: a run killed part-way, then run again. What
        # the first run finished is kept (the caches are written page by page
        # and cell by cell), so the second pays only for what was left.
        for spec in args.doc:
            label, pdf = _resolve(spec)
            cache = root / f"{label}_resume"
            if cache.exists():
                raise SystemExit(f"{cache} exists: a resume test needs an empty cache directory")
            env = dict(os.environ, FINSIGHT_CACHE_DIR=str(cache),
                       FINSIGHT_RENDER_DIR=str(cache / "renders"), FINSIGHT_LOG_LEVEL="WARNING")
            proc = subprocess.Popen([sys.executable, __file__, "--child", pdf, "--label", label],
                                    cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            time.sleep(args.resume_after)
            proc.kill()
            proc.wait()
            r = measure(pdf, label, cache, None, "resumed")
            r["killed_after_s"] = args.resume_after
            emit(r)
        return 0

    if args.knee:
        label, pdf = _resolve(args.knee)
        for w in [int(x) for x in args.workers.split(",") if x]:
            cache = root / f"{label}_w{w}"
            if cache.exists():
                raise SystemExit(f"{cache} exists: a cold run needs an empty cache directory")
            emit(measure(pdf, label, cache, w, "cold"))
    else:
        workers = int(args.workers) if args.workers else None
        for spec in args.doc:
            label, pdf = _resolve(spec)
            cache = root / label
            if "cold" in modes and cache.exists():
                raise SystemExit(f"{cache} exists: a cold run needs an empty cache directory")
            for mode in modes:
                emit(measure(pdf, label, cache, workers, mode))
            if args.rehome:
                # The same bytes under a new document id: OCR and second-read
                # caches hit by content hash; renders hit only through the
                # content-hash store. Measured with the store, then without it
                # (our own temporary directory's store removed).
                import shutil
                emit(measure(pdf, f"{label}_newid1", cache, workers, "new-id, render store"))
                shutil.rmtree(cache / "renders" / "_by_content", ignore_errors=True)
                emit(measure(pdf, f"{label}_newid2", cache, workers, "new-id, no render store"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
