"""Pydantic domain schemas.

These are the in-process contracts between pipeline stages *and* the API
response models.  They deliberately keep the *raw* observation next to the
normalised interpretation so nothing the document said is ever lost.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    CellRole, ChartType, CommentaryTopic, ConfidenceBand, Currency, DocumentStatus,
    FactKind, GrowthType, JobState, MagnitudeUnit, PeriodType, RegionType,
    ReviewAction, ReviewState, StageStatus, StatementNature, ValidationSeverity,
    ValidationStatus, ValueType,
)


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True,
                              use_enum_values=True)


class BBoxModel(Base):
    """Serialisable bounding box.

    Mirrors the read-only geometry API of ``app.core.geometry.BBox`` so callers
    can reason about position without converting at every site; ``.geom``
    returns the full-featured dataclass when set operations are needed.
    """
    x: float
    y: float
    width: float
    height: float

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height

    @property
    def cx(self) -> float:
        return self.x + self.width / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.height / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def geom(self) -> Any:
        from app.core.geometry import BBox  # local import: avoids a cycle
        return BBox(self.x, self.y, self.width, self.height)

    # -- set operations, delegated so both box types expose one API ----------
    def _other(self, other: Any) -> Any:
        return other.geom if isinstance(other, BBoxModel) else other

    def intersection(self, other: Any) -> Any:
        return self.geom.intersection(self._other(other))

    def iou(self, other: Any) -> float:
        return self.geom.iou(self._other(other))

    def contains_ratio(self, other: Any) -> float:
        return self.geom.contains_ratio(self._other(other))

    def h_overlap(self, other: Any) -> float:
        return self.geom.h_overlap(self._other(other))

    def v_overlap(self, other: Any) -> float:
        return self.geom.v_overlap(self._other(other))

    def h_overlap_ratio(self, other: Any) -> float:
        return self.geom.h_overlap_ratio(self._other(other))

    def v_overlap_ratio(self, other: Any) -> float:
        return self.geom.v_overlap_ratio(self._other(other))

    def padded(self, pad: float) -> Any:
        return self.geom.padded(pad)

    def clipped(self, w: float, h: float) -> Any:
        return self.geom.clipped(w, h)

    def to_relative(self, page_w: float, page_h: float) -> dict[str, float]:
        return self.geom.to_relative(page_w, page_h)


# --------------------------------------------------------------------- OCR
class OCRElement(Base):
    """A single recognised text unit with its spatial footprint.

    ``source`` records which evidence channel produced it:
    ``ocr`` (pixels, canonical) or ``text_layer`` (auxiliary PDF glyph stream).
    """
    id: str
    document_id: str
    page_number: int
    text: str
    confidence: float
    bbox: BBoxModel
    polygon: list[list[float]] | None = None
    block_id: str | None = None
    line_id: str | None = None
    word_id: str | None = None
    source: Literal["ocr", "text_layer", "merged"] = "ocr"
    reading_index: int | None = None
    font_size_px: float | None = None
    is_bold: bool | None = None
    corroborated: bool = Field(
        default=False,
        description="True when the auxiliary PDF text layer agrees with the OCR "
                    "reading of this element; used to raise confidence.",
    )
    aux_text: str | None = Field(
        default=None,
        description="Text-layer reading of the same region, when available. "
                    "Kept verbatim: it is evidence, not a replacement.",
    )
    currency_glyph: str | None = Field(
        default=None,
        description="Currency symbol recovered from the text layer when the OCR "
                    "charset could not emit it (e.g. the rupee sign).",
    )


class PageOCRResult(Base):
    page_number: int
    engine: str
    engine_version: str
    elements: list[OCRElement]
    mean_confidence: float
    duration_ms: float
    image_width: int
    image_height: int
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ layout
class Region(Base):
    id: str
    document_id: str
    page_number: int
    type: RegionType
    bbox: BBoxModel
    confidence: float
    reading_order: int
    column_index: int = 0
    text: str = ""
    element_ids: list[str] = Field(default_factory=list)
    parent_region_id: str | None = None
    detector: str = "heuristic"
    meta: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ tables
class TableCell(Base):
    id: str
    table_id: str
    document_id: str
    page_number: int
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    text: str
    role: CellRole = CellRole.DATA
    bbox: BBoxModel
    confidence: float = 0.0
    numeric_value: float | None = None
    raw_numeric_text: str | None = None
    is_numeric: bool = False
    element_ids: list[str] = Field(default_factory=list)


class TableColumn(Base):
    index: int
    header: str = ""
    header_path: list[str] = Field(default_factory=list)
    x_start: float = 0.0
    x_end: float = 0.0
    period_label: str | None = None
    normalized_period: str | None = None
    is_growth_column: bool = False
    growth_type: GrowthType | None = None
    numeric_ratio: float = 0.0


class ExtractedTable(Base):
    id: str
    document_id: str
    page_number: int
    region_id: str | None = None
    title: str = ""
    caption: str = ""
    bbox: BBoxModel
    n_rows: int
    n_cols: int
    columns: list[TableColumn] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)
    detector: str = "ruling+projection"
    confidence: float = 0.0
    has_ruling_lines: bool = False
    unit_hint: MagnitudeUnit | None = None
    currency_hint: Currency | None = None
    footnotes: list[str] = Field(default_factory=list)
    continued_from_table_id: str | None = None
    warnings: list[str] = Field(default_factory=list)

    def grid(self) -> list[list[str]]:
        g = [["" for _ in range(self.n_cols)] for _ in range(self.n_rows)]
        for c in self.cells:
            if 0 <= c.row < self.n_rows and 0 <= c.col < self.n_cols:
                g[c.row][c.col] = c.text
        return g


# ------------------------------------------------------------------ charts
class ChartSeriesPoint(Base):
    label: str | None = None
    value: float | None = None
    value_source: Literal["data_label", "axis_inferred", "unknown"] = "unknown"
    confidence: float = 0.0
    bbox: BBoxModel | None = None


class ExtractedChart(Base):
    id: str
    document_id: str
    page_number: int
    region_id: str | None = None
    chart_type: ChartType = ChartType.UNKNOWN
    type_confidence: float = 0.0
    title: str = ""
    axis_labels: list[str] = Field(default_factory=list)
    legend_labels: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    data_labels: list[ChartSeriesPoint] = Field(default_factory=list)
    unit_hint: MagnitudeUnit | None = None
    bbox: BBoxModel
    confidence: float = 0.0
    values_extracted: bool = False
    requires_review: bool = True
    notes: str = ""


# ------------------------------------------------------------ normalisation
class NormalizedNumber(Base):
    value: float
    raw_text: str
    is_negative: bool = False
    negative_from_parentheses: bool = False
    value_type: ValueType = ValueType.UNKNOWN
    magnitude: MagnitudeUnit = MagnitudeUnit.NONE
    currency: Currency = Currency.UNKNOWN
    currency_symbol: str | None = None
    currency_inferred: bool = Field(
        default=False,
        description="True when the currency was deduced from context (e.g. an "
                    "India-only magnitude word) rather than read as a symbol. "
                    "Kept distinct so a reader can tell the two apart.",
    )
    base_value: float | None = Field(
        default=None,
        description="Value converted to base units (currency units, not lakh/"
                    "crore/million).  None when the magnitude is unknown.",
    )
    confidence: float = 1.0


class NormalizedPeriod(Base):
    label: str
    raw_text: str
    period_type: PeriodType = PeriodType.UNKNOWN
    fiscal_year: int | None = None
    quarter: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_fiscal: bool = True
    confidence: float = 0.0

    @property
    def sort_key(self) -> tuple:
        return (self.fiscal_year or 0, self.quarter or 0)


# ------------------------------------------------------------------- facts
class Provenance(Base):
    document_id: str
    page_number: int
    region_id: str | None = None
    table_id: str | None = None
    cell_id: str | None = None
    chart_id: str | None = None
    bbox: BBoxModel | None = None
    bbox_relative: BBoxModel | None = None
    original_text: str = ""
    ocr_element_ids: list[str] = Field(default_factory=list)
    page_image_url: str | None = None
    crop_url: str | None = None
    extraction_method: str = ""


class ConfidenceBreakdown(Base):
    ocr: float = 0.0
    layout: float = 0.0
    structure: float = 0.0
    semantic: float = 0.0
    consistency: float = 0.0
    corroboration: float = 0.0
    overall: float = 0.0
    band: ConfidenceBand = ConfidenceBand.LOW
    notes: list[str] = Field(default_factory=list)


class FinancialFact(Base):
    fact_id: str
    document_id: str
    metric: str = Field(description="Metric label exactly as it appears in the source")
    canonical_metric: str = Field(description="Key in the financial taxonomy")
    metric_display: str = ""
    entity: str | None = Field(
        default=None,
        description="Reporting entity/segment the fact belongs to, inferred from "
                    "the enclosing section (e.g. a subsidiary or business segment).",
    )
    kind: FactKind = FactKind.TABLE_CELL
    nature: StatementNature = StatementNature.ACTUAL

    value: float | None = None
    raw_value: str = ""
    value_type: ValueType = ValueType.UNKNOWN
    currency: Currency = Currency.UNKNOWN
    unit: MagnitudeUnit = MagnitudeUnit.NONE
    base_value: float | None = None

    period: str | None = None
    period_normalized: NormalizedPeriod | None = None
    comparison_period: str | None = None
    comparison_value: float | None = None

    growth: float | None = None
    growth_type: GrowthType | None = None
    growth_reported: bool = False

    confidence: float = 0.0
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    confidence_breakdown: ConfidenceBreakdown | None = None

    validation_status: ValidationStatus = ValidationStatus.NOT_CHECKED
    requires_review: bool = False
    review_state: ReviewState = ReviewState.PENDING

    source_page: int = 0
    provenance: Provenance | None = None
    original_text: str = ""
    context_text: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------- commentary/guidance
class Commentary(Base):
    id: str
    document_id: str
    page_number: int
    region_id: str | None = None
    text: str
    topics: list[CommentaryTopic] = Field(default_factory=list)
    primary_topic: CommentaryTopic = CommentaryTopic.OTHER
    sentiment: Literal["positive", "neutral", "negative", "mixed"] = "neutral"
    speaker: str | None = None
    is_quote: bool = False
    confidence: float = 0.0
    bbox: BBoxModel | None = None
    summary: str | None = None
    summary_engine: str | None = None
    mentioned_metrics: list[str] = Field(default_factory=list)


class Guidance(Base):
    id: str
    document_id: str
    page_number: int
    region_id: str | None = None
    text: str
    nature: StatementNature = StatementNature.GUIDANCE
    canonical_metric: str | None = None
    metric: str | None = None
    target_value: float | None = None
    target_unit: MagnitudeUnit | None = None
    target_currency: Currency | None = None
    target_period: str | None = None
    direction: Literal["increase", "decrease", "maintain", "unspecified"] = "unspecified"
    trigger_phrase: str = ""
    confidence: float = 0.0
    bbox: BBoxModel | None = None
    requires_review: bool = False


# ------------------------------------------------------------- validation
class ValidationResult(Base):
    id: str
    document_id: str
    rule: str
    rule_family: str
    status: ValidationStatus
    severity: ValidationSeverity = ValidationSeverity.WARNING
    message: str
    reported_value: float | None = None
    calculated_value: float | None = None
    difference: float | None = None
    tolerance: float | None = None
    fact_ids: list[str] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


# -------------------------------------------------------------- documents
class PageInfo(Base):
    id: str
    document_id: str
    page_number: int
    pdf_width: float
    pdf_height: float
    render_width: int
    render_height: int
    dpi: int
    rotation: int = 0
    image_url: str | None = None
    thumbnail_url: str | None = None
    has_text_layer: bool = False
    text_layer_chars: int = 0
    ocr_engine: str | None = None
    ocr_signature: str | None = None
    ocr_duration_ms: float | None = None
    ocr_element_count: int = 0
    ocr_mean_confidence: float | None = None
    section_title: str | None = None
    entity_hint: str | None = None
    region_count: int = 0
    table_count: int = 0
    chart_count: int = 0
    is_scanned: bool = False
    status: StageStatus = StageStatus.PENDING
    error: str | None = None


class StageRecord(Base):
    stage: str
    status: StageStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    items_total: int = 0
    items_done: int = 0
    items_failed: int = 0
    engine: str | None = None
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class DocumentSummary(Base):
    id: str
    filename: str
    original_filename: str
    content_hash: str
    size_bytes: int
    page_count: int = 0
    status: DocumentStatus
    title: str | None = None
    company_name: str | None = None
    report_period: str | None = None
    report_type: str | None = None
    document_currency: Currency | None = None
    document_unit: MagnitudeUnit | None = None
    is_scanned: bool = False
    created_at: datetime
    updated_at: datetime
    processing_started_at: datetime | None = None
    processing_finished_at: datetime | None = None
    processing_ms: float | None = None
    error: str | None = None
    fact_count: int = 0
    table_count: int = 0
    chart_count: int = 0
    commentary_count: int = 0
    guidance_count: int = 0
    validation_warning_count: int = 0
    review_queue_count: int = 0
    pages_processed: int = 0
    pages_failed: int = 0


class ProcessingStatus(Base):
    document_id: str
    status: DocumentStatus
    progress: float = 0.0
    current_stage: str | None = None
    stages: list[StageRecord] = Field(default_factory=list)
    pages_total: int = 0
    pages_done: int = 0
    pages_failed: int = 0
    message: str | None = None
    error: str | None = None
    updated_at: datetime | None = None


class JobInfo(Base):
    id: str
    document_id: str
    job_type: str
    state: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int = 0
    error: str | None = None


# ----------------------------------------------------------------- review
class ReviewRecord(Base):
    id: str
    document_id: str
    target_type: Literal["fact", "table_cell", "chart", "commentary", "guidance"]
    target_id: str
    action: ReviewAction
    reviewer: str = "analyst"
    previous_value: dict[str, Any] | None = None
    new_value: dict[str, Any] | None = None
    note: str | None = None
    created_at: datetime


class ReviewRequest(Base):
    target_type: Literal["fact", "table_cell", "chart", "commentary", "guidance"] = "fact"
    target_id: str
    action: ReviewAction
    reviewer: str = "analyst"
    note: str | None = None
    new_value: dict[str, Any] | None = None
