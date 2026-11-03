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
    ANALYSING = "ANALYSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    FAILED = "FAILED"
    #: The user stopped a run and it came to rest inside a checkpoint.  Its
    #: worker is still held and its state is still in memory, so resuming
    #: continues rather than replays.
    PAUSED = "PAUSED"
    #: The user stopped a run for good.  Partial extraction is discarded, so a
    #: cancelled document never presents half a result as a whole one.
    CANCELLED = "CANCELLED"
    #: A run that was in flight when the server stopped.  Nothing is claiming
    #: to be processing it any more, and saying so is better than leaving a
    #: progress bar that will never move again.
    INTERRUPTED = "INTERRUPTED"

    @property
    def is_terminal(self) -> bool:
        """Whether the document has come to rest, however it got there."""
        return self in {DocumentStatus.COMPLETED, DocumentStatus.PARTIALLY_COMPLETED,
                        DocumentStatus.REQUIRES_REVIEW, DocumentStatus.FAILED,
                        DocumentStatus.CANCELLED, DocumentStatus.INTERRUPTED}

    @property
    def is_active(self) -> bool:
        """Whether a worker is currently occupied with this document."""
        return self in {DocumentStatus.QUEUED, DocumentStatus.VALIDATING,
                        DocumentStatus.RENDERING, DocumentStatus.OCR_PROCESSING,
                        DocumentStatus.LAYOUT_ANALYSIS,
                        DocumentStatus.TABLE_ANALYSIS, DocumentStatus.EXTRACTION,
                        DocumentStatus.VALIDATING_FACTS,
                        DocumentStatus.ANALYSING, DocumentStatus.PAUSED}

    @property
    def has_results(self) -> bool:
        """Whether opening this document will show extracted data."""
        return self in {DocumentStatus.COMPLETED,
                        DocumentStatus.PARTIALLY_COMPLETED,
                        DocumentStatus.REQUIRES_REVIEW}


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
    #: the run was stopped by the user while this stage was in flight
    CANCELLED = "CANCELLED"


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
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The rule was considered for this scope and deliberately not run --
    typically because the document does not publish every figure the rule
    needs.  Distinct from NOT_CHECKED, which means no rule looked at all."""
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
    CHF = "CHF"
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


class PeriodBasis(StrEnum):
    """How a figure's reporting period came to be known.

    A period is only as trustworthy as the evidence behind it, and a reader
    deserves to know which kind they are looking at.  These four are what the
    resolver can actually distinguish, and each is set at the point the period
    is decided -- never inferred afterwards from the shape of the answer.
    """

    #: The source wrote the period itself: a "Q3 FY25" column head, a
    #: "Three Months Ended June 28, 2025" banner, an "As at" date.
    STATED = "stated"
    #: Built from structural evidence in the document -- a span phrase combined
    #: with a date from the column hierarchy, or a fiscal year established from
    #: the statement's own arithmetic.
    DERIVED = "derived"
    #: Taken from adjacent structure in the same table: a continuation
    #: fragment's parent, or a sibling column under the same banner.
    INHERITED = "inherited"
    #: Not established.  The figure keeps whatever exact date the source gave,
    #: if any, and claims nothing further.
    UNKNOWN = "unknown"
    #: The column this figure came from is not a reporting period at all -- a
    #: business segment, a geography, a unit declaration, a variance column.
    #:
    #: This is a different statement from UNKNOWN and the difference matters to
    #: a reader.  UNKNOWN means the document offered a period here and it could
    #: not be read, which is a gap worth reviewing.  NOT_APPLICABLE means the
    #: document never attributed a period to this column, so there is nothing
    #: to find and nothing to review.  Showing the same warning for both makes
    #: an ordinary segment table look broken.
    NOT_APPLICABLE = "not_applicable"


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


class ReviewReason(StrEnum):
    """Why a figure was put in front of a person.

    A review flag is only useful if it says what to look at. Before these
    existed the flag was raised by any validation that touched a fact, which
    meant a document where 35 figures had no period column marked all 35 as
    needing review -- and a reader could not tell those from a figure whose
    arithmetic had actually failed.

    The distinction these draw is between **the value** and **what is known
    about the value**. A number read cleanly off the page, with a bounding box
    and a strong metric match, is a good number even if nothing said which
    quarter it belongs to. That gap belongs in the period column, where it is
    already reported honestly; repeating it as a warning on the value says
    something untrue about the value.
    """

    #: An arithmetic or consistency check the figure took part in did not pass.
    VALIDATION_FAILED = "validation_failed"
    #: The same metric, entity and period was found twice with different
    #: values, and neither source is clearly better.
    CONFLICTING_SOURCES = "conflicting_sources"
    #: The cell was located but its text could not be read as a number.
    VALUE_UNPARSED = "value_unparsed"
    #: A second, independent read of the cell -- the cell alone, at twice the
    #: resolution -- disagrees with the page read about the digits or the
    #: sign. Both readings are kept; neither is chosen (Phase 22).
    VALUE_DISPUTED = "value_disputed"
    #: A monetary figure in a table that never declared its magnitude, so the
    #: scale of the number is genuinely unknown.
    UNIT_UNDETERMINED = "unit_undetermined"
    #: The figure is outside any plausible range for what it claims to be.
    IMPLAUSIBLE_VALUE = "implausible_value"
    #: The table sets out several reporting periods and this column could not
    #: be safely assigned to one of them.
    AMBIGUOUS_PERIOD = "ambiguous_period"
    #: Extraction confidence is low -- typically poor OCR on the source cell.
    LOW_CONFIDENCE = "low_confidence"
    #: Two lines of one table were read as this measure with different
    #: figures, or the table says the name means something other than usual,
    #: and which line or meaning is this figure's is not certain (Phase 22).
    METRIC_AMBIGUOUS = "metric_ambiguous"
    #: Read from a sentence, a highlight tile or a chart -- not from a table --
    #: and no table in the document gives the same figure. Prose has no
    #: column head to fix the period or a caption to fix the scale, and a
    #: human audit found half such figures wrong in some dimension (Phase 22).
    NARRATIVE_UNCONFIRMED = "narrative_unconfirmed"


#: One sentence per reason, written for the person being asked to look.
REVIEW_REASON_TEXT: dict[str, str] = {
    ReviewReason.VALIDATION_FAILED:
        "A consistency check involving this figure did not pass. Compare it "
        "with the related figures on the same statement.",
    ReviewReason.CONFLICTING_SOURCES:
        "This metric appears more than once for the same entity and period "
        "with different values, and neither source is clearly better.",
    ReviewReason.VALUE_UNPARSED:
        "The cell was found on the page but its text could not be read as a "
        "number.",
    ReviewReason.VALUE_DISPUTED:
        "A second read of this cell alone, at twice the resolution, gives "
        "different digits or a different sign. Both readings are kept; check "
        "the figure against the page.",
    ReviewReason.UNIT_UNDETERMINED:
        "The table never declared whether it is stated in thousands, millions "
        "or crore, so the scale of this figure is not established.",
    ReviewReason.IMPLAUSIBLE_VALUE:
        "The value falls outside a plausible range for this kind of figure.",
    ReviewReason.AMBIGUOUS_PERIOD:
        "The table sets out several reporting periods and this value could "
        "not be safely assigned to one of them.",
    ReviewReason.LOW_CONFIDENCE:
        "The source cell was read with low confidence, usually because the "
        "page image is unclear at that point.",
    ReviewReason.METRIC_AMBIGUOUS:
        "Which measure this figure is cannot be settled from the table: two "
        "of its lines read as this measure with different figures, or the "
        "table uses the name in another sense.",
    ReviewReason.NARRATIVE_UNCONFIRMED:
        "This figure was read from a sentence or a highlight, not from a "
        "table, and no table in the document confirms it. Check the measure, "
        "the period and the scale against the text.",
}


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


# ===========================================================================
# Milestone 2 — financial intelligence vocabulary
# ===========================================================================
class StatementType(StrEnum):
    """Which formal financial statement a metric or table belongs to."""
    INCOME_STATEMENT = "income_statement"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    EQUITY = "equity"
    RATIO = "ratio"
    SEGMENT = "segment"
    OPERATIONAL = "operational"
    NOTES = "notes"
    UNKNOWN = "unknown"


class MetricCategory(StrEnum):
    """Analytical grouping, orthogonal to the statement a metric sits on."""
    REVENUE = "revenue"
    EARNINGS = "earnings"
    COSTS = "costs"
    MARGINS = "margins"
    RETURNS = "returns"
    LEVERAGE = "leverage"
    LIQUIDITY = "liquidity"
    CASHFLOW = "cashflow"
    INVESTMENT = "investment"
    PER_SHARE = "per_share"
    BALANCE_SHEET = "balance_sheet"
    SEGMENT = "segment"
    OPERATIONAL = "operational"
    OTHER = "other"


class EntityType(StrEnum):
    """What kind of reporting unit a fact belongs to.

    ``CONSOLIDATED`` and ``STANDALONE`` are reporting *bases* of the filing
    company; the rest are distinct reporting units beneath it.
    """
    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"
    COMPANY = "company"
    SUBSIDIARY = "subsidiary"
    SEGMENT = "segment"
    BUSINESS_UNIT = "business_unit"
    JOINT_VENTURE = "joint_venture"
    ASSOCIATE = "associate"
    UNKNOWN = "unknown"


class TableType(StrEnum):
    """Semantic role of a reconstructed table."""
    FINANCIAL_RESULTS = "financial_results"
    INCOME_STATEMENT = "income_statement"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    SEGMENT_RESULTS = "segment_results"
    OPERATIONAL_METRICS = "operational_metrics"
    RATIO_TABLE = "ratio_table"
    PRICE_TABLE = "price_table"
    OTHER = "other"


class ColumnRole(StrEnum):
    """What a reconstructed table column carries."""
    LABEL = "label"
    SERIAL = "serial"
    UNIT_OF_MEASURE = "unit_of_measure"
    PERIOD_VALUE = "period_value"
    CHANGE = "change"
    SHARE = "share"
    NOTE = "note"
    UNKNOWN = "unknown"


class DerivationStatus(StrEnum):
    """Whether a figure was stated by the document or computed here.

    The distinction is never allowed to blur: presenting a calculated number as
    management-reported would misrepresent the source.
    """
    REPORTED = "reported"
    CALCULATED = "calculated"
    REVIEWED = "reviewed"


class RelationKind(StrEnum):
    """Typed links between facts."""
    COMPARISON = "comparison"          # prior-period counterpart
    GROWTH_OF = "growth_of"            # growth figure derived from a pair
    INPUT_TO = "input_to"              # this fact feeds a derived metric
    DERIVED_FROM = "derived_from"      # inverse of INPUT_TO
    COMPONENT_OF = "component_of"      # segment value rolling into a total
    DUPLICATE_OF = "duplicate_of"      # same measure restated elsewhere
    CONFLICTS_WITH = "conflicts_with"  # same key, disagreeing values
    RESTATES = "restates"              # narrative restating a table figure


class DuplicateResolution(StrEnum):
    """Why two facts with a similar identity are or are not the same thing."""
    IDENTICAL_REPEAT = "identical_repeat"
    DIFFERENT_ENTITY = "different_entity"
    DIFFERENT_PERIOD = "different_period"
    DIFFERENT_DEFINITION = "different_definition"
    DIFFERENT_UNIT = "different_unit"
    DIFFERENT_SCOPE = "different_scope"
    GENUINE_CONFLICT = "genuine_conflict"
    UNCERTAIN = "uncertain"
    #: The same measure, entity and period on two stated bases -- "excluding
    #: an exceptional gain" beside the statutory figure. Two facts, not a
    #: contradiction.
    DIFFERENT_QUALIFICATION = "different_qualification"


class QualificationType(StrEnum):
    """The basis a figure is stated on, where the source states one that makes
    it a different figure from the plain measure (Phase 22)."""
    REPORTED = "reported"
    ADJUSTED = "adjusted"
    UNDERLYING = "underlying"
    RESTATED = "restated"
    PRO_FORMA = "pro_forma"
    LIKE_FOR_LIKE = "like_for_like"
    CONTINUING_OPERATIONS = "continuing_operations"
    DISCONTINUED_OPERATIONS = "discontinued_operations"
    BEFORE_EXCEPTIONAL = "before_exceptional"
    AFTER_EXCEPTIONAL = "after_exceptional"
    EXCLUDING = "excluding"
    INCLUDING = "including"
    OTHER = "other"


class RiskCategory(StrEnum):
    COMMODITY_PRICE = "commodity_price"
    DEMAND = "demand"
    REGULATORY = "regulatory"
    CURRENCY = "currency"
    SUPPLY_CHAIN = "supply_chain"
    MACROECONOMIC = "macroeconomic"
    OPERATIONAL = "operational"
    COMPETITIVE = "competitive"
    CREDIT = "credit"
    GEOPOLITICAL = "geopolitical"
    OTHER = "other"


class InsightType(StrEnum):
    GROWTH = "growth"
    MARGIN_MOVEMENT = "margin_movement"
    SEGMENT_CONTRIBUTION = "segment_contribution"
    SEGMENT_OUTPERFORMANCE = "segment_outperformance"
    LEVERAGE_CHANGE = "leverage_change"
    CASH_POSITION = "cash_position"
    CAPEX_INTENSITY = "capex_intensity"
    PROFITABILITY = "profitability"


class AnomalyType(StrEnum):
    REPORTED_VS_CALCULATED = "reported_vs_calculated"
    UNUSUAL_GROWTH = "unusual_growth"
    MARGIN_SWING = "margin_swing"
    SEGMENT_TOTAL_MISMATCH = "segment_total_mismatch"
    UNIT_CHANGE = "unit_change"
    CONTRADICTORY_DUPLICATE = "contradictory_duplicate"
    SIGN_FLIP = "sign_flip"
    OUTLIER_VALUE = "outlier_value"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
