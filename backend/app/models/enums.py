"""Enumerations shared by the pipeline, the database and the API."""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum used across schemas, ORM columns and the API.

    Pydantic models are configured with ``use_enum_values=True``, so a field
    read back from a model is the plain ``str``.  Members therefore compare and
    hash exactly like their value -- use ``==`` / ``in``, never ``is``, when
    testing an enum-typed field.
    """
    __hash__ = str.__hash__  # keep members interchangeable with their value

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class DocumentStatus(StrEnum):
    """Lifecycle of a document.  ``COMPLETED`` is only ever set when every
    mandatory stage succeeded for at least the minimum viable page set."""
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    RENDERING = "RENDERING"
    OCR_PROCESSING = "OCR_PROCESSING"
    LAYOUT_ANALYSIS = "LAYOUT_ANALYSIS"
    TABLE_ANALYSIS = "TABLE_ANALYSIS"
    EXTRACTION = "EXTRACTION"
    VALIDATING_FACTS = "VALIDATING_FACTS"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {DocumentStatus.COMPLETED, DocumentStatus.PARTIALLY_COMPLETED,
                        DocumentStatus.REQUIRES_REVIEW, DocumentStatus.FAILED}


PIPELINE_ORDER: tuple[DocumentStatus, ...] = (
    DocumentStatus.VALIDATING,
    DocumentStatus.RENDERING,
    DocumentStatus.OCR_PROCESSING,
    DocumentStatus.LAYOUT_ANALYSIS,
    DocumentStatus.TABLE_ANALYSIS,
    DocumentStatus.EXTRACTION,
    DocumentStatus.VALIDATING_FACTS,
)


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RegionType(StrEnum):
    TITLE = "title"
    SECTION_HEADING = "section_heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    CHART = "chart"
    FIGURE = "figure"
    KPI_BLOCK = "kpi_block"
    LIST = "list"
    FOOTNOTE = "footnote"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    CAPTION = "caption"
    MISC = "misc"


class ChartType(StrEnum):
    BAR = "bar"
    COLUMN = "column"
    LINE = "line"
    PIE = "pie"
    DONUT = "donut"
    STACKED_BAR = "stacked_bar"
    AREA = "area"
    KPI_VISUAL = "kpi_visual"
    UNKNOWN = "unknown"


class CellRole(StrEnum):
    COLUMN_HEADER = "column_header"
    ROW_LABEL = "row_label"
    DATA = "data"
    CORNER = "corner"
    FOOTNOTE = "footnote"
    SUBHEADER = "subheader"


class ConfidenceBand(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class ValidationStatus(StrEnum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    NOT_CHECKED = "NOT_CHECKED"


class ValidationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class FactKind(StrEnum):
    """Where the fact came from, and what it asserts."""
    TABLE_CELL = "table_cell"
    NARRATIVE = "narrative"
    KPI = "kpi"
    CHART = "chart"
    DERIVED = "derived"


class ValueType(StrEnum):
    MONETARY = "monetary"
    PERCENTAGE = "percentage"
    RATIO = "ratio"
    COUNT = "count"
    BPS = "bps"
    PER_SHARE = "per_share"
    UNKNOWN = "unknown"


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    AED = "AED"
    UNKNOWN = "UNKNOWN"


class MagnitudeUnit(StrEnum):
    UNIT = "unit"
    THOUSAND = "thousand"
    LAKH = "lakh"
    MILLION = "million"
    CRORE = "crore"
    BILLION = "billion"
    TRILLION = "trillion"
    PERCENT = "percent"
    BPS = "bps"
    NONE = "none"


class PeriodType(StrEnum):
    QUARTER = "quarter"
    HALF_YEAR = "half_year"
    NINE_MONTH = "nine_month"
    FISCAL_YEAR = "fiscal_year"
    CALENDAR_YEAR = "calendar_year"
    TTM = "ttm"
    POINT_IN_TIME = "point_in_time"
    UNKNOWN = "unknown"


class GrowthType(StrEnum):
    YOY = "YoY"
    QOQ = "QoQ"
    SEQUENTIAL = "sequential"
    YTD = "YTD"
    UNKNOWN = "unknown"


class StatementNature(StrEnum):
    """Historical fact vs forward-looking statement -- kept strictly separate."""
    ACTUAL = "actual"
    GUIDANCE = "guidance"
    OUTLOOK = "outlook"
    TARGET = "target"


class CommentaryTopic(StrEnum):
    REVENUE = "revenue"
    PROFITABILITY = "profitability"
    MARGINS = "margins"
    DEMAND = "demand"
    COSTS = "costs"
    CAPEX = "capex"
    DEBT = "debt"
    SEGMENTS = "segments"
    OPERATIONS = "operations"
    RISKS = "risks"
    OUTLOOK = "outlook"
    GUIDANCE = "guidance"
    STRATEGY = "strategy"
    SUSTAINABILITY = "sustainability"
    LEADERSHIP_QUOTE = "leadership_quote"
    OTHER = "other"


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    COMMENT = "comment"
    REOPEN = "reopen"


class ReviewState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
