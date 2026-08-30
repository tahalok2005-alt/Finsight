"""Pipeline orchestrator.

Runs the full document → structured data path, persisting incrementally so a
crash halfway through leaves a truthful partial record rather than nothing:

    validate → render → OCR → layout → assemble → tables → charts
             → extraction → normalisation → validation → confidence → persist

Honest status reporting is a hard requirement.  A document reaches ``COMPLETED``
only when every page was processed and no stage failed; otherwise it lands on
``PARTIALLY_COMPLETED``, ``REQUIRES_REVIEW`` or ``FAILED`` with the reason
recorded on the run.
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable

from app.charts.detector import detect_charts
from app.config.settings import settings
from app.core.errors import FinSightError, StageFailure
from app.core.ids import new_uuid, stable_id
from app.core.logging import get_logger, stage_timer
from app.document import assembly
from app.extraction import confidence as conf_mod
from app.extraction.facts import TableFactContext, deduplicate, extract_from_table
from app.extraction.narrative import (
    NarrativeContext, extract_commentary, extract_guidance,
    extract_narrative_facts, is_forward_looking,
)
from app.ingestion.validator import PdfProbe, probe_pdf
from app.layout.cv_signals import page_signals
from app.layout.detector import HeuristicLayoutDetector
from app.models.enums import (
    Currency, DocumentStatus, MagnitudeUnit, RegionType, StageStatus,
    StatementNature, ValidationStatus,
)
from app.models.schemas import (
    Commentary, ExtractedChart, ExtractedTable, FinancialFact, Guidance,
    OCRElement, PageInfo, Region, StageRecord, ValidationResult,
)
from app.ocr import cache as ocr_cache
from app.ocr import textlayer
from app.ocr.lines import TextLine, group_lines
from app.ocr.registry import get_engine
from app.provenance import service as prov_service
from app.rendering.renderer import PageRenderer, RenderedPage
from app.tables.structure import build_table
from app.validation.engine import validate

log = get_logger("pipeline")

ProgressFn = Callable[[str, StageStatus, int, int, dict], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class PageArtifacts:
    page: PageInfo
    regions: list[Region] = field(default_factory=list)
    elements: list[OCRElement] = field(default_factory=list)
    lines: list[TextLine] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    charts: list[ExtractedChart] = field(default_factory=list)
    error: str | None = None
    signals_v_lines: list = field(default_factory=list)


@dataclass(slots=True)
class PipelineResult:
    document_id: str
    status: DocumentStatus
    probe: PdfProbe | None
    pages: list[PageInfo] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    elements: list[OCRElement] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    charts: list[ExtractedChart] = field(default_factory=list)
    facts: list[FinancialFact] = field(default_factory=list)
    commentary: list[Commentary] = field(default_factory=list)
    guidance: list[Guidance] = field(default_factory=list)
    validations: list[ValidationResult] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def pages_failed(self) -> int:
        return sum(1 for p in self.pages if p.status == StageStatus.FAILED)

    @property
    def pages_done(self) -> int:
        return sum(1 for p in self.pages if p.status == StageStatus.SUCCEEDED)


class Pipeline:
    """Executes the full document pipeline for one document."""

    def __init__(self, document_id: str, pdf_path: str | Path, *,
                 progress: ProgressFn | None = None,
                 page_workers: int | None = None,
                 force_ocr: bool = False,
                 content_hash: str | None = None):
        self.document_id = document_id
        self.force_ocr = force_ocr
        # OCR results are cached against the file's content, not its id
        self.cache_scope = content_hash or document_id
        self.pdf_path = Path(pdf_path)
        self.progress = progress or (lambda *a, **k: None)
        self.page_workers = max(1, page_workers or settings.page_workers)
        self.detector = HeuristicLayoutDetector()
        self._stages: list[StageRecord] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- utilities
    def _stage(self, name: str, status: StageStatus, *, total: int = 0,
               done: int = 0, failed: int = 0, engine: str | None = None,
               duration_ms: float | None = None, error: str | None = None,
               detail: dict | None = None) -> StageRecord:
        rec = StageRecord(
            stage=name, status=status, items_total=total, items_done=done,
            items_failed=failed, engine=engine, duration_ms=duration_ms,
            error=error, detail=detail or {}, finished_at=_utcnow())
        with self._lock:
            self._stages = [s for s in self._stages if s.stage != name]
            self._stages.append(rec)
        self.progress(name, status, done, total, rec.detail)
        return rec

    # ------------------------------------------------------------------- run
    def run(self) -> PipelineResult:
        t_start = perf_counter()
        result = PipelineResult(document_id=self.document_id,
                                status=DocumentStatus.FAILED, probe=None)
        try:
            probe = self._validate()
            result.probe = probe

            renderer = PageRenderer(self.document_id, self.pdf_path)
            try:
                artifacts = self._process_pages(renderer, probe)
            finally:
                renderer.close()

            result.pages = [a.page for a in artifacts]
            result.elements = [e for a in artifacts for e in a.elements]
            result.regions = [r for a in artifacts for r in a.regions]

            if not any(a.page.status == StageStatus.SUCCEEDED for a in artifacts):
                raise StageFailure(
                    "ocr", "No page could be processed; the document yielded no "
                           "readable content.")

            model = self._assemble(artifacts)
            result.metadata = {
                "title": model.title, "company_name": model.company_name,
                "report_period": model.report_period,
                "report_type": model.report_type,
                "currency": model.currency, "unit": model.unit,
                "fiscal_year_end_month": model.calendar.year_end_month,
                "sections": [
                    {"id": s.id, "title": s.title, "entity": s.entity,
                     "start_page": s.start_page, "end_page": s.end_page}
                    for s in model.sections],
            }

            self._build_tables(artifacts, model)
            result.tables = [t for a in artifacts for t in a.tables]
            assembly.link_tables(model, result.tables)
            assembly.link_continuations(result.tables)

            self._build_charts(artifacts)
            result.charts = [c for a in artifacts for c in a.charts]

            facts, commentary, guidance = self._extract(artifacts, model)
            result.commentary = commentary
            result.guidance = guidance

            report = self._validate_facts(facts)
            result.validations = report.results

            self._finalise_confidence(facts, report.fact_status)
            result.facts = facts

            self._attach_provenance(result)

            result.status = self._final_status(result)
            result.stages = list(self._stages)
            result.duration_ms = round((perf_counter() - t_start) * 1000, 1)
            log.info("pipeline.finished", document_id=self.document_id,
                     status=str(result.status), pages=len(result.pages),
                     facts=len(result.facts), tables=len(result.tables),
                     charts=len(result.charts),
                     validations=len(result.validations),
                     duration_ms=result.duration_ms)
            return result

        except FinSightError as exc:
            result.error = exc.message
            result.status = DocumentStatus.FAILED
            result.stages = list(self._stages)
            result.duration_ms = round((perf_counter() - t_start) * 1000, 1)
            log.error("pipeline.failed", document_id=self.document_id,
                      error=exc.message, code=exc.code)
            return result
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = DocumentStatus.FAILED
            result.stages = list(self._stages)
            result.duration_ms = round((perf_counter() - t_start) * 1000, 1)
            log.exception("pipeline.crashed", document_id=self.document_id,
                          error=str(exc),
                          traceback=traceback.format_exc(limit=6))
            return result

    # -------------------------------------------------------------- stage 1
    def _validate(self) -> PdfProbe:
        with stage_timer(log, "validating", document_id=self.document_id):
            self._stage("validating", StageStatus.RUNNING)
            probe = probe_pdf(self.pdf_path)
            self._stage("validating", StageStatus.SUCCEEDED, total=1, done=1,
                        detail={"page_count": probe.page_count,
                                "is_scanned": probe.is_scanned,
                                "text_layer_chars": probe.text_layer_chars,
                                "warnings": probe.warnings[:10]})
        return probe

    # ------------------------------------------- stages 2-4 (per-page, parallel)
    def _process_pages(self, renderer: PageRenderer,
                       probe: PdfProbe) -> list[PageArtifacts]:
        total = probe.page_count
        engine = get_engine()
        engine.warmup()

        for name in ("rendering", "ocr", "layout"):
            self._stage(name, StageStatus.RUNNING, total=total)

        done = {"rendering": 0, "ocr": 0, "layout": 0}
        failed = 0
        artifacts: list[PageArtifacts] = []

        def work(page_number: int) -> PageArtifacts:
            return self._process_one_page(renderer, engine, page_number, probe)

        # ONNX Runtime releases the GIL during inference, so threads give real
        # parallelism here without the memory cost of extra processes.
        with ThreadPoolExecutor(max_workers=self.page_workers,
                                thread_name_prefix="page") as pool:
            for art in pool.map(work, range(1, total + 1)):
                artifacts.append(art)
                if art.page.status == StageStatus.FAILED:
                    failed += 1
                else:
                    for k in done:
                        done[k] += 1
                self._stage("rendering", StageStatus.RUNNING,
                            total=total, done=done["rendering"], failed=failed)
                self._stage("ocr", StageStatus.RUNNING, total=total,
                            done=done["ocr"], failed=failed,
                            engine=engine.info.name)
                self._stage("layout", StageStatus.RUNNING, total=total,
                            done=done["layout"], failed=failed,
                            engine=self.detector.name)

        artifacts.sort(key=lambda a: a.page.page_number)
        final = (StageStatus.SUCCEEDED if failed == 0
                 else (StageStatus.FAILED if failed == total else StageStatus.PARTIAL))
        for name in ("rendering", "ocr", "layout"):
            self._stage(name, final, total=total, done=done[name], failed=failed,
                        engine=engine.info.name if name == "ocr" else None)
        return artifacts

    def _process_one_page(self, renderer: PageRenderer, engine,
                          page_number: int, probe: PdfProbe) -> PageArtifacts:
        page_id = stable_id("page", self.document_id, page_number)
        info = PageInfo(
            id=page_id, document_id=self.document_id, page_number=page_number,
            pdf_width=0.0, pdf_height=0.0, render_width=0, render_height=0,
            dpi=settings.render_dpi, status=StageStatus.RUNNING)
        art = PageArtifacts(page=info)
        try:
            rendered: RenderedPage = renderer.render_page(page_number)
            info.pdf_width = rendered.pdf_width
            info.pdf_height = rendered.pdf_height
            info.render_width = rendered.width
            info.render_height = rendered.height
            info.dpi = rendered.dpi
            info.rotation = rendered.rotation
            info.image_url = prov_service.page_image_url(self.document_id, page_number)
            info.thumbnail_url = (
                f"{settings.api_prefix}/documents/{self.document_id}"
                f"/pages/{page_number}/thumbnail" if rendered.thumbnail_path else None)

            image = renderer.load_image_array(page_number)

            render_sig = f"dpi{rendered.dpi}-{settings.render_format}"
            ocr_sig = settings.ocr_signature
            ocr_result = (None if self.force_ocr else
                          ocr_cache.load(self.cache_scope, page_number,
                                         render_sig, ocr_sig))
            if ocr_result is None:
                ocr_result = engine.recognize(image, document_id=self.document_id,
                                              page_number=page_number)
                ocr_cache.store(self.cache_scope, page_number, render_sig,
                                ocr_sig, ocr_result)
            else:
                log.debug("ocr.cache_hit", document_id=self.document_id,
                          page_number=page_number)
            info.ocr_engine = ocr_result.engine
            info.ocr_signature = ocr_sig
            info.ocr_duration_ms = ocr_result.duration_ms
            art.elements = ocr_result.elements
            info.ocr_element_count = len(ocr_result.elements)
            info.ocr_mean_confidence = ocr_result.mean_confidence

            # auxiliary channel: corroborate against the PDF text layer
            words = textlayer.extract_words(self.pdf_path, page_number,
                                            rendered.scale)
            info.has_text_layer = bool(words)
            info.text_layer_chars = sum(len(w.text) for w in words)
            info.is_scanned = not info.has_text_layer
            if words:
                textlayer.corroborate(art.elements, words)

            art.lines = group_lines(art.elements, document_id=self.document_id,
                                    page_number=page_number)
            for idx, el in enumerate(art.elements):
                el.reading_index = idx

            signals = page_signals(image, [e.bbox.geom for e in art.elements])
            art.signals_v_lines = signals.v_lines
            art.regions = self.detector.detect(
                document_id=self.document_id, page_number=page_number,
                lines=art.lines, signals=signals, elements=art.elements)
            info.region_count = len(art.regions)

            # link each OCR element to the region that contains it
            region_of: dict[str, str] = {}
            for r in art.regions:
                for eid in r.element_ids:
                    region_of.setdefault(eid, r.id)
            for el in art.elements:
                el.block_id = region_of.get(el.id)

            art.page.status = StageStatus.SUCCEEDED
            del image
        except Exception as exc:  # noqa: BLE001 - a bad page must not stop the run
            art.error = f"{type(exc).__name__}: {exc}"
            info.status = StageStatus.FAILED
            info.error = art.error
            log.exception("pipeline.page_failed", document_id=self.document_id,
                          page_number=page_number, error=str(exc))
        return art

    # -------------------------------------------------------------- stage 5
    def _assemble(self, artifacts: list[PageArtifacts]):
        with stage_timer(log, "document_assembly", document_id=self.document_id):
            self._stage("document_assembly", StageStatus.RUNNING)
            regions_by_page = {a.page.page_number: a.regions for a in artifacts
                               if a.page.status != StageStatus.FAILED}
            heights = {a.page.page_number: a.page.render_height for a in artifacts}
            model = assembly.assemble(self.document_id, regions_by_page, heights)
            for a in artifacts:
                sec = next((s for s in model.sections
                            if s.start_page <= a.page.page_number <= s.end_page), None)
                if sec:
                    a.page.id = a.page.id  # unchanged; section carried below
            self._stage("document_assembly", StageStatus.SUCCEEDED, total=1, done=1,
                        detail={"sections": len(model.sections),
                                "chrome_regions": len(model.chrome_region_ids),
                                "company": model.company_name,
                                "report_period": model.report_period})
        return model

    # -------------------------------------------------------------- stage 6
    def _build_tables(self, artifacts: list[PageArtifacts], model) -> None:
        total = sum(1 for a in artifacts for r in a.regions
                    if r.type == RegionType.TABLE)
        self._stage("tables", StageStatus.RUNNING, total=total)
        built = 0
        rejected = 0
        with stage_timer(log, "tables", document_id=self.document_id):
            for art in artifacts:
                if art.page.status == StageStatus.FAILED:
                    continue
                by_line = {ln.id: ln for ln in art.lines}
                idx = 0
                ordered = sorted(art.regions, key=lambda r: r.reading_order)
                for region in ordered:
                    if region.type != RegionType.TABLE:
                        continue
                    # Repeated page furniture (the registered-office block) is
                    # column-aligned and full of numbers, so it survives band
                    # detection; cross-page chrome analysis is what rules it out.
                    if region.id in model.chrome_region_ids:
                        region.type = RegionType.FOOTER
                        region.meta["table_rejected"] = "page chrome"
                        rejected += 1
                        continue
                    lines = [by_line[i] for i in region.meta.get("line_ids", [])
                             if i in by_line]
                    if not lines:
                        rejected += 1
                        continue
                    context = self._table_context(ordered, region)
                    try:
                        table = build_table(
                            document_id=self.document_id,
                            page_number=art.page.page_number,
                            region_id=region.id, lines=lines,
                            v_lines=art.signals_v_lines,
                            page_width=art.page.render_width,
                            page_height=art.page.render_height,
                            table_index=idx, context_text=context,
                            calendar=model.calendar,
                            ruled=bool(region.meta.get("ruled")),
                            base_confidence=region.confidence,
                            entity_hint=assembly.entity_for(model, region.id))
                    except Exception as exc:  # noqa: BLE001
                        log.exception("tables.build_failed",
                                      document_id=self.document_id,
                                      page_number=art.page.page_number,
                                      region_id=region.id, error=str(exc))
                        rejected += 1
                        continue
                    idx += 1
                    if table is None:
                        # Not an error: the band did not survive structural
                        # reconstruction, so it is honestly not a table.
                        region.type = RegionType.PARAGRAPH
                        region.meta["table_rejected"] = True
                        rejected += 1
                        continue
                    art.tables.append(table)
                    built += 1
                art.page.table_count = len(art.tables)
        self._stage("tables", StageStatus.SUCCEEDED, total=total, done=built,
                    failed=0, detail={"built": built, "rejected": rejected})

    @staticmethod
    def _table_context(ordered: list[Region], region: Region) -> str:
        """Text immediately preceding a table -- its caption and unit declaration."""
        before = [r for r in ordered
                  if r.reading_order < region.reading_order
                  and r.type in (RegionType.PARAGRAPH, RegionType.SECTION_HEADING,
                                 RegionType.CAPTION, RegionType.TITLE)]
        return " ".join(r.text for r in before[-3:])[-400:]

    # -------------------------------------------------------------- stage 7
    def _build_charts(self, artifacts: list[PageArtifacts]) -> None:
        total = sum(1 for a in artifacts for r in a.regions
                    if r.type in (RegionType.CHART, RegionType.FIGURE))
        self._stage("charts", StageStatus.RUNNING, total=total)
        found = 0
        with stage_timer(log, "charts", document_id=self.document_id):
            if total:
                renderer = PageRenderer(self.document_id, self.pdf_path)
                try:
                    for art in artifacts:
                        if art.page.status == StageStatus.FAILED:
                            continue
                        if not any(r.type in (RegionType.CHART, RegionType.FIGURE)
                                   for r in art.regions):
                            continue
                        image = renderer.load_image_array(art.page.page_number)
                        art.charts = detect_charts(
                            document_id=self.document_id,
                            page_number=art.page.page_number,
                            regions=art.regions, image=image, lines=art.lines)
                        art.page.chart_count = len(art.charts)
                        found += len(art.charts)
                        del image
                finally:
                    renderer.close()
        self._stage("charts", StageStatus.SUCCEEDED, total=total, done=found,
                    detail={"charts": found})

    # -------------------------------------------------------------- stage 8
    def _extract(self, artifacts: list[PageArtifacts], model
                 ) -> tuple[list[FinancialFact], list[Commentary], list[Guidance]]:
        self._stage("extraction", StageStatus.RUNNING)
        facts: list[FinancialFact] = []
        commentary: list[Commentary] = []
        guidance: list[Guidance] = []
        currency = Currency(model.currency) if model.currency else None
        unit = MagnitudeUnit(model.unit) if model.unit else None
        # every entity the document introduced, so narrative sentences can
        # be attributed to the one they name rather than their section
        known_entities = tuple(sorted(
            {e for e in model.entity_by_region.values() if e},
            key=len, reverse=True))

        with stage_timer(log, "extraction", document_id=self.document_id):
            for art in artifacts:
                if art.page.status == StageStatus.FAILED:
                    continue
                image_url = prov_service.page_image_url(
                    self.document_id, art.page.page_number)

                for table in art.tables:
                    ctx = TableFactContext(
                        document_id=self.document_id,
                        entity=assembly.entity_for(model, table.region_id),
                        calendar=model.calendar, default_currency=currency,
                        default_unit=unit, page_image_url=image_url)
                    try:
                        facts.extend(extract_from_table(table, ctx))
                    except Exception as exc:  # noqa: BLE001
                        log.exception("extraction.table_failed",
                                      document_id=self.document_id,
                                      table_id=table.id, error=str(exc))

                for region in art.regions:
                    if region.type not in (RegionType.PARAGRAPH, RegionType.LIST,
                                           RegionType.KPI_BLOCK,
                                           RegionType.FOOTNOTE):
                        continue
                    nctx = NarrativeContext(
                        document_id=self.document_id,
                        entity=assembly.entity_for(model, region.id),
                        calendar=model.calendar, default_currency=currency,
                        default_unit=unit, default_period=model.primary_period,
                        page_image_url=image_url,
                        known_entities=known_entities)
                    try:
                        if is_forward_looking(region.text):
                            guidance.extend(extract_guidance(region, nctx))
                        else:
                            facts.extend(extract_narrative_facts(region, nctx))
                        c = extract_commentary(region, nctx)
                        if c is not None:
                            commentary.append(c)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("extraction.region_failed",
                                      document_id=self.document_id,
                                      region_id=region.id, error=str(exc))

            before = len(facts)
            facts = deduplicate(facts)

        self._stage("extraction", StageStatus.SUCCEEDED,
                    total=before, done=len(facts),
                    detail={"raw_facts": before, "after_dedup": len(facts),
                            "commentary": len(commentary),
                            "guidance": len(guidance)})
        return facts, commentary, guidance

    # -------------------------------------------------------------- stage 9
    def _validate_facts(self, facts: list[FinancialFact]):
        self._stage("validating_facts", StageStatus.RUNNING, total=len(facts))
        with stage_timer(log, "validating_facts", document_id=self.document_id):
            report = validate(self.document_id, facts)
        self._stage("validating_facts", StageStatus.SUCCEEDED, total=len(facts),
                    done=len(facts),
                    detail={"checks": len(report.results),
                            "passed": report.passed,
                            "warnings": report.warnings,
                            "failures": report.failures})
        return report

    def _finalise_confidence(self, facts: list[FinancialFact],
                             statuses: dict[str, ValidationStatus]) -> None:
        for f in facts:
            status = statuses.get(f.fact_id, ValidationStatus.NOT_CHECKED)
            f.validation_status = status
            if f.confidence_breakdown is not None:
                bd = conf_mod.apply_validation(f.confidence_breakdown, status)
                f.confidence_breakdown = bd
                f.confidence = bd.overall
                f.confidence_band = bd.band
                f.requires_review = conf_mod.requires_review(bd, status)
            else:
                f.requires_review = f.confidence < settings.review_threshold

    # ------------------------------------------------------------- stage 10
    def _attach_provenance(self, result: PipelineResult) -> None:
        dims = {p.page_number: (p.render_width, p.render_height)
                for p in result.pages}
        for f in result.facts:
            if f.provenance is None:
                continue
            w, h = dims.get(f.provenance.page_number, (0, 0))
            prov_service.enrich(f.provenance, render_width=w, render_height=h)

    # ------------------------------------------------------------ conclusion
    def _final_status(self, result: PipelineResult) -> DocumentStatus:
        """Report what actually happened -- never COMPLETED after a failure."""
        stage_failed = any(s.status == StageStatus.FAILED for s in self._stages)
        if stage_failed or result.pages_done == 0:
            return DocumentStatus.FAILED
        if result.pages_failed > 0:
            return DocumentStatus.PARTIALLY_COMPLETED
        if not result.facts:
            # Nothing extracted is not a crash, but it is not success either.
            return DocumentStatus.PARTIALLY_COMPLETED
        if any(f.requires_review for f in result.facts):
            return DocumentStatus.REQUIRES_REVIEW
        return DocumentStatus.COMPLETED
