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
    PeriodBasis,
    AnomalyType, CellRole, ChartType, ColumnRole, CommentaryTopic,
    ConfidenceBand, Currency, DerivationStatus, DocumentStatus,
    DuplicateResolution, EntityType, FactKind, GrowthType, InsightType,
    JobState, MagnitudeUnit, MetricCategory, PeriodType, QualificationType, RegionType,
    RelationKind, ReviewAction, ReviewReason, ReviewState, RiskCategory,
    Severity,
    StageStatus, StatementNature, StatementType, TableType,
    ValidationSeverity, ValidationStatus, ValueType,
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
    #: The period as the resolver actually established it, dates and all.
    #: ``normalized_period`` is only its label; re-parsing that label loses the
    #: source's exact dates -- a 52/53-week filer's "Q3 FY25" ends 28 June, and
    #: the label alone re-derives 30 June.  Facts read this, not the label.
    period_resolved: NormalizedPeriod | None = None
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
    #: What the reconstruction concluded about this table: its shape (are the
    #: columns periods?) and the header block it read. Facts consult the shape
    #: to decide whether a missing period is a defect or a property of the
    #: table, which is a question nothing downstream could otherwise answer.
    meta: dict[str, Any] = Field(default_factory=dict)
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
    #: The exact dates the *source* supports.  When the document printed
    #: "Three Months Ended June 28, 2025" these are that period, to the day --
    #: not the calendar month end a fiscal label would imply.  A resolver that
    #: could not establish them leaves them None; None is the honest answer and
    #: a later stage must never fill it in from the label.
    start_date: str | None = None
    end_date: str | None = None
    is_fiscal: bool = True
    confidence: float = 0.0
    #: How this period was arrived at.  Set where the period is decided.
    basis: PeriodBasis = PeriodBasis.UNKNOWN

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


class Qualification(Base):
    """The basis a figure is stated on, when the source states one.

    Promoted only where it materially distinguishes financial facts: a
    results release's first page prints last year's quarter "excluding" a
    one-off gain, the statutory statement prints it including the gain, and
    both are the quarter's profit before tax. Without this they were one fact
    in conflict with itself.
    """
    qualification_type: QualificationType
    qualification_label: str
    #: column_head | footnote | row_label | table_title
    qualification_source: str
    footnote_reference: str | None = None
    source_text: str = ""


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
    entity_id: str | None = Field(
        default=None,
        description="Stable id of the reporting entity, so facts can be grouped "
                    "and rolled up without string matching on names.",
    )
    entity_type: EntityType = EntityType.UNKNOWN
    parent_entity_id: str | None = None
    statement: StatementType = StatementType.UNKNOWN
    category: MetricCategory = MetricCategory.OTHER
    kind: FactKind = FactKind.TABLE_CELL
    nature: StatementNature = StatementNature.ACTUAL
    derivation: DerivationStatus = Field(
        default=DerivationStatus.REPORTED,
        description="Whether the document stated this figure or the system "
                    "computed it. Never allowed to blur.",
    )
    formula: str | None = Field(
        default=None,
        description="Human-readable formula, for calculated facts only.",
    )
    input_fact_ids: list[str] = Field(
        default_factory=list,
        description="The facts a calculated value was computed from.",
    )

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
    growth_calculated: float | None = Field(
        default=None,
        description="Growth recomputed from the value pair, stored even when "
                    "the document reported its own figure, so the two can "
                    "always be compared.",
    )
    change_absolute: float | None = None
    change_bps: float | None = None
    duplicate_resolution: DuplicateResolution | None = None
    #: The stated basis of the figure, where the source gives one (Phase 22).
    qualification: Qualification | None = None
    unit_inferred: bool = False
    currency_inferred: bool = False

    confidence: float = 0.0
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    confidence_breakdown: ConfidenceBreakdown | None = None

    validation_status: ValidationStatus = ValidationStatus.NOT_CHECKED
    requires_review: bool = False
    #: Why, when it does. None whenever requires_review is False.
    review_reason: ReviewReason | None = None
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
    #: Wall time for this page across the whole page loop.
    duration_ms: float | None = None
    ocr_element_count: int = 0
    ocr_mean_confidence: float | None = None
    #: measured capture defects of the rendered page: skew, noise, contrast,
    #: illumination, sharpness and effective text height
    image_quality: dict[str, Any] = Field(default_factory=dict)
    #: which image the OCR text was read from.  Always the rendered page
    #: itself: no enhanced or intermediate image feeds extraction, so every
    #: bounding box addresses pixels the reader can be shown.
    ocr_image: str = "original"
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


