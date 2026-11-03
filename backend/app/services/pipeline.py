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

import contextvars
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable

from app.charts.detector import detect_charts
from app.charts.triage import summarise as summarise_charts, triage
from app.layout.furniture import find_templates, group_by_template
from app.config.settings import _available_memory_mb, settings
from app.core.errors import FinSightError, StageFailure
from app.core.geometry import BBox
from app.core.ids import stable_id
from app.core.logging import get_logger, stage_timer
from app.core.control import Control, ProcessingCancelled
from app.core.modes import (
    AccessLedger, NativeTextScope, ProcessingMode,
    native_text_allowed, processing_mode,
)
from app.document import assembly
from app.extraction import confidence as conf_mod
from app.extraction import fact_state
from app.extraction.facts import TableFactContext, apply_introduced_basis, extract_from_table
from app.extraction.narrative import (
    NarrativeContext, extract_commentary, extract_guidance,
    extract_narrative_facts, is_forward_looking,
)
from app.ingestion.validator import PdfProbe, probe_pdf
from app.layout.cv_signals import page_signals
from app.layout.detector import HeuristicLayoutDetector
from app.models.enums import (
    DuplicateResolution,
    Currency, DocumentStatus, EntityType, MagnitudeUnit, RegionType,
    StageStatus, ValidationStatus,
)
from app.models.schemas import (
    Anomaly, Commentary, DerivedMetric, EntityDto, ExtractedChart,
    ExtractedTable, FactRelation, FinancialFact, Guidance, Insight, OCRElement,
    PageInfo, Region, RiskStatement, StageRecord, TableSemantics,
    ValidationResult,
)
from app.ocr import cache as ocr_cache
from app.ocr import textlayer
from app.ocr.lines import TextLine, group_lines
from app.extraction.qualification import qualify_from_footnotes
from app.ocr import second_read
from app.ocr.registry import get_engine
from app.provenance import service as prov_service
from app.rendering.quality import measure as measure_quality
from app.rendering.renderer import PageRenderer, RenderedPage
from app.analysis.anomalies import detect_anomalies
from app.analysis.derived import compute_derived
from app.analysis.insights import generate_insights
from app.analysis.reconcile import cross_metric_conflicts, reconcile
from app.analysis.relations import build_relations
from app.analysis.risks import extract_risks
from app.tables import semantics as table_semantics
from app.normalization.periods import parse_period
from app.tables.periods import (
    harmonise_periods, infer_calendar_from_headers,
    reresolve_column_periods,
)
from app.services.page_routing import plan_page
from app.spatial.cards import detect_cards
from app.spatial.geometry import median_line_height as spatial_line_height
from app.spatial.rows import detect_rows
from app.spatial.transposed import detect_transposed
from app.spatial.extract import (
    CardContext, extract_from_cards, extract_from_grid, extract_from_rows,
)
from app.tables.fragments import group_table_regions
from app.tables.structure import build_table
from app.validation.engine import validate

log = get_logger("pipeline")

#: Table shapes whose columns are reporting periods, so a figure
#: from one owes a period. Mirrors `tables.shape.TableShape`.
_PERIODIZED_SHAPES = frozenset(
    {"period_flow", "point_in_time", "periodized_kpi"})

ProgressFn = Callable[[str, StageStatus, int, int, dict], None]


def _region_governing(regions: list, box: BBox | None):
    """The region whose structure owns this piece of the page.

    A spatial fact has a bounding box but no region of its own -- the spatial
    readers work on OCR elements and never build one. Something still has to
    say which section of the document it sits in, and until now that was
    ``regions[0]``: the first entry of a list the detector fills tables-first,
    graphics-second, text-third. It is not reading order and it is not
    containment. On one page it resolved a bank's shareholding panel to the
    lending segment named fifty pages earlier.

    The smallest region that actually contains the box is the honest answer:
    smallest, because a page-sized region says less about a figure than the
    panel it sits inside. Where nothing contains it, the answer is nothing --
    the caller then falls back to the page's own context rather than to an
    arbitrary neighbour.
    """
    if box is None:
        return None
    best = None
    best_area = None
    for region in regions:
        geom = region.bbox.geom
        if geom.contains_ratio(box) <= 0.6:
            continue
        area = geom.area
        if best_area is None or area < best_area:
            best, best_area = region, area
    return best


def _spatial_entity_ref(model, art, boxes: list[BBox]):
    """Entity for a set of spatial candidates, from the strongest context.

    All the candidates in one grid or one block share a context, so the region
    governing them is resolved once from their combined extent. Where the
    regions disagree -- the block spans a section boundary -- there is no
    single structural answer and the caller is told so.
    """
    box = BBox.union_all(boxes) if boxes else None
    region = _region_governing(art.regions, box)
    if region is not None:
        return assembly.entity_ref_for(model, region.id), region.id

    # Nothing contains it. The page's own governing entity is weaker evidence
    # than containment but stronger than a guess, and it is what every other
    # region on the page resolved to. Where the page agrees with itself, take
    # it; where it does not, fall back to the document's reporting basis rather
    # than picking one of the disagreeing sections.
    on_page = {assembly.entity_for(model, r.id) for r in art.regions
               if r.type not in (RegionType.HEADER, RegionType.FOOTER,
                                 RegionType.PAGE_NUMBER)}
    if len(on_page) == 1:
        return model.entities.get(next(iter(on_page))), None
    return assembly.entity_ref_for(model, None), None


