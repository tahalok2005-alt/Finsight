"""The benchmark corpus: which real documents this system is measured on.

Two kinds of document sit here and they are kept apart on purpose.

**Keyed documents** have a ground-truth answer file beside them, so precision,
recall and F1 mean something. There are four, and they are the regression gate:
a change that moves one of those numbers has to be explained before it ships.

**Corpus documents** have no answer key. They are real reports that exercise
shapes the keyed set does not -- a statutory bank filing, an earnings release
built out of KPI tiles, a limited-review filing under a legal name nobody would
recognise. What they measure is not accuracy but *behaviour*: how long the
document takes, how much of what was extracted is fully placed in time and
attributed, how much needed a person. A count from one of these is evidence
about the pipeline, never evidence that a figure is right.

Nothing here copies or modifies a source file. A document is read from wherever
it lives, and a missing one is reported rather than skipped silently -- a
benchmark that quietly shrinks is worse than one that fails.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: Where the manifest lives. Paths inside it may be absolute or relative to it.
DEFAULT_MANIFEST = Path(__file__).resolve().parents[3] / "data" / "benchmark" / "corpus.json"


@dataclass(slots=True)
class CorpusDocument:
    """One document the system is measured on."""
    key: str
    path: Path
    #: What kind of report this is, in the words a person would use.
    kind: str
    #: Structures it is known to contain, for reading a result in context.
    structures: list[str] = field(default_factory=list)
    #: Ground-truth answer file, where one exists.
    truth: Path | None = None
    notes: str = ""

    @property
    def has_key(self) -> bool:
        return self.truth is not None and self.truth.exists()

    @property
    def exists(self) -> bool:
        return self.path.exists()


def load(manifest: Path | None = None) -> list[CorpusDocument]:
    """Read the manifest. Raises if it is missing -- silence is not an answer."""
    path = manifest or DEFAULT_MANIFEST
    data = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    out: list[CorpusDocument] = []
    for entry in data["documents"]:
        p = Path(entry["path"])
        if not p.is_absolute():
            p = (base / p).resolve()
        truth = entry.get("truth")
        tp = None
        if truth:
            tp = Path(truth)
            if not tp.is_absolute():
                tp = (base / tp).resolve()
        out.append(CorpusDocument(
            key=entry["key"], path=p, kind=entry.get("kind", "unknown"),
            structures=list(entry.get("structures", [])),
            truth=tp, notes=entry.get("notes", "")))
    return out


def missing(documents: list[CorpusDocument]) -> list[CorpusDocument]:
    return [d for d in documents if not d.exists]
