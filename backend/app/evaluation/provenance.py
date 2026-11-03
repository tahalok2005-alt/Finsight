"""What an evaluation run was made from, so a scorecard can never mix states.

Phase 21 lost a cycle to this: ``corpus_run.py`` skipped documents already
recorded for a tag, a run stopped midway was relaunched under the same tag on
changed code, and one scorecard ended up describing three code versions. The
fix is not a convention but a record: every run carries

* ``code`` -- a fingerprint of every source file that can change an output
  (the backend package and the corpus scripts), plus the OCR engine and model
  versions;
* ``config`` -- a hash of every processing setting;
* ``git`` -- the commit and whether the tree was dirty (the fingerprint, not
  the commit, is what decides sameness: most of this project's history is
  uncommitted work);
* the corpus manifest and golden set it was measured against;

and a tag is refused if its records disagree on any of them.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"

#: Settings that describe where things are, or how the work is scheduled,
#: rather than what is computed. They are recorded (``runtime``) but kept out
#: of the configuration hash: a worker count must never change an output, and
#: a performance experiment that changes one is a finding, not a new config.
_NON_SEMANTIC_SETTINGS = {
    "database_url", "data_dir", "upload_dir", "cache_dir", "render_dir",
    "export_dir", "log_level", "log_json", "cors_origins", "api_host", "api_port",
    "api_prefix", "app_name", "app_version", "secret_key", "environment", "db_echo",
    "page_workers", "min_page_workers", "worker_threads", "ocr_intra_threads",
    "page_worker_memory_mb", "slow_page_ms", "delete_cancel_timeout_s",
    "second_read_workers",
    "thumbnail_width", "max_upload_mb", "allowed_extensions"}


def _sha_files(paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda q: q.relative_to(root).as_posix()):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version(name)
    except Exception:  # noqa: BLE001 - not installed
        return "absent"


@lru_cache(maxsize=1)
def code_fingerprint() -> dict:
    """A hash of everything that can change an extraction, and its parts."""
    sources = [p for p in (BACKEND / "app").rglob("*")
               if p.is_file() and "__pycache__" not in p.parts
               and p.suffix in (".py", ".json", ".yaml", ".yml", ".txt", ".csv")]
    scripts = [p for p in (BACKEND / "scripts").glob("corpus_*.py")]
    engines = {name: _package_version(name) for name in
               ("rapidocr-onnxruntime", "onnxruntime", "pymupdf", "numpy", "opencv-python")}
    app_sha = _sha_files(sources, BACKEND)
    scripts_sha = _sha_files(scripts, BACKEND) if scripts else ""
    whole = hashlib.sha256(json.dumps(
        {"app": app_sha, "scripts": scripts_sha, "engines": engines},
        sort_keys=True).encode()).hexdigest()
    return {"fingerprint": whole[:16], "app_sha256": app_sha,
            "scripts_sha256": scripts_sha, "files": len(sources),
            "engines": engines, "python": platform.python_version()}


def config_snapshot() -> dict:
    """Every processing setting, and a hash of them."""
    from app.config.settings import settings  # noqa: PLC0415
    raw = settings.model_dump(mode="json")
    semantic = {k: v for k, v in sorted(raw.items()) if k not in _NON_SEMANTIC_SETTINGS}
    runtime = {k: raw[k] for k in sorted(raw) if k in _NON_SEMANTIC_SETTINGS
               and k in ("page_workers", "min_page_workers", "worker_threads",
                         "ocr_intra_threads", "page_worker_memory_mb")}
    digest = hashlib.sha256(json.dumps(semantic, sort_keys=True, default=str)
                            .encode()).hexdigest()[:16]
    return {"hash": digest, "settings": semantic, "runtime": runtime}


def git_state() -> dict:
    def run(*args) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                  text=True, timeout=20).stdout.strip()
        except Exception:  # noqa: BLE001 - git absent
            return ""
    head = run("rev-parse", "HEAD")
    dirty = run("status", "--porcelain", "--", "backend")
    return {"head": head[:12], "dirty_files": len(dirty.splitlines()) if dirty else 0}


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def golden_fingerprint(directory: Path) -> str:
    files = sorted(directory.glob("*.json"))
    return _sha_files(files, directory)[:16] if files else ""


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def same_state(a: dict, b: dict) -> list[str]:
    """What differs between two recorded states (empty if they are the same)."""
    out = []
    if (a.get("code") or {}).get("fingerprint") != (b.get("code") or {}).get("fingerprint"):
        out.append("code")
    if (a.get("config") or {}).get("hash") != (b.get("config") or {}).get("hash"):
        out.append("config")
    if a.get("manifest_sha256") != b.get("manifest_sha256"):
        out.append("corpus manifest")
    return out
