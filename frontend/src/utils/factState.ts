/**
 * The state of a financial fact, as the backend's evidence model decided it.
 *
 * A single confidence percentage answers "how clearly was this printed?", which
 * is rarely the question. The evidence model scores metric, value, period,
 * entity and unit separately and lets the weakest decide, so a figure read
 * perfectly from a clean page but never matched to a column reads as UNKNOWN
 * rather than as 99%.
 *
 * This module only renders what the backend recorded. It never derives a state
 * of its own -- a UI that computed its own answer could disagree with the
 * export, the API and the review queue, and a reader would have no way to tell
 * which was right.
 */

export type FactStateName =
  | "VALIDATED"
  | "HIGH_CONFIDENCE"
  | "MEDIUM_CONFIDENCE"
  | "REQUIRES_REVIEW"
  | "UNKNOWN";

export interface FactStateDisplay {
  /** What the chip says. */
  label: string;
  /** Chip classes from index.css. */
  chip: string;
  /** One sentence explaining what this state means, for the tooltip. */
  title: string;
  /** True when a person needs to look at this figure. */
  needsAttention: boolean;
}

const DISPLAY: Record<FactStateName, FactStateDisplay> = {
  VALIDATED: {
    label: "Validated",
    chip: "chip-pass",
    title: "Every part of this figure is established from the document, and an "
      + "accounting check computed from other figures agrees with it.",
    needsAttention: false,
  },
  HIGH_CONFIDENCE: {
    label: "High",
    chip: "chip-pass",
    title: "The metric, value, period and its span, entity, scale, currency and "
      + "the table position are all established from the document. No accounting "
      + "check applied to it.",
    needsAttention: false,
  },
  MEDIUM_CONFIDENCE: {
    label: "Medium",
    chip: "chip-warn",
    title: "Everything about this figure is established, but at least one part "
      + "was inherited or inferred rather than stated outright.",
    needsAttention: false,
  },
  REQUIRES_REVIEW: {
    label: "Needs review",
    chip: "chip-warn",
    title: "Something about this figure needs a person to confirm it.",
    needsAttention: true,
  },
  UNKNOWN: {
    label: "Unresolved",
    chip: "chip-fail",
    title: "Part of this figure could not be established from the document at "
      + "all, so it is shown without it rather than with a guess.",
    needsAttention: true,
  },
};

/** The dimension names, in the order a reader needs them. */
export const EVIDENCE_ORDER = [
  "metric", "value", "period", "duration", "entity", "unit", "currency",
  "structure",
] as const;

export const EVIDENCE_LABEL: Record<string, string> = {
  metric: "Metric",
  value: "Value",
  period: "Period end",
  duration: "Duration",
  entity: "Entity",
  unit: "Scale",
  currency: "Currency",
  structure: "Table position",
  accounting: "Accounting check",
  corroboration: "Corroboration",
};

/** What a weak reading of each dimension means, for the detail panel. */
export const LIMITING_EXPLANATION: Record<string, string> = {
  metric: "the row label was matched to a metric by similarity, not word for word",
  value: "the digits were read with low confidence, or a second read disagreed",
  period: "the period end was reconstructed or could not be read from the column",
  duration: "the span (quarter, year, balance) was not established",
  entity: "the reporting entity or basis was not stated for this figure",
  unit: "the scale (crore, million) was not stated",
  currency: "the currency was not printed, or was inferred from magnitude words",
  structure: "the table's reconstruction could not vouch for the figure's column",
};

export interface EvidenceReading {
  levels: { name: string; label: string; level: string }[];
  /** The dimension that held the fact back, if any. */
  limiting: string | null;
  /** Dimensions that do not apply to this fact. */
  notApplicable: string[];
}

interface FactLike {
  meta?: Record<string, unknown> | null;
  confidence_band?: string | null;
}

/**
 * The recorded state, or null when the backend did not record one.
 *
 * Returning null rather than falling back to the confidence band is
 * deliberate: an older fact that predates the evidence model should keep
 * showing the score it was given, not be relabelled by a rule that never ran
 * on it.
 */
export function factState(fact: FactLike): FactStateDisplay | null {
  const raw = fact.meta?.["fact_state"];
  if (typeof raw !== "string") return null;
  return DISPLAY[raw as FactStateName] ?? null;
}

/** The per-dimension readings behind that state. */
export function evidenceReading(fact: FactLike): EvidenceReading | null {
  const raw = fact.meta?.["evidence"];
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const notApplicable = Array.isArray(record["not_applicable"])
    ? (record["not_applicable"] as string[])
    : [];
  const levels = [...EVIDENCE_ORDER, "accounting", "corroboration"]
    .filter((name) => typeof record[name] === "string"
      && !notApplicable.includes(name))
    .map((name) => ({
      name,
      label: EVIDENCE_LABEL[name] ?? name,
      level: String(record[name]),
    }));
  const limiting = typeof record["limiting"] === "string"
    ? (record["limiting"] as string) : null;
  return { levels, limiting, notApplicable };
}