# ===========================================================================
# Milestone 2 — financial intelligence schemas
# ===========================================================================
class EntityDto(Base):
    """A reporting unit named by the document."""
    entity_id: str
    entity_name: str
    entity_type: EntityType = EntityType.UNKNOWN
    parent_entity_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    source: str = "heading"
    first_page: int | None = None
    evidence: str = ""
    fact_count: int = 0


class ColumnSemantics(Base):
    """What a reconstructed table column means, beyond its header text."""
    index: int
    header: str = ""
    role: ColumnRole = ColumnRole.UNKNOWN
    period: str | None = None
    period_type: PeriodType | None = None
    fiscal_year: int | None = None
    quarter: int | None = None
    is_current: bool = False
    comparison_of: int | None = Field(
        default=None,
        description="Index of the column this one is a stated change against.")
    growth_type: GrowthType | None = None
    unit: MagnitudeUnit | None = None
    currency: Currency | None = None
    confidence: float = 0.0


class TableSemantics(Base):
    """Semantic interpretation layered over a reconstructed table."""
    table_id: str
    document_id: str
    page_number: int
    table_type: TableType = TableType.OTHER
    statement: StatementType = StatementType.UNKNOWN
    title: str = ""
    entity_id: str | None = None
    entity_name: str | None = None
    periods: list[str] = Field(default_factory=list)
    current_period: str | None = None
    comparison_period: str | None = None
    sequential_period: str | None = None
    unit: MagnitudeUnit | None = None
    currency: Currency | None = None
    columns: list[ColumnSemantics] = Field(default_factory=list)
    row_metrics: dict[str, str] = Field(
        default_factory=dict,
        description="Row index -> canonical metric, for rows the taxonomy knows.")
    confidence: float = 0.0
    notes: list[str] = Field(default_factory=list)


class DerivedMetric(Base):
    """A figure the system computed, with the arithmetic that produced it.

    Kept structurally distinct from a reported fact so a calculated number can
    never be presented as something management stated.
    """
    id: str
    document_id: str
    canonical_metric: str
    metric_display: str = ""
    entity_id: str | None = None
    entity_name: str | None = None
    period: str | None = None
    value: float | None = None
    base_value: float | None = Field(
        default=None,
        description="The result in absolute base units, which is what any "
                    "further calculation chains off.")
    value_type: ValueType = ValueType.UNKNOWN
    unit: MagnitudeUnit = MagnitudeUnit.NONE
    currency: Currency = Currency.UNKNOWN
    formula: str = ""
    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="Formula variable -> fact id.")
    input_fact_ids: list[str] = Field(
        default_factory=list,
        description="Every reported fact this figure was computed from.")
    derived_inputs: list[str] = Field(
        default_factory=list,
        description="Other derived metrics used as inputs, so a chain of "
                    "calculations stays traceable.")
    input_values: dict[str, float] = Field(default_factory=dict)
    reported_fact_id: str | None = Field(
        default=None,
        description="The document's own figure for the same measure, when it "
                    "published one, so the two can be compared.")
    reported_value: float | None = None
    agrees_with_reported: bool | None = None
    difference: float | None = None
    confidence: float = 0.0
    derivation: DerivationStatus = DerivationStatus.CALCULATED
    source_pages: list[int] = Field(default_factory=list)
    definition: str = ""
    notes: str = ""


