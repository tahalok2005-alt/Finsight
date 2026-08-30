"""Canonical financial metric taxonomy.

Terminology varies between filers and between statements, so labels are mapped
onto canonical keys.  Crucially, *near-synonyms are not collapsed*: "Revenue",
"Gross Revenue", "Revenue from Operations" and "Net Revenue" are related but
different measures, so each keeps its own canonical key and they are linked
through ``family`` instead.  Merging them would silently corrupt any comparison.

Each entry declares:

* ``aliases``    - surface forms seen in filings (matched case/punctuation-insensitively)
* ``family``     - grouping used for roll-ups and comparison views
* ``value_type`` - what kind of quantity it is, used to sanity-check parsing
* ``sign``       - "positive" metrics that should not normally be negative
* ``is_flow``    - flow (period) vs stock (point-in-time) measure
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from app.models.enums import ValueType

_PUNCT = re.compile(r"[^a-z0-9%&/ ]+")
_WS = re.compile(r"\s+")
_NOISE = re.compile(
    r"\b(total|consolidated|standalone|reported|for the (period|quarter|year)|"
    r"as per|ind as|gaap|the|and|of|in|on|at|rs|inr|crore|cr|million|mn|"
    r"billion|bn|lakh|figures?)\b")
_TRAILING_UNIT = re.compile(r"\((?:%|rs|inr|₹|\$|in [^)]*)\)\s*$", re.I)
# any short trailing parenthetical, used only for a fallback match attempt
_TRAILING_PAREN = re.compile(r"\s*\([^)]{1,30}\)\s*$")
_FOOTNOTE_MARK = re.compile(r"[*#†‡^]+|\(\d\)|\[\d\]")


def normalize_label(text: str) -> str:
    """Canonical form used for alias lookup and fuzzy comparison."""
    t = (text or "").lower().strip()
    t = _FOOTNOTE_MARK.sub(" ", t)
    t = _TRAILING_UNIT.sub(" ", t)
    t = t.replace("&", " and ")
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t


def _strip_noise(t: str) -> str:
    return _WS.sub(" ", _NOISE.sub(" ", t)).strip()


@dataclass(frozen=True, slots=True)
class MetricDef:
    key: str
    display: str
    family: str
    aliases: tuple[str, ...]
    value_type: ValueType = ValueType.MONETARY
    sign: str = "any"            # "positive" | "any"
    is_flow: bool = True
    is_operational: bool = False
    notes: str = ""


def _m(key: str, display: str, family: str, aliases: list[str], **kw) -> MetricDef:
    return MetricDef(key=key, display=display, family=family,
                     aliases=tuple(aliases), **kw)


PCT = ValueType.PERCENTAGE
MON = ValueType.MONETARY
CNT = ValueType.COUNT
RAT = ValueType.RATIO
PS = ValueType.PER_SHARE

METRICS: tuple[MetricDef, ...] = (
    # ---------------------------------------------------------- revenue family
    # NOTE: bare "income" is deliberately NOT an alias of revenue -- it appears
    # inside many unrelated line items ("income tax relating to...").
    _m("revenue", "Revenue", "revenue",
       ["revenue", "total revenue", "value of sales and services",
        "value of sales & services (revenue)"],
       sign="positive"),
    _m("total_income", "Total Income", "revenue",
       ["total income"], sign="positive"),
    _m("investment_income", "Investment Income", "revenue",
       ["investment income", "income from investments"]),
    _m("gross_revenue", "Gross Revenue", "revenue",
       ["gross revenue", "gross turnover", "gross sales"], sign="positive"),
    _m("revenue_from_operations", "Revenue from Operations", "revenue",
       ["revenue from operations", "operating revenue", "operational revenue",
        "revenue from operation", "income from operations"], sign="positive"),
    _m("net_revenue", "Net Revenue", "revenue",
       ["net revenue", "net sales", "net turnover"], sign="positive"),
    _m("sales", "Sales", "revenue", ["sales", "turnover"], sign="positive"),
    _m("segment_revenue", "Segment Revenue", "revenue",
       ["segment revenue", "segment sales", "revenue by segment"], sign="positive"),
    _m("other_income", "Other Income", "revenue", ["other income"]),

    # ---------------------------------------------------------- profitability
    _m("ebitda", "EBITDA", "earnings",
       ["ebitda", "earnings before interest tax depreciation and amortisation",
        "operating profit before depreciation"]),
    _m("segment_ebitda", "Segment EBITDA", "earnings",
       ["segment ebitda", "ebitda by segment"]),
    _m("ebit", "EBIT", "earnings",
       ["ebit", "earnings before interest and tax", "operating profit"]),
    _m("pbt", "Profit Before Tax", "earnings",
       ["profit before tax", "pbt", "profit before taxes",
        "profit before exceptional items and tax"]),
    _m("pat", "Profit After Tax", "earnings",
       ["profit after tax", "pat", "profit for the period", "profit for the year",
        "net profit"]),
    # A distinct measure from PAT: a results table lists both, and collapsing
    # them would make a period-over-period comparison compare different things.
    _m("pat_after_associates",
       "Profit After Tax incl. Share of Associates & JVs", "earnings",
       ["profit after tax and share of profit loss of associates and jvs",
        "profit after tax and share of profit loss of associates and jv",
        "profit after tax and share of profit loss of associates and joint ventures",
        "profit after tax and share of profit of associates and jvs",
        "profit after tax and share of"]),
    _m("net_income", "Net Income", "earnings",
       ["net income", "net earnings", "profit attributable to owners"]),
    _m("segment_profit", "Segment Profit", "earnings",
       ["segment profit", "segment results", "segment result"]),
    _m("share_of_associates", "Share of Profit of Associates & JV", "earnings",
       ["share of profit loss of associates and jv", "share of profit of associates",
        "share of profit loss of associates and joint ventures",
        "share of associates"]),
    _m("exceptional_items", "Exceptional Items", "earnings",
       ["exceptional items", "exceptional item"]),

    # ----------------------------------------------------------------- margins
    _m("ebitda_margin", "EBITDA Margin", "margins",
       ["ebitda margin", "ebitda margins", "ebitda %", "ebitda margin %"],
       value_type=PCT),
    _m("ebit_margin", "EBIT Margin", "margins",
       ["ebit margin", "operating margin", "operating profit margin"], value_type=PCT),
    _m("gross_margin", "Gross Margin", "margins",
       ["gross margin", "gross profit margin"], value_type=PCT),
    _m("net_margin", "Net Margin", "margins",
       ["net margin", "net profit margin", "pat margin"], value_type=PCT),

    # --------------------------------------------------------------- cost side
    _m("depreciation", "Depreciation", "costs",
       ["depreciation", "depreciation and amortisation", "depreciation amortisation",
        "d and a", "depreciation, amortisation and depletion"], sign="positive"),
    _m("finance_costs", "Finance Costs", "costs",
       ["finance costs", "finance cost", "interest costs", "interest expense",
        "finance charges"], sign="positive"),
    _m("tax_expense", "Tax Expense", "costs",
       ["tax expenses", "tax expense", "total tax expense", "income tax expense",
        "current and deferred tax"]),
    _m("current_tax", "Current Tax", "costs", ["current tax"]),
    _m("deferred_tax", "Deferred Tax", "costs", ["deferred tax"]),
    # Distinct homes for expense lines that would otherwise be force-fitted onto
    # the nearest known metric by fuzzy matching.
    _m("total_expenses", "Total Expenses", "costs",
       ["total expenses", "total expense"], sign="positive"),
    _m("other_expenses", "Other Expenses", "costs",
       ["other expenses", "other expense"], sign="positive"),
    _m("other_comprehensive_income", "Other Comprehensive Income", "earnings",
       ["other comprehensive income", "total comprehensive income"]),
    _m("employee_cost", "Employee Cost", "costs",
       ["employee benefit expense", "employee costs", "staff costs"], sign="positive"),
    _m("raw_material_cost", "Raw Material Cost", "costs",
       ["cost of materials consumed", "raw material cost", "cost of goods sold",
        "cogs"], sign="positive"),

    # --------------------------------------------------------------- per share
    _m("eps", "EPS", "per_share",
       ["eps", "earnings per share", "basic eps", "earning per share",
        "basic earnings per share"], value_type=PS),
    _m("diluted_eps", "Diluted EPS", "per_share",
       ["diluted eps", "diluted earnings per share"], value_type=PS),

    # ------------------------------------------------------ balance sheet/cash
    _m("capex", "Capital Expenditure", "investment",
       ["capital expenditure", "capex", "capital expenditure incurred",
        "capital investment"], sign="positive"),
    _m("cash", "Cash", "liquidity", ["cash"], is_flow=False, sign="positive"),
    _m("cash_and_equivalents", "Cash & Cash Equivalents", "liquidity",
       ["cash and cash equivalents", "cash and bank balances",
        "cash and cash equivalent"], is_flow=False, sign="positive"),
    _m("debt", "Debt", "leverage", ["debt", "borrowings", "total borrowings"],
       is_flow=False, sign="positive"),
    _m("outstanding_debt", "Outstanding Debt", "leverage",
       ["outstanding debt", "gross debt", "total debt"], is_flow=False,
       sign="positive"),
    _m("net_debt", "Net Debt", "leverage", ["net debt"], is_flow=False),
    _m("net_debt_to_ebitda", "Net Debt to EBITDA", "leverage",
       ["net debt to ebitda", "net debt ebitda", "net debt / ebitda",
        "leverage ratio"], value_type=RAT, is_flow=False),
    _m("working_capital", "Working Capital", "liquidity",
       ["working capital", "net working capital"], is_flow=False),
    _m("net_worth", "Net Worth", "leverage",
       ["net worth", "shareholders funds", "total equity", "equity"],
       is_flow=False, sign="positive"),
    _m("total_assets", "Total Assets", "balance_sheet",
       ["total assets"], is_flow=False, sign="positive"),

    # ---------------------------------------------------------------- cashflow
    _m("operating_cash_flow", "Operating Cash Flow", "cashflow",
       ["operating cash flow", "cash flow from operations", "cfo",
        "net cash from operating activities"]),
    _m("free_cash_flow", "Free Cash Flow", "cashflow",
       ["free cash flow", "fcf"]),
    _m("investing_cash_flow", "Investing Cash Flow", "cashflow",
       ["net cash used in investing activities", "cash flow from investing"]),
    _m("financing_cash_flow", "Financing Cash Flow", "cashflow",
       ["net cash from financing activities", "cash flow from financing"]),

    # ------------------------------------------------------------------ ratios
    _m("roe", "ROE", "returns",
       ["roe", "return on equity"], value_type=PCT),
    _m("roce", "ROCE", "returns",
       ["roce", "return on capital employed"], value_type=PCT),
    _m("roa", "ROA", "returns", ["roa", "return on assets"], value_type=PCT),

    # ------------------------------------------------------------ operational
    _m("subscribers", "Subscribers", "operational",
       ["subscriber base", "total subscribers", "subscribers",
        "customer base", "total customer base"], value_type=CNT,
       is_operational=True, sign="positive"),
    _m("arpu", "ARPU", "operational",
       ["arpu", "average revenue per user", "arpu per month"],
       value_type=MON, is_operational=True, sign="positive"),
    _m("data_traffic", "Data Traffic", "operational",
       ["data traffic", "total data traffic"], value_type=CNT,
       is_operational=True, sign="positive"),
    _m("voice_traffic", "Voice Traffic", "operational",
       ["voice traffic", "total voice traffic"], value_type=CNT,
       is_operational=True, sign="positive"),
    _m("store_count", "Store Count", "operational",
       ["number of stores", "store count", "total stores", "stores"],
       value_type=CNT, is_operational=True, sign="positive"),
    _m("area_operated", "Area Operated", "operational",
       ["area operated", "retail area", "warehousing and retail area"],
       value_type=CNT, is_operational=True, sign="positive"),
    _m("footfalls", "Footfalls", "operational",
       ["footfalls", "customer footfalls"], value_type=CNT,
       is_operational=True, sign="positive"),
    _m("registered_customers", "Registered Customers", "operational",
       ["registered customer base", "registered customers"], value_type=CNT,
       is_operational=True, sign="positive"),
    _m("throughput", "Throughput", "operational",
       ["throughput", "production meant for sale", "crude processed"],
       value_type=CNT, is_operational=True, sign="positive"),
    _m("production", "Production", "operational",
       ["production", "gross production", "output"], value_type=CNT,
       is_operational=True, sign="positive"),
    _m("net_subscriber_addition", "Net Subscriber Addition", "operational",
       ["net subscriber addition", "net adds", "net additions"],
       value_type=CNT, is_operational=True),
)

METRICS_BY_KEY: dict[str, MetricDef] = {m.key: m for m in METRICS}

_ALIAS_INDEX: dict[str, str] = {}
for _m_def in METRICS:
    for _a in (_m_def.display, *_m_def.aliases):
        _ALIAS_INDEX.setdefault(normalize_label(_a), _m_def.key)


@dataclass(slots=True)
class MetricMatch:
    key: str
    definition: MetricDef
    score: float
    matched_alias: str
    method: str  # "exact" | "prefix" | "fuzzy"


def _similarity(a: str, b: str) -> float:
    """Blend sequence similarity with token overlap.

    Token overlap alone rewards long labels that merely share common words;
    sequence similarity alone punishes reordering.  Together they behave well on
    financial labels such as "Profit After Tax and Share of Profit of Associates".
    """
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return seq
    jac = len(ta & tb) / len(ta | tb)
    cover = len(ta & tb) / min(len(ta), len(tb))
    return max(seq, 0.45 * seq + 0.30 * jac + 0.25 * cover)


@lru_cache(maxsize=4096)
def match_metric(label: str, *, threshold: float | None = None,
                 operational_ok: bool = True) -> MetricMatch | None:
    """Map a source label onto a canonical metric, or None if nothing fits.

    Returning None is a valid, meaningful outcome: a row the taxonomy does not
    recognise must not be forced onto the nearest known metric.
    """
    from app.config.settings import settings  # local: avoids import cycle at import time
    thr = settings.metric_match_threshold if threshold is None else threshold

    norm = normalize_label(label)
    if not norm or len(norm) < 2:
        return None

    hit = _ALIAS_INDEX.get(norm)
    if hit:
        d = METRICS_BY_KEY[hit]
        if operational_ok or not d.is_operational:
            return MetricMatch(hit, d, 1.0, norm, "exact")

    # Retry without a trailing qualifier: financial tables append the unit or a
    # clarifier to the row label -- "ARPU (Rs/Sub/month)", "Area (Mn sq ft)".
    bare = _TRAILING_PAREN.sub("", label or "").strip()
    if bare and bare != label:
        norm_bare = normalize_label(bare)
        hit = _ALIAS_INDEX.get(norm_bare)
        if hit:
            d = METRICS_BY_KEY[hit]
            if operational_ok or not d.is_operational:
                return MetricMatch(hit, d, 0.97, norm_bare, "exact")

    stripped = _strip_noise(norm)
    if stripped and stripped != norm:
        hit = _ALIAS_INDEX.get(stripped)
        if hit:
            d = METRICS_BY_KEY[hit]
            if operational_ok or not d.is_operational:
                return MetricMatch(hit, d, 0.95, stripped, "exact")

    best: MetricMatch | None = None
    for d in METRICS:
        if not operational_ok and d.is_operational:
            continue
        for alias in (d.display, *d.aliases):
            na = normalize_label(alias)
            if not na:
                continue
            # A prefix match must account for most of the label.  Without the
            # coverage test, the 6-character alias "income" would claim
            # "income tax relating to items that will not be reclassified..."
            # with a near-perfect score.
            coverage = len(na) / max(len(norm), 1)
            if norm.startswith(na) and len(na) >= 4 and coverage >= 0.55 and (
                    len(norm) == len(na) or not norm[len(na)].isalnum()):
                score = 0.9 + 0.1 * coverage
                method = "prefix"
            else:
                score = _similarity(norm, na)
                method = "fuzzy"
            if best is None or score > best.score:
                best = MetricMatch(d.key, d, round(score, 4), na, method)
    if best and best.score >= thr:
        return best
    return None


def get(key: str) -> MetricDef | None:
    return METRICS_BY_KEY.get(key)


def families() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in METRICS:
        out.setdefault(m.family, []).append(m.key)
    return out


def all_metrics() -> list[dict]:
    return [{"key": m.key, "display": m.display, "family": m.family,
             "value_type": m.value_type.value, "is_flow": m.is_flow,
             "is_operational": m.is_operational,
             "aliases": list(m.aliases)} for m in METRICS]
