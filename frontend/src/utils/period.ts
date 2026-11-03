import type { PeriodBasis } from "../types";

/**
 * How a figure's reporting period is put into words.
 *
 * A financial figure without its period is not information, it is a number. The
 * rules here exist because the alternative — showing "Q3 FY25" and letting the
 * reader assume the quarter ended on the last day of June — is wrong for any
 * filer on a 52/53-week calendar, and wrong silently.
 *
 * Two principles drive every branch below:
 *
 *  1. **The exact date leads.** Where the source established one, it is the
 *     primary line. The fiscal label is secondary, because a label is a
 *     convention and a date is a fact.
 *  2. **Absence is stated, not styled away.** A figure whose period could not
 *     be established says so in words a reader can act on. Nothing here ever
 *     computes a date, a quarter or a fiscal year — it only renders what the
 *     backend resolved.
 */

/**
 * How much the period cell should assert.
 *
 * `none` and `not-applicable` are deliberately separate. "The document offered
 * a period here and it could not be read" is a gap worth a reader's attention.
 * "This column was a business segment, so it never carried a period" is not a
 * gap at all — it is what a segment table looks like. Drawing both in warning
 * colour makes an ordinary table appear broken.
 */
export type PeriodCertainty = "resolved" | "low" | "none" | "not-applicable";

export interface PeriodDisplay {
  /** The line that leads: "Three months ended 28 June 2025", or the honest gap. */
  primary: string;
  /** Fiscal label and provenance: "Q3 FY25 · derived from statement header". */
  secondary: string | null;
  /** Drives emphasis and whether the row reads as uncertain. */
  certainty: PeriodCertainty;
  /** Long-form explanation for a tooltip. */
  title: string;
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];

/** "2025-06-28" -> "28 June 2025". Returns null for anything unparseable. */
export function formatIsoDate(iso?: string | null): string | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso.trim());
  if (!m) return null;
  const [, y, mo, d] = m;
  const month = MONTHS[Number(mo) - 1];
  if (!month) return null;
  return `${Number(d)} ${month} ${y}`;
}

/** The span phrase a period type implies, e.g. "Three months ended". */
function spanPhrase(periodType?: string | null): string | null {
  switch (periodType) {
    case "quarter": return "Three months ended";
    case "half_year": return "Six months ended";
    case "nine_month": return "Nine months ended";
    case "fiscal_year": return "Twelve months ended";
    case "calendar_year": return "Calendar year ended";
    case "ttm": return "Twelve months to";
    case "point_in_time": return "As at";
    default: return null;
  }
}

/**
 * Plain wording for how the period was established.
 *
 * `inherited` deliberately does not name a table. A period can be inherited
 * from a table's column structure, from a slide's own heading, or from a date
 * set above a block of metrics, and telling a reader looking at a KPI tile
 * that its quarter came from "the table structure" is simply false. Where the
 * backend recorded *which* piece of the page lent it, `inheritedFrom` carries
 * that text and it replaces the generic phrase entirely.
 */
export function basisPhrase(basis?: PeriodBasis | null,
                            inheritedFrom?: string | null): string | null {
  switch (basis) {
    case "stated": return "stated in the document";
    case "derived": return "derived from the statement header";
    case "inherited":
      return inheritedFrom
        ? `inherited from ${inheritedFrom}`
        : "inherited from the surrounding structure";
    default: return null;
  }
}

/**
 * Confidence as words, never a decimal.
 *
 * Thresholds follow the resolver's own scale: it emits 0.9+ where the document
 * said the period outright, around 0.75–0.85 where it was assembled from a
 * header, and 0.6–0.7 where a date carried no span of its own.
 */
export function periodConfidenceLabel(c?: number | null): string | null {
  if (c === null || c === undefined) return null;
  if (c >= 0.85) return "High confidence";
  if (c >= 0.7) return "Medium confidence";
  return "Low confidence";
}

const LOW_CONFIDENCE = 0.7;

/**
 * Turn what the backend stored into what the reader sees.
 *
 * Deliberately takes only the fields the API serves, so it cannot reach for
 * anything the resolver did not establish.
 */
export interface PeriodFields {
  period?: string | null;
  period_type?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  period_confidence?: number | null;
  period_basis?: PeriodBasis | null;
  /** Carries `period_inherited_from`, where the backend recorded one. */
  meta?: Record<string, unknown> | null;
}

/** The exact page evidence an inherited period came from, if it was recorded. */
function inheritedFrom(f: PeriodFields): string | null {
  const from = f.meta?.["period_inherited_from"];
  return typeof from === "string" && from.trim() ? from.trim() : null;
}

/** Shapes of table whose columns are reporting periods. */
const PERIODIZED = new Set(["period_flow", "point_in_time", "periodized_kpi"]);

/**
 * Whether this figure came from a table organised by reporting period.
 *
 * The distinction decides which of two very different sentences a reader sees,
 * and they must never be swapped. A segment column genuinely has no period, so
 * "Not period-specific" is the truth and a warning would be noise. A column of
 * a statement of profit and loss always has one, so the same words would be a
 * false statement about the document.
 *
 * This is checked here as well as in the backend because getting it wrong is
 * exactly the defect this phase existed to fix: four dated columns of one
 * statement were all reported as "Not period-specific" at high confidence.
 * The backend now sends the right basis; this makes the wrong sentence
 * unreachable from the UI even if it stops doing so.
 */
function fromPeriodizedTable(f: PeriodFields): boolean {
  const shape = f.meta?.["table_shape"];
  return typeof shape === "string" && PERIODIZED.has(shape);
}

