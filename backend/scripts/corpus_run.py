"""Process the corpus once and record each run for replay -- one tag, one state.

    cd backend
    ../.venv/Scripts/python.exe scripts/corpus_run.py --tag p22a
    ../.venv/Scripts/python.exe scripts/corpus_run.py --tag p22a --resume
    ../.venv/Scripts/python.exe scripts/corpus_run.py --tag p22a --force

A tag means a FRESH evaluation: one run id, one code fingerprint, one
configuration (``app.evaluation.provenance``). Phase 21 mixed three code
versions under one tag because a relaunched run skipped documents already
recorded; that is now impossible:

* a tag with records is refused, unless
* ``--resume`` is given and the tag's recorded state (code, config, corpus
  manifest) is identical to the current one -- documents already recorded
  by that same run are reused and SAID to be reused -- or
* ``--force`` is given, which discards the tag's records and starts again.

The run is written to ``data/corpus/evaluations/<tag>.json`` as it goes:
run id, state, and for each document its file hash, record hash, facts and
timing. Documents are processed one at a time, never concurrently, so the
per-document timings describe the document and not whatever else the machine
was doing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.corpus_runs import (  # noqa: E402
    EVALUATIONS_DIR, MANIFEST, RUNS_DIR, load_manifest, record_path, run_document,
)
from app.evaluation.provenance import (  # noqa: E402
    code_fingerprint, config_snapshot, file_sha, git_state, new_run_id, now, same_state,
)


def _state() -> dict:
    return {"code": code_fingerprint(), "config": config_snapshot(),
            "git": git_state(), "manifest_sha256": file_sha(MANIFEST)}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="document keys (default: all)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--resume", action="store_true",
                    help="complete a tag started on this exact code and config")
    ap.add_argument("--force", action="store_true",
                    help="discard the tag's records and run it again")
    args = ap.parse_args()

    docs = load_manifest()
    if args.keys:
        wanted = set(args.keys)
        docs = [d for d in docs if d.key in wanted]
        unknown = wanted - {d.key for d in docs}
        if unknown:
            print(f"unknown keys: {sorted(unknown)}")
            return 2

    state = _state()
    eval_path = EVALUATIONS_DIR / f"{args.tag}.json"
    existing = sorted(RUNS_DIR.glob(f"*.{args.tag}.json"))
    if (existing or eval_path.exists()) and args.force:
        for p in existing:
            p.unlink()
        eval_path.unlink(missing_ok=True)
        print(f"FORCE: discarded {len(existing)} records of tag {args.tag}")
        existing = []
    if existing or eval_path.exists():
        if not args.resume:
            print(f"REFUSED: tag {args.tag!r} already has {len(existing)} records. A tag is "
                  f"one fresh run: use a new tag, --resume to complete this run on the "
                  f"same code, or --force to discard it.")
            return 3
        if not eval_path.exists():
            print(f"REFUSED: tag {args.tag!r} has records but no run manifest "
                  f"(made before Phase 22); it cannot be resumed.")
            return 3
        evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
        diff = same_state(evaluation, state)
        if diff:
            print(f"REFUSED: tag {args.tag!r} was run on a different state "
                  f"({', '.join(diff)}); resuming would mix versions. Use a new tag.")
            return 3
        print(f"RESUME run {evaluation['run_id']} (code {state['code']['fingerprint']})")
    else:
        evaluation = {"run_id": new_run_id(), "tag": args.tag, "started_at": now(),
                      **state, "documents": {}, "status": "running"}
        _write(eval_path, evaluation)
        print(f"RUN id {evaluation['run_id']} code {state['code']['fingerprint']} "
              f"config {state['config']['hash']} git {state['git']['head']}"
              f"{' (dirty)' if state['git']['dirty_files'] else ''}")

    provenance = {"run_id": evaluation["run_id"],
                  "code_fingerprint": state["code"]["fingerprint"],
                  "config_hash": state["config"]["hash"]}
    t_all = time.perf_counter()
    for doc in docs:
        path = record_path(doc.key, args.tag)
        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            if (rec.get("provenance") or {}).get("run_id") == evaluation["run_id"]:
                print(f"REUSE {doc.key}: recorded by this run", flush=True)
                continue
            print(f"REFUSED: {path.name} belongs to another run", flush=True)
            return 3
        print(f"RUN  {doc.key} ({doc.pages} pages)", flush=True)
        try:
            rec = run_document(doc, tag=args.tag, provenance=provenance)
        except Exception as exc:  # recorded, never swallowed silently
            print(f"FAIL {doc.key}: {type(exc).__name__}: {exc}", flush=True)
            evaluation["documents"][doc.key] = {"error": f"{type(exc).__name__}: {exc}"}
            _write(eval_path, evaluation)
            continue
        st = rec.stage_seconds()
        evaluation["documents"][doc.key] = {
            "file": str(doc.path.name), "sha256": doc.sha256, "pages": rec.pages,
            "record": path.name, "record_sha256": file_sha(path),
            "facts": len(rec.facts), "wall_seconds": rec.wall_seconds,
            "status": rec.status, "finished_at": now()}
        _write(eval_path, evaluation)
        print(f"DONE {doc.key}: {rec.pages}p {len(rec.facts)} facts "
              f"{len(rec.tables)} tables {rec.wall_seconds:.0f}s  "
              f"ocr={st.get('ocr', 0):.0f}s tables={st.get('tables', 0):.0f}s "
              f"extract={st.get('extraction', 0):.0f}s status={rec.status}",
              flush=True)
    all_keys = {d.key for d in load_manifest()}
    done = {k for k, v in evaluation["documents"].items() if "record" in v}
    evaluation["status"] = "complete" if all_keys <= done else "partial"
    evaluation["finished_at"] = now()
    _write(eval_path, evaluation)
    print(f"ALL DONE in {time.perf_counter() - t_all:.0f}s  run {evaluation['run_id']} "
          f"{evaluation['status']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
