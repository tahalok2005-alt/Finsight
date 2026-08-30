"""Deterministic, collision-resistant identifiers.

Entity ids are derived from their natural key so that re-processing the same
document with the same settings produces stable ids -- important for provenance
links that may be exported and cited externally.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Iterable

_HASH_CHUNK = 1024 * 1024


def new_uuid() -> str:
    return uuid.uuid4().hex


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    """A short deterministic id: prefix + 12 hex chars of sha256(parts)."""
    raw = "|".join(str(p) for p in parts)
    return prefix + "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def content_signature(parts: Iterable[object]) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