/** The safety gate's own words for what is missing, when it recorded any. */
function gateMessage(f: PeriodFields): string | null {
  const message = f.meta?.["safety_gate_message"];
  return typeof message === "string" && message.trim() ? message.trim() : null;
}

export function describePeriod(f: PeriodFields): PeriodDisplay {
  const end = formatIsoDate(f.period_end);
  const label = f.period ?? null;
  const basis = basisPhrase(f.period_basis, inheritedFrom(f));
  const conf = f.period_confidence ?? null;
  const low = conf !== null && conf < LOW_CONFIDENCE;

  // --- the column never carried a period ---------------------------------
  // Only sayable when the table is not organised by period. A statement whose
  // columns are dates always has one for every figure, so on that table the
  // sentence below would be untrue of the document, not merely unhelpful.
  if (!label && !end && f.period_basis === "not_applicable"
      && !fromPeriodizedTable(f)) {
    return {
      primary: "Not period-specific",
      secondary: null,
      certainty: "not-applicable",
      title: "This figure comes from a column that sets out a business "
        + "segment, a geography or a unit rather than a reporting period, so "
        + "no period applies to it on its own.",
    };
  }

  // --- a period was offered here and could not be read --------------------
  if (!label && !end) {
    const periodized = fromPeriodizedTable(f);
    return {
      primary: "Period not identified",
      secondary: periodized ? "needs review" : null,
      certainty: "none",
      title: gateMessage(f)
        ?? (periodized
          ? "This figure comes from a statement whose columns are reporting "
            + "periods, but its own column could not be matched to one. The "
            + "period has been left blank rather than guessed."
          : "The column heading did not provide enough evidence to establish "
            + "which period this figure belongs to. It has not been guessed."),
    };
  }

  // --- an exact date exists: it leads ------------------------------------
  if (end) {
    const phrase = spanPhrase(f.period_type);
    const instant = f.period_type === "point_in_time";
    const primary = phrase ? `${phrase} ${end}` : end;

    // The label repeats the date for instants ("As at 2025-06-28"), so it is
    // only worth showing when it says something the date does not.
    const labelIsJustTheDate = !label
      || (f.period_end !== null && f.period_end !== undefined
          && label.includes(f.period_end));
    const bits = [
      labelIsJustTheDate ? null : label,
      basis,
    ].filter(Boolean);

    return {
      primary,
      secondary: bits.length ? bits.join(" · ") : null,
      certainty: low ? "low" : "resolved",
      title: [
        primary,
        labelIsJustTheDate ? null : `Fiscal label: ${label}`,
        basis ? `Period ${basis}.` : null,
        instant ? "A balance at a point in time, not a period total." : null,
        low ? "The period was resolved with low confidence and may need review."
            : null,
      ].filter(Boolean).join(" "),
    };
  }

  // --- a label but no date: say exactly that ------------------------------
  return {
    primary: label as string,
    secondary: [basis, "exact dates not established"].filter(Boolean).join(" · "),
    certainty: low ? "low" : "resolved",
    title: `${label}. The document did not establish exact start and end `
      + "dates for this period, so none are shown.",
  };
}

/** A single compact string, for tight places like a comparison column head. */
export function periodOneLine(f: Parameters<typeof describePeriod>[0]): string {
  const d = describePeriod(f);
  return d.secondary && d.certainty !== "none"
    ? `${d.primary} (${d.secondary.split(" · ")[0]})`
    : d.primary;
}


/**
 * How a figure was found, in the words a reader needs.
 *
 * The pipeline can reach the same kind of number by different routes: a
 * reconstructed statement table, a KPI tile on a slide, a sentence. Which
 * route was taken changes how much a reader should lean on it, so it is worth
 * one short phrase rather than a badge nobody can decode.
 */
export type ExtractionStrategy =
  | "table_cell" | "spatial_kpi" | "spatial_row" | "spatial_grid"
  | "narrative" | "narrative_sentence" | "chart" | "derived";

const STRATEGY_TEXT: Record<string, { label: string; title: string }> = {
  table_cell: {
    label: "Statement table",
    title: "Read from a reconstructed table, at the intersection of a "
      + "recognised row label and a period column.",
  },
  spatial_kpi: {
    label: "KPI tile",
    title: "Read from a highlight tile on the page: a label with its figure "
      + "beneath it, in a layout repeated across the page.",
  },
  spatial_grid: {
    label: "Drawn table",
    title: "Read from a grid of figures the document laid out without ruling "
      + "lines: a row label, a column head, and the figure where they meet.",
  },
  spatial_row: {
    label: "Metric block",
    title: "Read from a panel of metrics set out one per line, with the "
      + "figure beside its label rather than beneath it.",
  },
  narrative: {
    label: "Narrative text",
    title: "Read from a sentence in the document's own prose.",
  },
  // The backend records the strategy as `narrative_sentence`; the fact's
  // `kind` is `narrative`. Both reach this map, from different callers.
  narrative_sentence: {
    label: "Narrative text",
    title: "Read from a sentence in the document's own prose.",
  },
  chart: {
    label: "Chart label",
    title: "Read from a value printed on a chart.",
  },
  derived: {
    label: "Calculated",
    title: "Not printed in the document. FinSight computed it from figures "
      + "that were.",
  },
};

/** The strategy a fact records, or null when it predates the record. */
export function describeStrategy(fact: {
  kind?: string | null;
  meta?: Record<string, unknown> | null;
}): { label: string; title: string } | null {
  const declared = fact.meta?.["extraction_strategy"];
  const key = typeof declared === "string" ? declared : (fact.kind ?? "");
  return STRATEGY_TEXT[key] ?? null;
}
