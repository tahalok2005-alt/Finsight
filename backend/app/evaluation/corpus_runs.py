"""The real-world corpus: run once, keep the evidence, score from the record.

A document is processed through the normal pipeline exactly once per code
state, and everything the scoring needs -- facts, tables, validations, stage
timings -- is written to ``data/corpus/runs/<KEY>.<tag>.json``. Scoring then
reads that record. Re-running a 70-page presentation to ask a new question of
it would cost minutes and could not produce a different answer; replaying the
record costs milliseconds and produces the same one.

A record carries the sha256 of the file it was made from. A record whose file
has since changed is refused rather than scored, because golden facts are
transcribed against a specific file.

It also carries the run it belongs to (``provenance``): the run id, the code
fingerprint and the configuration hash (see ``app.evaluation.provenance``).
A tag is one run on one code state; ``scripts/corpus_run.py`` refuses to add to
a tag made from anything else, and the scorer refuses to score a tag whose
records disagree.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.ids import sha256_file
from app.core.logging import get_logger
from app.core.modes import ProcessingMode
from app.models.schemas import ExtractedTable, FinancialFact, ValidationResult

log = get_logger("evaluation.corpus_runs")

ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = ROOT / "data" / "corpus"
RUNS_DIR = CORPUS_DIR / "runs"
MANIFEST = CORPUS_DIR / "manifest.json"
EVALUATIONS_DIR = CORPUS_DIR / "evaluations"


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    key: str
    company: str
    sector: str
    path: Path
    sha256: str
    pages: int
    meta: dict


def load_manifest(path: Path = MANIFEST) -> list[CorpusDocument]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    docs = []
    for d in raw["documents"]:
        docs.append(CorpusDocument(
            key=d["key"], company=d["company"], sector=d["sector"],
            path=(path.parent / d["file"]).resolve(), sha256=d["sha256"],
            pages=d["pages"], meta=d))
    return docs


@dataclass
class RunRecord:
    """What one pipeline run produced, in a form that survives the process."""
    key: str
    tag: str
    sha256: str
    mode: str
    pages: int
    wall_seconds: float
    stages: list[dict]
    facts: list[FinancialFact] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    validations: list[ValidationResult] = field(default_factory=list)
    status: str = ""
    error: str | None = None
    #: The run this record belongs to: run_id, code fingerprint, config hash.
    provenance: dict = field(default_factory=dict)

    # ---- timing, as the brief asks for it (section 27) --------------------
    def stage_seconds(self) -> dict[str, float]:
        """Seconds per stage. The page phases run interleaved in one parallel
        loop, so for those the time actually spent in each phase is reported
        (summed across workers) rather than a wall clock they share."""
        out: dict[str, float] = {}
        for s in self.stages:
            phase = (s.get("detail") or {}).get("phase_seconds")
            if phase is not None:
                out[s["stage"]] = float(phase)
            elif s.get("duration_ms") is not None:
                out[s["stage"]] = round(float(s["duration_ms"]) / 1000.0, 2)
        return out

    def to_json(self) -> dict:
        return {
            "key": self.key, "tag": self.tag, "sha256": self.sha256,
            "mode": self.mode, "pages": self.pages,
            "wall_seconds": self.wall_seconds, "status": self.status,
            "error": self.error, "stages": self.stages,
            "provenance": self.provenance,
            "facts": [f.model_dump(mode="json") for f in self.facts],
            "tables": [t.model_dump(mode="json") for t in self.tables],
            "validations": [v.model_dump(mode="json") for v in self.validations],
        }

    @classmethod
    def from_json(cls, raw: dict) -> "RunRecord":
        return cls(
            key=raw["key"], tag=raw["tag"], sha256=raw["sha256"],
            mode=raw["mode"], pages=raw["pages"],
            wall_seconds=raw["wall_seconds"], stages=raw["stages"],
            facts=[FinancialFact.model_validate(f) for f in raw["facts"]],
            tables=[ExtractedTable.model_validate(t) for t in raw["tables"]],
            validations=[ValidationResult.model_validate(v)
                         for v in raw.get("validations", [])],
            status=raw.get("status", ""), error=raw.get("error"),
            provenance=raw.get("provenance") or {})


def record_path(key: str, tag: str) -> Path:
    return RUNS_DIR / f"{key}.{tag}.json"


def run_document(doc: CorpusDocument, *, tag: str,
                 mode: ProcessingMode = ProcessingMode.VISUAL_ONLY,
                 provenance: dict | None = None) -> RunRecord:
    """Process one corpus document through the ordinary pipeline and record it.

    The pipeline is not told it is being evaluated. Nothing about the document
    beyond its bytes reaches it -- not its key, not its company, and certainly
    not the golden facts -- so a rule cannot be fitted to a filer by accident.
    """
    from app.services.pipeline import Pipeline

    actual = sha256_file(doc.path)
    if actual != doc.sha256:
        raise ValueError(f"{doc.key}: file hash {actual[:12]} does not match "
                         f"the manifest's {doc.sha256[:12]}")
    t0 = time.perf_counter()
    result = Pipeline(document_id=f"corpus_{doc.key}_{tag}", pdf_path=doc.path,
                      content_hash=actual, mode=mode).run()
    wall = round(time.perf_counter() - t0, 2)
    rec = RunRecord(
        key=doc.key, tag=tag, sha256=actual, mode=str(mode),
        pages=len(result.pages), wall_seconds=wall,
        stages=[s.model_dump(mode="json") for s in result.stages],
        facts=list(result.facts), tables=list(result.tables),
        validations=list(result.validations),
        status=str(result.status), error=result.error,
        provenance=dict(provenance or {}))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    record_path(doc.key, tag).write_text(
        json.dumps(rec.to_json(), ensure_ascii=False), encoding="utf-8")
    log.info("corpus.run", key=doc.key, tag=tag, pages=rec.pages,
             facts=len(rec.facts), tables=len(rec.tables), seconds=wall)
    return rec


def load_record(key: str, tag: str, *, expect_sha256: str | None = None) -> RunRecord:
    raw = json.loads(record_path(key, tag).read_text(encoding="utf-8"))
    rec = RunRecord.from_json(raw)
    if expect_sha256 and rec.sha256 != expect_sha256:
        raise ValueError(f"{key}.{tag}: recorded against a different file")
    return rec