def _refused_labels(offered: list[str], accepted: list[FinancialFact],
                    *, limit: int = 20) -> list[str]:
    """Which labels a strategy read but could not turn into a fact.

    Counting refusals says how many; naming them says which, and that is the
    difference between "51 unrecognised labels" and a list of the metrics the
    taxonomy is missing.
    """
    kept = {f.context_text for f in accepted}
    return sorted({label for label in offered if label not in kept})[:limit]


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
    corroboration: dict = field(default_factory=dict)


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
    entities: list[EntityDto] = field(default_factory=list)
    entity_hierarchy: list[dict] = field(default_factory=list)
    table_semantics: list[TableSemantics] = field(default_factory=list)
    derived_metrics: list[DerivedMetric] = field(default_factory=list)
    relations: list[FactRelation] = field(default_factory=list)
    risks: list[RiskStatement] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    mode: str = str(ProcessingMode.HYBRID)
    native_text_audit: dict = field(default_factory=dict)

    @property
    def pages_failed(self) -> int:
        return sum(1 for p in self.pages if p.status == StageStatus.FAILED)

    @property
    def pages_done(self) -> int:
        return sum(1 for p in self.pages if p.status == StageStatus.SUCCEEDED)


def _basis_entities(model, table) -> dict:
    """The reporting bases a table's column heads declare, as registered
    entities. A column head naming "Standalone" is evidence of that basis on
    its own, and registers it if no page header already had."""
    wanted = set(((getattr(table, "meta", None) or {}).get("column_basis") or {}).values())
    if not wanted:
        return {}
    registry = model.entities
    out = {}
    for basis in wanted:
        if basis == "standalone":
            eid = registry.register_basis(
                EntityType.STANDALONE, page=table.page_number,
                evidence="declared by a column group head of this table")
        elif basis == "consolidated":
            eid = registry.consolidated_id
        else:
            continue
        ref = registry.get(eid)
        if ref is not None:
            out[basis] = (ref.name, ref.entity_id, ref.entity_type, ref.parent_id)
    return out


