"""Deciding what, if anything, a detected chart can tell us.

The question this answers is not "what does this chart say" but "is there any
prospect of reading it safely". Getting that order right matters: the previous
phase's worst defect came from treating a chart's title, its axis tick and one
of its bars as though they were a labelled figure.

Why association and not containment
-----------------------------------
The chart region comes from a graphic blob, and a blob covers the *ink of the
bars*. Printed data labels sit above the bars, outside it. Measured on one page
of a bar chart with seven printed percentages, the containment test used by the
chart reader kept **none** of them -- the best label scored 0.58 against a
threshold of 0.6, and four scored zero. Across a whole 75-page deck it captured
38 labels in total, and the ones it did capture were the accidental ones:
``343397434477.0`` from a page whose real figures are in the thousands.

So the evidence for a chart is gathered from a band *around* it -- the plot
area plus the margin where a designer puts the labels and the axis -- and the
triage then says what was found. Association is not ownership: text near a
chart may belong to something else, which is why nothing here produces a fact.

What the triage is for
----------------------
Only a chart with a printed value, a period to attach it to and a metric to
call it can ever become a financial fact. This module measures how many charts
clear that bar, and it is expected to be honest when the answer is very few.
Nothing here estimates a value from a bar's height, a line's position or any
other pixel geometry.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from app.core.geometry import BBox
from app.models.enums import ChartType
from app.extraction.taxonomy import match_metric
from app.models.schemas import ExtractedChart, OCRElement
from app.normalization.numbers import normalize_number
from app.normalization.periods import looks_like_period_header

#: How far beyond a chart's own box its labels may sit, as a share of the
#: box's size. A designer puts a data label just clear of the bar it belongs
#: to and the axis just clear of the plot; neither is far.
_MARGIN_RATIO = 0.18
#: Below this many printed figures a chart has no values to read, whatever
#: else it has. Two is a comparison; one is a callout.
_MIN_PRINTED_VALUES = 3
#: A chart needs at least this many period labels to place its values in time.
_MIN_PERIOD_LABELS = 2
#: Axis tick values run in a sequence a person would choose: 0, 20, 40 ...
_AXIS_HINT = re.compile(r"^\s*[\d,]+(\.\d+)?\s*%?\s*$")


@dataclass(slots=True)
class ChartEvidence:
    """What was found in and around one chart region."""
    chart_id: str
    page_number: int
    chart_type: ChartType
    type_confidence: float
    #: Figures printed on the chart, in reading order.
    printed_values: list[str] = field(default_factory=list)
    #: Figures that form a vertical run at one x position: a value axis.
    axis_ticks: list[str] = field(default_factory=list)
    #: Text that reads as a reporting period.
    period_labels: list[str] = field(default_factory=list)
    #: Text that could name what is being measured.
    metric_labels: list[str] = field(default_factory=list)
    #: Legend entries -- series names, never metrics in their own right.
    legend_labels: list[str] = field(default_factory=list)
    area_ratio: float = 0.0
    elements_nearby: int = 0

    @property
    def data_labels(self) -> list[str]:
        """Printed figures that are not part of the value axis.

        An axis tick and a data label look identical as text. What separates
        them is arrangement: ticks line up vertically at one x position because
        they label a scale, and data labels sit over the marks they belong to.
        """
        ticks = set(self.axis_ticks)
        return [v for v in self.printed_values if v not in ticks]

    @property
    def has_data_labels(self) -> bool:
        return len(self.data_labels) >= _MIN_PRINTED_VALUES

    @property
    def has_period_labels(self) -> bool:
        return len(self.period_labels) >= _MIN_PERIOD_LABELS

    @property
    def has_metric_labels(self) -> bool:
        return bool(self.metric_labels)

    @property
    def has_numeric_axis(self) -> bool:
        """Whether a value axis was printed, as opposed to data labels.

        An axis is a scale a reader interpolates against. It is *not* a source
        of values: reading a bar against it means measuring pixels, which this
        system does not do. It is recorded because a chart that has one and no
        data labels is precisely the chart that cannot be read.
        """
        return len(self.axis_ticks) >= 3

    @property
    def likely_financial(self) -> bool:
        """Whether this looks like a chart of financial data at all.

        A decorative graphic, a photograph and a logo all reach the chart
        detector. What separates a financial chart from them is that somebody
        printed numbers and words on it.
        """
        return (self.elements_nearby >= 4
                and bool(self.printed_values)
                and (self.has_period_labels or self.has_metric_labels))

    @property
    def extraction_possible(self) -> bool:
        """Whether a value could be turned into a fact without guessing.

        All three are required, and none of them is negotiable:

        * **printed values** -- otherwise the only way to a number is the
          geometry, and that is estimation;
        * **period labels** -- a financial figure without its period is not
          information;
        * **a metric label** -- a number with no name is not a fact either.

        And even with all three, the values and the periods must correspond
        one for one. Anything else is an association problem, and guessing at
        it is how a figure from one year comes to be reported under another's.
        """
        if not (self.likely_financial and self.has_data_labels
                and self.has_period_labels and self.has_metric_labels):
            return False
        # One value per period, or there is no way to say which is which.
        #
        # A chart plotting seventeen figures against fourteen period labels
        # has an association problem this phase does not solve: matching a bar
        # to a tick means reasoning about x-position, and getting it wrong
        # produces exactly the defect the previous phase removed -- a figure
        # from one year reported under another's label.
        return len(self.data_labels) == len(self.period_labels)

    def as_dict(self) -> dict:
        return {
            "chart_id": self.chart_id, "page": self.page_number,
            "chart_type": str(self.chart_type),
            "type_confidence": round(self.type_confidence, 3),
            "printed_values": len(self.printed_values),
            "axis_ticks": len(self.axis_ticks),
            "data_labels": len(self.data_labels),
            "period_labels": len(self.period_labels),
            "metric_labels": len(self.metric_labels),
            "legend_labels": len(self.legend_labels),
            "elements_nearby": self.elements_nearby,
            "has_data_labels": self.has_data_labels,
            "has_period_labels": self.has_period_labels,
            "has_metric_labels": self.has_metric_labels,
            "has_numeric_axis": self.has_numeric_axis,
            "likely_financial": self.likely_financial,
            "extraction_possible": self.extraction_possible,
        }


def _reads_as_value(text: str) -> bool:
    t = (text or "").strip()
    if not t or not any(c.isdigit() for c in t):
        return False
    if looks_like_period_header(t):
        return False
    letters = sum(1 for c in t if c.isalpha())
    if letters / len(t) > 0.4:
        return False
    parsed = normalize_number(t)
    return parsed is not None and parsed.value is not None


def _reads_as_metric_label(text: str) -> bool:
    """Whether this names a measure the system actually knows.

    An earlier draft asked only whether the text was wordy, and every chart in
    the deck passed: "In US$ Mn." is a unit declaration, "#Excluding Money
    Market Borrowings" is a footnote, "IDFC FIRST" is the company. Seventeen
    charts looked extractable and not one had a metric name among them.

    Requiring a taxonomy match is the same standard the tile and row readers
    are held to, and it is the standard that matters: without a recognised
    metric there is no fact to make, only a number near some words.
    """
    t = (text or "").strip()
    if len(t) < 3 or looks_like_period_header(t):
        return False
    if any(c.isdigit() for c in t):
        return False
    letters = sum(1 for c in t if c.isalpha())
    if letters / max(1, len(t)) < 0.6 or len(t.split()) > 8:
        return False
    return match_metric(t) is not None


def gather(chart: ExtractedChart, elements: list[OCRElement], *,
           page_width: int, page_height: int) -> ChartEvidence:
    """Collect the evidence in and around one chart, without judging it."""
    box = chart.bbox.geom
    margin = max(box.width, box.height) * _MARGIN_RATIO
    probe = box.padded(margin).clipped(page_width or 1, page_height or 1)

    evidence = ChartEvidence(
        chart_id=chart.id, page_number=chart.page_number,
        chart_type=chart.chart_type, type_confidence=chart.type_confidence,
        area_ratio=round(box.area / max(1.0, float(page_width * page_height)), 4))

    numeric_boxes: list[tuple[str, BBox]] = []
    for element in elements:
        text = (element.text or "").strip()
        if not text:
            continue
        b = BBox.from_dict(element.bbox.model_dump())
        if probe.contains_ratio(b) <= 0.5:
            continue
        evidence.elements_nearby += 1
        if looks_like_period_header(text):
            evidence.period_labels.append(text)
        elif _reads_as_value(text):
            evidence.printed_values.append(text)
            numeric_boxes.append((text, b))
        elif _reads_as_metric_label(text):
            evidence.metric_labels.append(text)

    evidence.axis_ticks = _axis_ticks(numeric_boxes)

    # The chart's own captured legend is kept separate: a series name says
    # which line is which, never what the figure is.
    evidence.legend_labels = list(chart.legend_labels)
    return evidence


def _axis_ticks(numeric: list[tuple[str, BBox]]) -> list[str]:
    """Figures that line up vertically at one x: the value axis.

    Three or more numbers sharing a left edge and stacked down the plot are a
    scale, not readings. They are recorded so the triage can say a chart has an
    axis and no data labels -- which is exactly the chart that cannot be read
    without measuring pixels.
    """
    if len(numeric) < 3:
        return []
    columns: dict[int, list[str]] = {}
    widths = [b.width for _, b in numeric] or [1.0]
    tolerance = max(4.0, statistics.median(widths) * 0.6)
    for text, b in numeric:
        key = int(round(b.x / max(tolerance, 1.0)))
        columns.setdefault(key, []).append(text)
    best = max(columns.values(), key=len)
    return best if len(best) >= 3 else []


def triage(charts: list[ExtractedChart], elements_by_page: dict[int, list],
           dims_by_page: dict[int, tuple[int, int]]) -> list[ChartEvidence]:
    """Evidence for every chart in a document."""
    out: list[ChartEvidence] = []
    for chart in charts:
        width, height = dims_by_page.get(chart.page_number, (0, 0))
        out.append(gather(chart, elements_by_page.get(chart.page_number, []),
                          page_width=width, page_height=height))
    return out


def summarise(evidence: list[ChartEvidence]) -> dict:
    """Document-level counts, for the diagnostics and the report."""
    return {
        "charts": len(evidence),
        "by_type": _counted(str(e.chart_type) for e in evidence),
        "likely_financial": sum(1 for e in evidence if e.likely_financial),
        "with_data_labels": sum(1 for e in evidence if e.has_data_labels),
        "with_period_labels": sum(1 for e in evidence if e.has_period_labels),
        "with_metric_labels": sum(1 for e in evidence if e.has_metric_labels),
        "with_numeric_axis_only": sum(1 for e in evidence
                                      if e.has_numeric_axis),
        "extraction_possible": sum(1 for e in evidence if e.extraction_possible),
    }


def _counted(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
