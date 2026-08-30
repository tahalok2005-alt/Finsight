"""On-disk cache for page OCR results.

OCR is by far the most expensive stage (~13 s/page on this CPU), so re-running a
document -- after a code change downstream, or via the reprocess endpoint --
must not repeat it unnecessarily.

The cache key binds the result to everything that could change it: the *content
hash* of the PDF, the page, the render signature (dpi/format) and the OCR
signature (engine plus its detection parameters).  Changing any of them yields a
different key, so a stale result can never be served for a new configuration.

Keying on content rather than document id is deliberate: identical pixels always
produce identical OCR, so re-ingesting the same file -- or reprocessing it under
a new id -- reuses the work instead of repeating several minutes of inference.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.config.settings import settings
from app.core.logging import get_logger
from app.models.schemas import PageOCRResult

log = get_logger("ocr.cache")

CACHE_VERSION = "v1"
_SAFE_SCOPE = re.compile(r"[^A-Za-z0-9_-]+")


def _dir(cache_scope: str) -> Path:
    scope = _SAFE_SCOPE.sub("_", cache_scope)[:80] or "default"
    root = settings.cache_dir.resolve()
    d = (root / "ocr" / scope).resolve()
    if not str(d).startswith(str(root)):
        raise ValueError("Invalid OCR cache path")
    d.mkdir(parents=True, exist_ok=True)
    return d


def key(page_number: int, render_signature: str, ocr_signature: str) -> str:
    raw = f"{CACHE_VERSION}-p{page_number:04d}-{render_signature}-{ocr_signature}"
    return _SAFE_SCOPE.sub("_", raw)


def path_for(cache_scope: str, page_number: int, render_signature: str,
             ocr_signature: str) -> Path:
    return _dir(cache_scope) / f"{key(page_number, render_signature, ocr_signature)}.json"


def load(cache_scope: str, page_number: int, render_signature: str,
         ocr_signature: str) -> PageOCRResult | None:
    p = path_for(cache_scope, page_number, render_signature, ocr_signature)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return PageOCRResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - a bad cache entry is not fatal
        log.warning("ocr.cache_unreadable", page=page_number, path=str(p),
                    error=str(exc))
        try:
            p.unlink()
        except OSError:
            pass
        return None


def store(cache_scope: str, page_number: int, render_signature: str,
          ocr_signature: str, result: PageOCRResult) -> None:
    p = path_for(cache_scope, page_number, render_signature, ocr_signature)
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(result.model_dump_json(), encoding="utf-8")
        tmp.replace(p)  # atomic: readers never see a half-written entry
    except Exception as exc:  # noqa: BLE001 - caching must never break the run
        log.warning("ocr.cache_write_failed", page=page_number, error=str(exc))
        tmp.unlink(missing_ok=True)


def clear(cache_scope: str) -> int:
    """Drop every cached page for one scope.  Returns the number removed."""
    d = _dir(cache_scope)
    n = 0
    for f in d.glob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