class FactRelation(Base):
    """A typed link between two facts."""
    id: str
    document_id: str
    from_fact_id: str
    to_fact_id: str
    kind: RelationKind
    detail: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0


class RiskStatement(Base):
    """A source-supported risk or cautionary statement."""
    id: str
    document_id: str
    page_number: int
    region_id: str | None = None
    text: str
    category: RiskCategory = RiskCategory.OTHER
    categories: list[RiskCategory] = Field(default_factory=list)
    entity_id: str | None = None
    entity_name: str | None = None
    period: str | None = None
    trigger_phrase: str = ""
    severity: Severity = Severity.INFO
    is_forward_looking: bool = False
    confidence: float = 0.0
    bbox: BBoxModel | None = None


class Insight(Base):
    """A deterministic, source-backed observation.

    Every insight is generated by an explicit rule from facts that exist, and
    names those facts.  Nothing is inferred beyond what the arithmetic supports.
    """
    id: str
    document_id: str
    insight_type: InsightType
    headline: str
    detail: str = ""
    entity_id: str | None = None
    entity_name: str | None = None
    metric: str | None = None
    periods: list[str] = Field(default_factory=list)
    supporting_fact_ids: list[str] = Field(default_factory=list)
    value: float | None = None
    comparison_value: float | None = None
    change: float | None = None
    change_unit: str | None = None
    derivation: DerivationStatus = DerivationStatus.CALCULATED
    confidence: float = 0.0
    source_pages: list[int] = Field(default_factory=list)


class Anomaly(Base):
    """An explainable flag on something unusual in the extracted data."""
    id: str
    document_id: str
    anomaly_type: AnomalyType
    severity: Severity = Severity.MEDIUM
    message: str
    reason: str = Field(
        default="",
        description="Why this was flagged, in terms a reviewer can check.")
    entity_id: str | None = None
    entity_name: str | None = None
    metric: str | None = None
    period: str | None = None
    observed_value: float | None = None
    expected_value: float | None = None
    deviation: float | None = None
    threshold: float | None = None
    fact_ids: list[str] = Field(default_factory=list)
    source_pages: list[int] = Field(default_factory=list)
    requires_review: bool = True


class ConfidenceComponent(Base):
    """One named contribution to a fact's confidence, with its reason."""
    name: str
    value: float
    weight: float
    reason: str = ""


class FactExplanation(Base):
    """A deterministic, source-grounded answer to 'why do you believe this?'."""
    fact_id: str
    document_id: str
    metric: str
    metric_display: str = ""
    canonical_metric: str = ""
    metric_definition: str = ""
    value: float | None = None
    formatted_value: str = ""
    entity_name: str | None = None
    entity_type: EntityType = EntityType.UNKNOWN
    period: str | None = None
    period_basis: str = ""
    unit: MagnitudeUnit = MagnitudeUnit.NONE
    currency: Currency = Currency.UNKNOWN
    source_page: int = 0
    why_this_metric: str = ""
    why_this_period: str = ""
    why_this_entity: str = ""
    why_this_unit: str = ""
    confidence: float = 0.0
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    confidence_components: list[ConfidenceComponent] = Field(default_factory=list)
    validation_summary: list[dict[str, Any]] = Field(default_factory=list)
    derivation: DerivationStatus = DerivationStatus.REPORTED
    derivation_note: str = Field(
        default="",
        description="Whether the figure was reported or calculated, how any "
                    "change on it was obtained, and how a duplicate collision "
                    "was resolved.")
    formula: str | None = None
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    related_facts: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Provenance | None = None
    review_history: list[dict[str, Any]] = Field(default_factory=list)
    requires_review: bool = False
    generated_by: str = Field(
        default="deterministic templates over recorded evidence (no language "
                "model, no external API)",
        description="How this explanation was produced. Stated explicitly so a "
                    "reader knows no text here was generated by a model.")
