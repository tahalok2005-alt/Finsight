import type { FinancialFact } from "../types";
import { describeStrategy } from "./period";

/**
 * How a figure stood up against the rest of the document, in a reader's words.
 *
 * The backend compares every figure with every other reading of the same
 * measure, entity and period. Three outcomes matter to somebody deciding
 * whether to quote a number, and they are not degrees of the same thing:
 *
 *  - **corroborated** — another strategy read the same value from different
 *    pixels. That is the strongest thing this system can say about a figure.
 *  - **disputed** — something else in the document reports a different value
 *    for the same slot. Not a lower confidence: a different kind of statement,
 *    and one a person has to settle.
 *  - **single source** — read once. The ordinary case, and not a criticism.
 *
 * Everything here reads what the backend recorded. Nothing is inferred from
 * the value, and no verdict is invented where the backend gave none.
 */

export interface SourceRecord {
  page: number | null;
  strategy: string | null;
  strategyLabel: string;
  printedLabel: string | null;
  value: number | null;
  unit?: string | null;
  currency?: string | null;
}

export type EvidenceState = "corroborated" | "disputed" | "single";

export interface EvidenceSummary {
  state: EvidenceState;
  /** Independent readings that agreed, excluding the surviving fact itself. */
  corroborating: SourceRecord[];
  /** Readings that disagree. Always retained; never silently dropped. */
  conflicting: SourceRecord[];
  /** One line for a table cell. Null when there is nothing worth saying. */
  summary: string | null;
}

function strategyLabel(strategy: string | null): string {
  if (!strategy) return "Another source";
  const described = describeStrategy({ meta: { extraction_strategy: strategy } });
  return described ? described.label : "Another source";
}

function toRecord(raw: unknown): SourceRecord | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const strategy = typeof r.strategy === "string" ? r.strategy : null;
  return {
    page: typeof r.page === "number" ? r.page : null,
    strategy,
    strategyLabel: strategyLabel(strategy),
    printedLabel:
      typeof r.printed_label === "string" ? r.printed_label : null,
    value: typeof r.value === "number" ? r.value : null,
    unit: typeof r.unit === "string" ? r.unit : null,
    currency: typeof r.currency === "string" ? r.currency : null,
  };
}

function list(meta: Record<string, unknown> | null | undefined,
              key: string): SourceRecord[] {
  const raw = meta?.[key];
  if (!Array.isArray(raw)) return [];
  return raw.map(toRecord).filter((r): r is SourceRecord => r !== null);
}

export function describeEvidence(fact: FinancialFact): EvidenceSummary {
  const meta = fact.meta ?? null;
  const conflicting = list(meta, "conflicts");
  const corroborating = list(meta, "corroboration");

  if (conflicting.length) {
    // A dispute outranks agreement. A figure two sources confirm and a third
    // contradicts still needs somebody to decide which is right.
    const where = conflicting
      .map((c) => (c.page !== null ? `${c.strategyLabel} · p${c.page}` : c.strategyLabel))
      .join(", ");
    return {
      state: "disputed",
      corroborating,
      conflicting,
      summary: `Another source in this document reports a different value (${where})`,
    };
  }
  if (corroborating.length) {
    const sources = Array.from(
      new Set(corroborating.map((c) => c.strategyLabel)),
    );
    return {
      state: "corroborated",
      corroborating,
      conflicting,
      summary:
        sources.length === 1
          ? `Also read from ${sources[0].toLowerCase()}`
          : `Also read from ${sources.length} other sources`,
    };
  }
  return { state: "single", corroborating, conflicting, summary: null };
}

/** How a figure's value should read, with its scale and currency. */
export function formatSourceValue(record: SourceRecord): string {
  if (record.value === null) return "—";
  const n = record.value.toLocaleString(undefined, {
    maximumFractionDigits: 2,
  });
  const bits = [n];
  if (record.unit && record.unit !== "none") bits.push(record.unit);
  if (record.currency && record.currency !== "UNKNOWN") {
    return `${record.currency} ${bits.join(" ")}`;
  }
  return bits.join(" ");
}