class Pipeline:
    """Executes the full document pipeline for one document."""

    def __init__(self, document_id: str, pdf_path: str | Path, *,
                 progress: ProgressFn | None = None,
                 page_workers: int | None = None,
                 force_ocr: bool = False,
                 content_hash: str | None = None,
                 control: Control | None = None,
                 mode: ProcessingMode | str = ProcessingMode.HYBRID):
        self.document_id = document_id
        #: Where the user's pause/cancel requests arrive.  A run created
        #: without one is uninterruptible, which is what the tests and the CLI
        #: want; the API always supplies one.
        self.control = control
        self.mode = ProcessingMode(mode)
        self.force_ocr = force_ocr
        # OCR results are cached against the file's content, not its id
        self.cache_scope = content_hash or document_id
        #: The PDF bytes' sha256 when the caller knows it; renders are then
        #: shared by every document with the same bytes (Phase 22).
        self.content_hash = content_hash
        self.pdf_path = Path(pdf_path)
        self.progress = progress or (lambda *a, **k: None)
        self.page_workers = max(1, page_workers or settings.resolved_page_workers)
        # Recorded because a run's speed is mostly this number, and the number
        # depends on what the machine had free at the time.  Without it, a run
        # that was throttled looks identical to one that was simply slow.
        log.info("pipeline.concurrency", document_id=document_id,
                 page_workers=self.page_workers,
                 cores=os.cpu_count(),
                 available_memory_mb=(round(_available_memory_mb() or 0)))
        self.detector = HeuristicLayoutDetector()
        self._stages: list[StageRecord] = []
        self._lock = threading.Lock()
        self._stage_started: dict[str, float] = {}
        #: Wall time actually spent in each per-page phase, summed across the
        #: worker threads.  The page stages run interleaved in one parallel
        #: loop, so their individual cost cannot be read off the stage records.
        self._phase_seconds: dict[str, float] = {}
        #: What each page was offered to, what it produced, and why the rest
        #: was refused.  This is what turns "page 18 produced nothing" from a
        #: mystery into a sentence.
        self._page_diagnostics: dict[int, dict] = {}

    # ------------------------------------------------------------- utilities
    def _checkpoint(self, *, page: int = 0, total: int = 0,
                    stage: str = "") -> None:
        """Offer the run a chance to pause or stop.

        Called where stopping is safe: after a page finishes and before each
        stage begins.  No-ops when the run has no control attached.
        """
        if self.control is not None:
            self.control.checkpoint(page=page, total=total, stage=stage)

    def _add_phase(self, name: str, seconds: float) -> None:
        with self._lock:
            self._phase_seconds[name] = self._phase_seconds.get(name, 0.0) + seconds

    def _stage(self, name: str, status: StageStatus, *, total: int = 0,
               done: int = 0, failed: int = 0, engine: str | None = None,
               duration_ms: float | None = None, error: str | None = None,
               detail: dict | None = None) -> StageRecord:
        # Time each stage from its RUNNING marker to its terminal one, so the
        # per-stage cost of a run is recorded and not merely logged.  Callers
        # may still pass an explicit duration, which wins.
        now = perf_counter()
        if status == StageStatus.RUNNING:
            # Only the *first* RUNNING marker starts the clock.  The per-page
            # loop republishes RUNNING after every page to report progress;
            # restarting the timer there measured the last page alone.
            self._stage_started.setdefault(name, now)
        elif duration_ms is None:
            started = self._stage_started.get(name)
            if started is not None:
                duration_ms = round((now - started) * 1000, 1)
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
        """Execute the pipeline under this run's processing mode.

        The mode is entered here so that every stage -- including the
        per-page worker threads -- inherits it, and so the access ledger
        covers the whole run.
        """
        with processing_mode(self.mode) as ledger:
            result = self._run(ledger)
        if self._cancelled is not None:
            # A cancellation is the user's decision, not a failure of the run,
            # and it must reach the caller as itself.  Returning a FAILED
            # result here would record "processing failed" for a document the
            # user deliberately stopped.
            raise self._cancelled
        result.mode = str(self.mode)
        result.native_text_audit = ledger.summary()
        return result

    def _run(self, ledger: AccessLedger) -> PipelineResult:
        t_start = perf_counter()
        self._cancelled: ProcessingCancelled | None = None
        result = PipelineResult(document_id=self.document_id,
                                status=DocumentStatus.FAILED, probe=None)
        try:
            probe = self._validate()
            result.probe = probe

            renderer = PageRenderer(self.document_id, self.pdf_path,
                                     content_hash=self.content_hash)
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
            # Repair column periods across the whole document before any fact
            # is read from a cell: a header the document resolved on one page
            # means the same thing where its continuation repeats it.
            # The fiscal year end is often only establishable once the tables
            # exist: a column head reading "Year Ended" over "31st Mar'26"
            # states it outright, where the same words in flowing text cannot
            # be paired safely.  Where the headers settle it, adopt it and
            # re-read every column under it.
            if not model.calendar.is_known:
                from_headers = infer_calendar_from_headers(result.tables)
                if from_headers is not None:
                    model.calendar = from_headers
                    reresolve_column_periods(result.tables,
                                             calendar=from_headers)
            harmonise_periods(result.tables, calendar=model.calendar)

            self._build_charts(artifacts)
            result.charts = [c for a in artifacts for c in a.charts]

            facts, commentary, guidance, semantics = self._extract(
                artifacts, model)
            result.commentary = commentary
            result.guidance = guidance
            result.table_semantics = semantics
            result.entities = [
                EntityDto(entity_id=e.entity_id,
                          entity_name=e.name, entity_type=e.entity_type,
                          parent_entity_id=e.parent_id,
                          aliases=sorted(e.aliases), confidence=e.confidence,
                          source=e.source, first_page=e.first_page,
                          evidence=e.evidence)
                for e in model.entities.all()]
            result.entity_hierarchy = model.entities.hierarchy()

            self._second_read(facts)
            report = self._validate_facts(facts)
            result.validations = report.results

            self._finalise_confidence(facts, report.fact_status)
            result.facts = facts

            self._analyse(result, model, artifacts)
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
        except ProcessingCancelled as stop:
            # Remembered rather than raised from here, so the mode context and
            # its access ledger still close cleanly; ``run`` re-raises it once
            # the run has unwound.
            self._cancelled = stop
            result.status = DocumentStatus.CANCELLED
            result.stages = list(self._stages)
            result.duration_ms = round((perf_counter() - t_start) * 1000, 1)
            log.info("pipeline.cancelled", document_id=self.document_id,
                     page=stop.page, total=stop.total, stage=stop.stage)
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

        # ContextVars do NOT propagate into ThreadPoolExecutor workers, so the
        # processing mode (and its access ledger) must be carried across
        # explicitly.  Without this the native-text guard would be inactive in
        # exactly the threads that do the page work.
        parent_context = contextvars.copy_context()

        def work(page_number: int) -> PageArtifacts:
            # The checkpoint belongs *here*, inside the worker, not in the loop
            # that consumes results: ``pool.map`` submits every page at once,
            # so pausing the consumer would leave the workers churning through
            # all 165 pages regardless.  Checking before each page starts means
            # a pause takes effect within one page per worker, and a cancel
            # drains the queued pages almost immediately because each one
            # raises at its own checkpoint instead of doing any OCR.
            self._checkpoint(page=page_number, total=total, stage="ocr")
            return self._process_one_page(renderer, engine, page_number, probe)

        # ONNX Runtime releases the GIL during inference, so threads give real
        # parallelism here without the memory cost of extra processes.
        with ThreadPoolExecutor(max_workers=self.page_workers,
                                thread_name_prefix="page") as pool:
            def work_in_context(page_number: int) -> PageArtifacts:
                return parent_context.copy().run(work, page_number)

            for art in pool.map(work_in_context, range(1, total + 1)):
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
            # The three phases interleave inside one parallel loop, so report
            # the time actually spent in each alongside the loop's wall clock.
            self._stage(
                name, final, total=total, done=done[name], failed=failed,
                engine=engine.info.name if name == "ocr" else None,
                detail={"phase_seconds": round(
                    self._phase_seconds.get(name, 0.0), 2)})
        return artifacts

    # ----------------------------------------------------------- page quality
    def _recognize(self, engine, image, page_number: int, info):
        """OCR the page, after measuring how well it was captured.

        The measurement is recorded and reported; it does not change what is
        read.  Selective repair -- deskew, unsharp, CLAHE, upscaling, adaptive
        and Otsu binarisation, alone and combined -- was built, run against the
        scanner-degraded benchmark and measured, and none of it recovered
        accuracy: the twelve variants moved the OCR score by between -2.0% and
        +1.3% on the worst pages, inside run-to-run noise, while element counts
        stayed put.  The detector was finding the same text either way; what
        the degradation destroyed was stroke detail inside those boxes, and no
        filter puts that back.  Since a second OCR pass costs roughly three
        times the wall clock for no accuracy, the pass is not shipped.  What
        remains is the measurement, which tells a reader *why* a page read
        badly instead of leaving them to guess.
        """
        quality = measure_quality(image)
        info.image_quality = quality.as_dict()
        if quality.defects or quality.observations:
            log.info("ocr.page_quality", document_id=self.document_id,
                     page_number=page_number, **quality.as_dict())
        return engine.recognize(image, document_id=self.document_id,
                                page_number=page_number)

    def _process_one_page(self, renderer: PageRenderer, engine,
                          page_number: int, probe: PdfProbe) -> PageArtifacts:
        page_id = stable_id("page", self.document_id, page_number)
        t_page = perf_counter()
        info = PageInfo(
            id=page_id, document_id=self.document_id, page_number=page_number,
            pdf_width=0.0, pdf_height=0.0, render_width=0, render_height=0,
            dpi=settings.render_dpi, status=StageStatus.RUNNING)
        art = PageArtifacts(page=info)
        try:
            t_render = perf_counter()
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
            self._add_phase("rendering", perf_counter() - t_render)

            t_ocr = perf_counter()
            render_sig = f"dpi{rendered.dpi}-{settings.render_format}"
            ocr_sig = settings.ocr_signature
            ocr_result = (None if self.force_ocr else
                          ocr_cache.load(self.cache_scope, page_number,
                                         render_sig, ocr_sig,
                                         document_id=self.document_id))
            if ocr_result is None:
                ocr_result = self._recognize(engine, image, page_number, info)
                ocr_cache.store(self.cache_scope, page_number, render_sig,
                                ocr_sig, ocr_result)
            else:
                info.image_quality = measure_quality(image).as_dict()
                log.debug("ocr.cache_hit", document_id=self.document_id,
                          page_number=page_number)
            self._add_phase("ocr", perf_counter() - t_ocr)
            info.ocr_engine = ocr_result.engine
            info.ocr_signature = ocr_sig
            info.ocr_duration_ms = ocr_result.duration_ms
            art.elements = ocr_result.elements
            info.ocr_element_count = len(ocr_result.elements)
            info.ocr_mean_confidence = ocr_result.mean_confidence

            # Auxiliary channel: corroborate against the PDF text layer.
            # In VISUAL_ONLY this is skipped entirely -- not attempted and
            # caught, which would be a fallback in disguise.  Everything below
            # this point therefore sees only pixel-derived evidence.
            if native_text_allowed(NativeTextScope.CORROBORATION):
                words = textlayer.extract_words(self.pdf_path, page_number,
                                                rendered.scale)
                info.has_text_layer = bool(words)
                info.text_layer_chars = sum(len(w.text) for w in words)
                info.is_scanned = not info.has_text_layer
                if words:
                    art.corroboration = textlayer.corroborate(art.elements, words)
            else:
                # Unknown rather than False: visual-only mode deliberately does
                # not look, so it cannot report whether a text layer exists.
                info.has_text_layer = False
                info.text_layer_chars = 0
                info.is_scanned = True
                art.corroboration = {"skipped": "VISUAL_ONLY"}

            art.lines = group_lines(art.elements, document_id=self.document_id,
                                    page_number=page_number)
            for idx, el in enumerate(art.elements):
                el.reading_index = idx

            t_layout = perf_counter()
            signals = page_signals(image, [e.bbox.geom for e in art.elements])
            art.signals_v_lines = signals.v_lines
            art.regions = self.detector.detect(
                document_id=self.document_id, page_number=page_number,
                lines=art.lines, signals=signals, elements=art.elements)
            info.region_count = len(art.regions)
            self._add_phase("layout", perf_counter() - t_layout)

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
        # Recorded for failed pages too: a page that took ninety seconds and
        # then threw is exactly the page worth knowing about.
        info.duration_ms = round((perf_counter() - t_page) * 1000.0, 1)
        if info.duration_ms > settings.slow_page_ms:
            log.warning("pipeline.slow_page", document_id=self.document_id,
                        page_number=page_number, duration_ms=info.duration_ms,
                        ocr_ms=info.ocr_duration_ms,
                        elements=info.ocr_element_count)
        return art

    # -------------------------------------------------------------- stage 5
    def _assemble(self, artifacts: list[PageArtifacts]):
        with stage_timer(log, "document_assembly", document_id=self.document_id):
            self._checkpoint(stage="document_assembly")
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
        self._checkpoint(stage="tables")
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
                # A statement's internal rules read as band boundaries, so one
                # table arrives as several regions -- and the fragment holding
                # the column heads is not the one holding the figures. Rejoin
                # them before building, so the header block and the data are in
                # the same table. See `tables/fragments.py`.
                groups = group_table_regions(
                    ordered, art.page.render_height or 0,
                    page_width=art.page.render_width or 0,
                    lines_by_id=by_line)
                merged_into = {rid: g.region_ids[0]
                               for g in groups if g.is_merge
                               for rid in g.region_ids[1:]}
                extra_lines: dict[str, list[str]] = {}
                merge_reasons = {g.region_ids[0]: g for g in groups if g.is_merge}
                for g in groups:
                    if g.is_merge:
                        extra_lines[g.region_ids[0]] = [
                            lid for rid in g.region_ids[1:]
                            for lid in next(
                                (r.meta.get("line_ids", []) for r in ordered
                                 if r.id == rid), [])]
                for region in ordered:
                    if region.type != RegionType.TABLE:
                        continue
                    if region.id in merged_into:
                        # Its lines are being built as part of the fragment
                        # above it. Keep the region -- a reader can still see
                        # what layout found -- but do not build it twice.
                        region.meta["merged_into"] = merged_into[region.id]
                        continue
                    merge_record = merge_reasons.get(region.id)
                    # Repeated page furniture (the registered-office block) is
                    # column-aligned and full of numbers, so it survives band
                    # detection; cross-page chrome analysis is what rules it out.
                    if region.id in model.chrome_region_ids:
                        region.type = RegionType.FOOTER
                        region.meta["table_rejected"] = "page chrome"
                        rejected += 1
                        continue
                    # Fragments of one statement overlap vertically -- layout
                    # cuts them at a rule and both sides claim the lines around
                    # it -- so the same line arrives from two regions. Taking
                    # them as they come duplicated the header rows, and a
                    # duplicated header row is read as data, which cost the
                    # table its column dates.
                    line_ids: list[str] = []
                    seen_lines: set[str] = set()
                    for lid in (list(region.meta.get("line_ids", []))
                                + extra_lines.get(region.id, [])):
                        if lid not in seen_lines:
                            seen_lines.add(lid)
                            line_ids.append(lid)
                    lines = [by_line[i] for i in line_ids if i in by_line]
                    lines.sort(key=lambda ln: (ln.bbox.y, ln.bbox.x))
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
                            page_lines=list(by_line.values()),
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
                    if merge_record is not None:
                        # Why these bands were joined, in the table's own
                        # record, so a merge is never invisible.
                        table.meta = dict(table.meta or {})
                        table.meta["fragment_merge"] = {
                            "regions": list(merge_record.region_ids),
                            "explanation": merge_record.reason,
                            "verdicts": list(merge_record.verdicts),
                        }
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
    _GRAPHIC_TYPES = (RegionType.CHART, RegionType.FIGURE)

    def _mark_page_furniture(self, artifacts: list[PageArtifacts]) -> int:
        """Set aside graphic regions that belong to the page template.

        Done here rather than in the layout detector because the evidence is
        cross-page: a logo is not identifiable from the one page it sits on,
        only from the sixty it repeats across. The regions are kept, marked and
        counted -- not deleted -- so a reader can still see what was decided.
        """
        furniture = find_templates(
            [(a.page.page_number, a.page.render_width or 0,
              a.page.render_height or 0, a.regions, a.elements)
             for a in artifacts if a.page.status != StageStatus.FAILED],
            types=self._GRAPHIC_TYPES)
        if not furniture:
            return 0
        for art in artifacts:
            for region in art.regions:
                template = furniture.get(region.id)
                if template is None:
                    continue
                region.meta["page_furniture"] = True
                region.meta["page_furniture_reason"] = template.describe()
        self._furniture_templates = [
            {"pages": len(t.pages), "regions": len(t.region_ids),
             "size_px": f"{t.width_px}x{t.height_px}", "reason": t.describe()}
            for t in group_by_template(furniture)]
        return len(furniture)

    def _build_charts(self, artifacts: list[PageArtifacts]) -> None:
        # This stage has three distinct parts and used to report one number for
        # all of them, which made it impossible to say where its time went: on
        # one 18-page deck the stage cost 43s while the chart detection inside
        # it accounted for 6s. The parts are timed separately and reported.
        t_furniture = perf_counter()
        suppressed = self._mark_page_furniture(artifacts)
        t_furniture = perf_counter() - t_furniture

        def analysable(region) -> bool:
            return (region.type in self._GRAPHIC_TYPES
                    and not region.meta.get("page_furniture"))

        total = sum(1 for a in artifacts for r in a.regions if analysable(r))
        self._checkpoint(stage="charts")
        self._stage("charts", StageStatus.RUNNING, total=total)
        found = 0
        t_detect = perf_counter()
        with stage_timer(log, "charts", document_id=self.document_id):
            if total:
                renderer = PageRenderer(self.document_id, self.pdf_path,
                                     content_hash=self.content_hash)
                try:
                    for art in artifacts:
                        if art.page.status == StageStatus.FAILED:
                            continue
                        regions = [r for r in art.regions if analysable(r)]
                        if not regions:
                            continue
                        image = renderer.load_image_array(art.page.page_number)
                        art.charts = detect_charts(
                            document_id=self.document_id,
                            page_number=art.page.page_number,
                            regions=regions, image=image, lines=art.lines)
                        art.page.chart_count = len(art.charts)
                        found += len(art.charts)
                        del image
                finally:
                    renderer.close()
        t_detect = perf_counter() - t_detect
        t_triage = perf_counter()
        # What each surviving chart could tell us, if anything.
        #
        # Gathered by association rather than containment: a chart region is
        # the ink of its bars, and the printed data labels sit outside it. On
        # one page of seven printed percentages the containment test kept none.
        evidence = triage(
            [c for a in artifacts for c in a.charts],
            {a.page.page_number: a.elements for a in artifacts},
            {a.page.page_number: (a.page.render_width or 0,
                                  a.page.render_height or 0)
             for a in artifacts})
        self._chart_evidence = evidence
        summary = summarise_charts(evidence)
        for ev in evidence:
            self._page_diagnostics.setdefault(
                ev.page_number, {}).setdefault("charts", []).append(ev.as_dict())

        t_triage = perf_counter() - t_triage
        self._stage("charts", StageStatus.SUCCEEDED, total=total, done=found,
                    detail={"charts": found,
                            "page_furniture_suppressed": suppressed,
                            "page_furniture_templates":
                                getattr(self, "_furniture_templates", []),
                            "seconds": {"page_furniture": round(t_furniture, 2),
                                        "detection": round(t_detect, 2),
                                        "triage": round(t_triage, 2)},
                            "triage": summary})

    # -------------------------------------------------------------- stage 8
    def _extract(self, artifacts: list[PageArtifacts], model
                 ) -> tuple[list[FinancialFact], list[Commentary], list[Guidance],
                            list[TableSemantics]]:
        self._checkpoint(stage="extraction")
        self._stage("extraction", StageStatus.RUNNING)
        facts: list[FinancialFact] = []
        commentary: list[Commentary] = []
        guidance: list[Guidance] = []
        semantics: list[TableSemantics] = []
        currency = Currency(model.currency) if model.currency else None
        unit = MagnitudeUnit(model.unit) if model.unit else None
        # every entity the document introduced, so narrative sentences can
        # be attributed to the one they name rather than their section
        registry = model.entities
        known_entities = tuple(sorted(
            {e.name for e in registry.all() if e.name}, key=len, reverse=True))

        with stage_timer(log, "extraction", document_id=self.document_id):
            for art in artifacts:
                if art.page.status == StageStatus.FAILED:
                    continue
                image_url = prov_service.page_image_url(
                    self.document_id, art.page.page_number)

                page_before = len(facts)
                diag = self._page_diagnostics.setdefault(
                    art.page.page_number, {})
                diag["tables_offered"] = len(art.tables)
                diag["narrative_regions_offered"] = sum(
                    1 for r in art.regions
                    if r.type in (RegionType.PARAGRAPH, RegionType.LIST,
                                  RegionType.KPI_BLOCK, RegionType.FOOTNOTE))

                # Tables that actually explained something.  A detected table
                # that yielded no fact has explained nothing, so its area is
                # still available to another strategy -- which matters on a
                # results slide, where the detector lays a low-quality grid
                # over a KPI band and reads nothing from it.
                explained: list[BBox] = []

                for table in art.tables:
                    # A table whose introducing sentence names its basis --
                    # a standalone table in a consolidated page's notes.
                    apply_introduced_basis(table)
                    ref = assembly.entity_ref_for(model, table.region_id)
                    ctx = TableFactContext(
                        document_id=self.document_id,
                        entity=ref.name if ref else None,
                        entity_id=ref.entity_id if ref else None,
                        entity_type=(ref.entity_type if ref
                                     else EntityType.UNKNOWN),
                        parent_entity_id=ref.parent_id if ref else None,
                        calendar=model.calendar, default_currency=currency,
                        default_unit=unit, page_image_url=image_url,
                        basis_entities=_basis_entities(model, table))
                    try:
                        from_table = extract_from_table(table, ctx)
                        if from_table:
                            explained.append(
                                BBox.from_dict(table.bbox.model_dump()))
                        facts.extend(from_table)
                        semantics.append(table_semantics.interpret(
                            table, calendar=model.calendar,
                            entity_id=ctx.entity_id, entity_name=ctx.entity))
                    except Exception as exc:  # noqa: BLE001
                        log.exception("extraction.table_failed",
                                      document_id=self.document_id,
                                      table_id=table.id, error=str(exc))

                # ---- spatial strategies, in order of evidence ---------
                # Both run on the page's own OCR geometry, so they cost no
                # rendering and no second recognition pass.  A page whose
                # figures are already tabulated produces neither tiles nor
                # rows, and the cost is the clustering alone.
                #
                # The order *is* the routing rule.  A table that explained
                # something claims its ground; the tiles claim theirs; the
                # horizontal reader sees what is left.  Each figure is
                # explained once, by whichever strategy the page suits, and
                # nothing is read twice from two directions.
                # What this page could possibly support, asked before the
                # detectors cluster anything. Each gate is a condition the
                # detector enforces itself further down, so a page skipped here
                # is one that would have been refused there -- at the cost of
                # having built the candidates first. On a 165-page report the
                # grid reader was constructing 3,587 candidates to keep 26.
                plan = plan_page(art.elements)
                diag["routing"] = plan.as_dict()

                claimed = list(explained)
                if plan.tiles:
                    claimed += self._extract_cards(art, model, facts,
                                                   image_url, currency, unit,
                                                   claimed)
                if plan.grid:
                    claimed += self._extract_grid(art, model, facts, image_url,
                                                  currency, unit, claimed)
                if plan.rows:
                    self._extract_rows(art, model, facts, image_url,
                                       currency, unit, claimed)

                for region in art.regions:
                    if region.type not in (RegionType.PARAGRAPH, RegionType.LIST,
                                           RegionType.KPI_BLOCK,
                                           RegionType.FOOTNOTE):
                        continue
                    rref = assembly.entity_ref_for(model, region.id)
                    nctx = NarrativeContext(
                        document_id=self.document_id,
                        entity=rref.name if rref else None,
                        entity_id=rref.entity_id if rref else None,
                        entity_type=(rref.entity_type if rref
                                     else EntityType.UNKNOWN),
                        parent_entity_id=rref.parent_id if rref else None,
                        calendar=model.calendar, default_currency=currency,
                        default_unit=unit, default_period=model.primary_period,
                        page_image_url=image_url,
                        known_entities=known_entities, registry=registry)
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

                diag["facts_from_this_page"] = len(facts) - page_before

            # A footnote that states how a period's measure is stated ("1Q
            # FY26 EBITDA excludes ...") qualifies those figures before they
            # are reconciled, so a differently stated figure is kept beside
            # the statutory one instead of being called a contradiction.
            page_texts = {art.page.page_number: [
                " ".join(e.text for e in ln.elements if e.text) for ln in art.lines]
                for art in artifacts}
            qualified = qualify_from_footnotes(facts, page_texts, model.calendar)
            if qualified:
                log.info("extraction.qualified", document_id=self.document_id,
                         facts=qualified)
            before = len(facts)
            reconciliation = reconcile(self.document_id, facts)
            facts = reconciliation.facts
            self._reconciliation = reconciliation

        self._stage("extraction", StageStatus.SUCCEEDED,
                    total=before, done=len(facts),
                    detail={"raw_facts": before, "after_dedup": len(facts),
                            "merged_duplicates": reconciliation.merged,
                            "conflicts": len(reconciliation.conflicts),
                            "duplicate_classification": reconciliation.classified,
                            "commentary": len(commentary),
                            "guidance": len(guidance),
                            # Why each page produced what it did. Without this
                            # "page 18 yielded nothing" has no answer but a
                            # shrug, which is the black box this replaces.
                            "per_page": {str(k): v for k, v in
                                         sorted(self._page_diagnostics.items())}})
        return facts, commentary, guidance, semantics

    # -------------------------------------------------------------- stage 9
    def _extract_cards(self, art: PageArtifacts, model,
                       facts: list[FinancialFact], image_url: str | None,
                       currency: Currency | None,
                       unit: MagnitudeUnit | None,
                       explained: list[BBox] | None = None) -> list[BBox]:
        """Read KPI tiles on one page into canonical facts.

        Returns the ground the tiles claimed, so the strategy after this one
        does not read the same figures from the other axis.

        The page's own recognised elements are the input: nothing is rendered
        or recognised again, which is what keeps a second extraction strategy
        from doubling the cost of the run.

        The page's period, where the page states one, is offered to the tiles
        as *inherited* context only. A tile that carries its own period keeps
        it; the rest record where theirs came from.
        """
        page_number = art.page.page_number
        if not art.elements:
            return []
        # Ground the table strategy has already read. Passing it in is the
        # routing decision: each figure is explained once, by whichever
        # strategy the page's structure suits.
        occupied = list(explained or [])
        try:
            finding = detect_cards(art.elements, page_number=page_number,
                                   page_width=art.page.render_width or 1,
                                   occupied=occupied)
        except Exception as exc:  # noqa: BLE001 - a bad page must not stop the run
            log.exception("spatial.detect_failed", document_id=self.document_id,
                          page_number=page_number, error=str(exc))
            return []

        diag = self._page_diagnostics.setdefault(page_number, {})
        diag["kpi_cards_detected"] = len(finding.cards)
        diag["kpi_elements_covered_by_tables"] = finding.excluded_by_tables
        diag["kpi_columns"] = finding.columns
        diag["kpi_detection_rejected"] = dict(finding.rejected)
        if finding.notes:
            diag["kpi_notes"] = list(finding.notes)
        if not finding.cards:
            return []

        page_period, page_period_source = self._page_period(
            finding.headings, model)

        ref, ref_region = _spatial_entity_ref(
            model, art, [c.bbox for c in finding.cards])
        diag["kpi_entity"] = ref.name if ref else None
        diag["kpi_entity_from_region"] = ref_region
        ctx = CardContext(
            document_id=self.document_id, calendar=model.calendar,
            entity=(ref.name if ref else None),
            entity_id=(ref.entity_id if ref else None),
            entity_type=(ref.entity_type if ref else EntityType.UNKNOWN),
            parent_entity_id=(ref.parent_id if ref else None),
            default_currency=currency, default_unit=unit,
            page_period=page_period, page_period_source=page_period_source,
            page_image_url=image_url)

        result = extract_from_cards(finding, ctx)
        diag["kpi_facts_accepted"] = len(result.facts)
        diag["kpi_extraction_rejected"] = dict(result.rejected)
        diag["kpi_labels_refused"] = _refused_labels(
            [c.label_text for c in finding.cards], result.facts)
        facts.extend(result.facts)
        # Every tile that was *offered* claims its ground, not only the ones
        # that became facts: a tile the taxonomy did not recognise is still a
        # tile, and reading it again sideways would not make it a metric.
        return [c.bbox for c in finding.cards]

    @staticmethod
    def _page_period(headings, model):
        """The period a page heading states, offered for inheritance.

        Only a *heading* qualifies. Every page-spanning line used to, which let
        a footnote about a one-time provision lend its "H1 FY19" to a figure
        from a different year, on a different metric, in a different panel.
        """
        for heading in headings:
            resolved = parse_period(heading.text, calendar=model.calendar)
            if resolved is not None:
                return resolved, f"the page heading {heading.text.strip()!r}"
        return None, ""

    # ------------------------------------------------------------ stage 9a2
    def _extract_grid(self, art: PageArtifacts, model,
                      facts: list[FinancialFact], image_url: str | None,
                      currency: Currency | None,
                      unit: MagnitudeUnit | None,
                      claimed: list[BBox] | None = None) -> list[BBox]:
        """Read a drawn grid of figures on one page into canonical facts.

        Runs after the tiles and before the horizontal reader. A grid carries
        two axes of evidence where a row carries one, so it is the stronger
        claim on the same ground; and it runs after tables and tiles because
        both of those read their structure rather than inferring it.

        Where this re-reads something a table already explained, the evidence
        layer merges the two as corroboration rather than counting them twice.
        """
        page_number = art.page.page_number
        if not art.elements:
            return []
        try:
            finding = detect_transposed(
                art.elements, page_number=page_number,
                page_width=art.page.render_width or 1,
                occupied=list(claimed or []))
        except Exception as exc:  # noqa: BLE001 - a bad page must not stop the run
            log.exception("spatial.grid_failed", document_id=self.document_id,
                          page_number=page_number, error=str(exc))
            return []

        diag = self._page_diagnostics.setdefault(page_number, {})
        diag["grid_shape"] = f"{finding.rows}x{finding.columns}"
        diag["grid_filled"] = finding.fill
        diag["grid_orientation"] = finding.orientation
        diag["grid_candidates"] = len(finding.candidates)
        diag["grid_detection_rejected"] = dict(finding.rejected)
        if finding.notes:
            diag["grid_notes"] = list(finding.notes)
        if not finding.candidates:
            return []

        ref, ref_region = _spatial_entity_ref(
            model, art, [c.bbox for c in finding.candidates])
        diag["grid_entity"] = ref.name if ref else None
        ctx = CardContext(
            document_id=self.document_id, calendar=model.calendar,
            entity=(ref.name if ref else None),
            entity_id=(ref.entity_id if ref else None),
            entity_type=(ref.entity_type if ref else EntityType.UNKNOWN),
            parent_entity_id=(ref.parent_id if ref else None),
            default_currency=currency, default_unit=unit,
            page_image_url=image_url)

        result = extract_from_grid(finding, ctx)
        diag["grid_facts_accepted"] = len(result.facts)
        diag["grid_extraction_rejected"] = dict(result.rejected)
        diag["grid_labels_refused"] = _refused_labels(
            [c.label_text for c in finding.candidates], result.facts)
        facts.extend(result.facts)
        return [c.bbox for c in finding.candidates]

    # ------------------------------------------------------------- stage 9b
    def _extract_rows(self, art: PageArtifacts, model,
                      facts: list[FinancialFact], image_url: str | None,
                      currency: Currency | None,
                      unit: MagnitudeUnit | None,
                      claimed: list[BBox] | None = None) -> None:
        """Read horizontal metric rows on one page into canonical facts.

        Runs last of the spatial strategies, and on the least ground: whatever
        neither a successful table nor a tile has already explained. The order
        is deliberate. A row is the weakest spatial evidence there is -- the
        same shape describes a table row, a chart legend and a credit rating
        grid -- so it gets the last look rather than the first.
        """
        page_number = art.page.page_number
        if not art.elements:
            return
        try:
            finding = detect_rows(art.elements, page_number=page_number,
                                  page_width=art.page.render_width or 1,
                                  occupied=list(claimed or []))
        except Exception as exc:  # noqa: BLE001 - a bad page must not stop the run
            log.exception("spatial.rows_failed", document_id=self.document_id,
                          page_number=page_number, error=str(exc))
            return

        diag = self._page_diagnostics.setdefault(page_number, {})
        diag["row_blocks_detected"] = len(finding.blocks)
        diag["rows_detected"] = len(finding.rows)
        diag["row_detection_rejected"] = dict(finding.rejected)
        diag["rows_claimed_by_another_strategy"] = (
            finding.rows_claimed_by_other_strategies)
        if finding.notes:
            diag["row_notes"] = list(finding.notes)
        if not finding.blocks:
            return

        ref, ref_region = _spatial_entity_ref(
            model, art, [r.bbox for b in finding.blocks for r in b.rows])
        diag["row_entity"] = ref.name if ref else None
        diag["row_entity_from_region"] = ref_region
        # No page period is offered. A block occupies a corner of a page that
        # may hold three others, so only a heading that spans *this* block may
        # lend it a period -- see ``_period_for_row``.
        ctx = CardContext(
            document_id=self.document_id, calendar=model.calendar,
            entity=(ref.name if ref else None),
            entity_id=(ref.entity_id if ref else None),
            entity_type=(ref.entity_type if ref else EntityType.UNKNOWN),
            parent_entity_id=(ref.parent_id if ref else None),
            default_currency=currency, default_unit=unit,
            page_image_url=image_url)

        result = extract_from_rows(
            finding, ctx, line_height=spatial_line_height(art.elements))
        diag["row_facts_accepted"] = len(result.facts)
        diag["row_extraction_rejected"] = dict(result.rejected)
        diag["row_labels_refused"] = _refused_labels(
            [r.label_text for r in finding.rows], result.facts)
        facts.extend(result.facts)

    def _second_read(self, facts: list[FinancialFact]) -> None:
        """Read each table figure's cell a second time, alone and at a higher
        resolution (``app.ocr.second_read``). Disagreement is recorded on
        the fact and sends it to review; the value is never changed."""
        if not settings.second_read_enabled:
            return
        self._stage("second_read", StageStatus.RUNNING, total=len(facts))
        with stage_timer(log, "second_read", document_id=self.document_id):
            stats = second_read.verify(facts, self.pdf_path,
                                       cache_scope=self.cache_scope,
                                       engine=get_engine())
        self._stage("second_read", StageStatus.SUCCEEDED, total=stats.read,
                    done=stats.read, detail=stats.as_dict())

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
        """Fold validation and the evidence layer into every fact's score.

        Reconciliation runs before this, so what other strategies made of the
        same figure is available here rather than being scored blind. That
        ordering is the point: a fact corroborated across strategies and a fact
        contradicted by one used to look identical, because both were scored
        before anything had compared them.
        """
        reconciliation = getattr(self, "_reconciliation", None)
        agreements = dict(reconciliation.corroboration) if reconciliation else {}
        for f in facts:
            status = statuses.get(f.fact_id, ValidationStatus.NOT_CHECKED)
            f.validation_status = status
            if f.confidence_breakdown is not None:
                bd = conf_mod.apply_validation(f.confidence_breakdown, status)
                bd = conf_mod.apply_reconciliation(
                    bd, agreeing_sources=agreements.get(f.fact_id, 0),
                    contradicted=(f.duplicate_resolution
                                  == DuplicateResolution.GENUINE_CONFLICT))
                if f.value is None:
                    bd = conf_mod.cap_unparsed_value(bd)
                f.confidence_breakdown = bd
                f.confidence = bd.overall
                f.confidence_band = bd.band
            f.review_reason = conf_mod.review_reason_for(f, status)
            f.requires_review = f.review_reason is not None

            # The dimensions of a fact, kept apart and read separately, with
            # the weakest deciding its state. A single blended score lets
            # excellent recognition hide poor attribution, and that is the
            # failure this project keeps finding: a figure read perfectly from
            # a clean page is worth nothing if its column was never matched to
            # a heading.
            #
            # Recorded alongside the score rather than replacing it -- the
            # score still orders a review queue -- so nothing downstream that
            # reads confidence changes behaviour.
            shape = (f.meta or {}).get("table_shape", "")
            state, evidence = fact_state.describe(
                f, period_mandatory=shape in _PERIODIZED_SHAPES)
            f.meta = dict(f.meta or {})
            f.meta["fact_state"] = str(state)
            f.meta["evidence"] = evidence.as_dict()

    # ------------------------------------------------------------- stage 10
    def _analyse(self, result: PipelineResult, model,
                 artifacts: list[PageArtifacts]) -> None:
        """Financial intelligence over the extracted facts.

        Runs after validation so every fact already carries its validation
        status, and deliberately after extraction so that nothing here can
        influence what was read off the page.
        """
        self._stage("analysis", StageStatus.RUNNING, total=len(result.facts))
        with stage_timer(log, "analysis", document_id=self.document_id):
            result.derived_metrics = compute_derived(self.document_id,
                                                     result.facts)
            relations = build_relations(self.document_id, result.facts,
                                        result.derived_metrics)
            reconciliation = getattr(self, "_reconciliation", None)
            if reconciliation is not None:
                relations.extend(reconciliation.relations)
            relations.extend(cross_metric_conflicts(self.document_id,
                                                    result.facts))
            result.relations = relations

            narrative_regions = [
                r for a in artifacts for r in a.regions
                if r.type in (RegionType.PARAGRAPH, RegionType.LIST,
                              RegionType.FOOTNOTE)]
            result.risks = extract_risks(
                self.document_id, narrative_regions,
                entity_id=model.entities.consolidated_id,
                entity_name=(model.entities.get(
                    model.entities.consolidated_id).name
                    if model.entities.get(model.entities.consolidated_id)
                    else None),
                period=model.primary_period)

            result.insights = generate_insights(self.document_id, result.facts,
                                                result.derived_metrics)
            result.anomalies = detect_anomalies(self.document_id, result.facts,
                                                result.derived_metrics)

        self._stage("analysis", StageStatus.SUCCEEDED,
                    total=len(result.facts), done=len(result.insights),
                    detail={"derived_metrics": len(result.derived_metrics),
                            "relations": len(result.relations),
                            "risks": len(result.risks),
                            "insights": len(result.insights),
                            "anomalies": len(result.anomalies),
                            "table_semantics": len(result.table_semantics)})

    # ------------------------------------------------------------- stage 11
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
