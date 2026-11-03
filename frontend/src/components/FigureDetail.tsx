import { useState } from "react";
import { AlertTriangle, Check, ChevronDown, ChevronRight, X } from "lucide-react";
import type { FinancialFact } from "../types";
import { formatFull } from "../utils/format";
import { describePeriod, describeStrategy, periodConfidenceLabel } from "../utils/period";
import { describeEvidence, formatSourceValue } from "../utils/evidence";
import {
  EVIDENCE_LABEL, LIMITING_EXPLANATION, evidenceReading, factState,
} from "../utils/factState";
import type { SourceRecord } from "../utils/evidence";

/**
 * Everything known about one figure, in the order a reader needs it.
 *
 * The ordering is the design. What the document printed comes first, because
 * that is what a reader can check; how FinSight read it comes second; the
 * machinery that produced it comes last and folded away. A panel that opens on
 * bounding boxes and extraction strategies is a debugger, not an analysis tool.
 *
 * Nothing here computes. Every line renders what the backend established, and
 * where it established nothing the panel says so in words rather than showing
 * a dash and leaving the reader to guess whether that means zero.
 */

function Row({ label, children, mono }: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-3 py-1.5 border-b border-ink-800/60 last:border-0">
      <div className="w-40 shrink-0 text-2xs uppercase tracking-wide text-ink-500 pt-0.5">
        {label}
      </div>
      <div className={`text-xs text-ink-200 min-w-0 ${mono ? "num" : ""}`}>
        {children}
      </div>
    </div>
  );
}

function Unknown({ children }: { children: React.ReactNode }) {
  return <span className="text-ink-500 italic">{children}</span>;
}

/** A dimension the figure did not print itself, carried from elsewhere. */
function Inferred({ children }: { children: React.ReactNode }) {
  return (
    <span className="ml-2 text-2xs uppercase tracking-wide text-amber-400/80">
      inferred — {children}
    </span>
  );
}

/**
 * Where in the document this figure sits: table, row, column, and the heading
 * the period came from.
 *
 * This is the chain that answers "why does FinSight believe 260.40 is the
 * quarter ended 30 June 2026?" -- the answer is that it stood in the column
 * headed "30 June 2026 (Unaudited)", and a reader can check that against the
 * page. Without it the panel shows a resolved period and asks to be believed.
 */
function sourceChain(fact: FinancialFact) {
  const meta = (fact.meta ?? {}) as Record<string, unknown>;
  const asText = (key: string): string | null => {
    const value = meta[key];
    return typeof value === "string" && value.trim() ? value.trim() : null;
  };
  const asNumber = (key: string): number | null => {
    const value = meta[key];
    return typeof value === "number" ? value : null;
  };
  const row = asNumber("row");
  const col = asNumber("col");
  const cellRef = row !== null && col !== null
    ? `row ${row + 1}, column ${col + 1}` : null;
  const tableLabel = [
    fact.provenance?.table_id ? "statement table" : null,
    cellRef,
    fact.source_page ? `page ${fact.source_page}` : null,
  ].filter(Boolean).join(" · ");
  return {
    columnHeader: asText("column_header"),
    tableLabel: tableLabel || null,
    shape: asText("table_shape"),
    gateMessage: asText("safety_gate_message"),
  };
}

function SourceLine({ record }: { record: SourceRecord }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="num text-ink-200">{formatSourceValue(record)}</span>
      <span className="text-2xs text-ink-500">
        {record.strategyLabel}
        {record.page !== null ? ` · p${record.page}` : ""}
        {record.printedLabel ? ` · “${record.printedLabel}”` : ""}
      </span>
    </div>
  );
}

export function FigureDetail({ fact, onClose, onOpenSource }: {
  fact: FinancialFact;
  onClose: () => void;
  onOpenSource?: (page: number) => void;
}) {
  const [showProvenance, setShowProvenance] = useState(false);
  const period = describePeriod(fact);
  const strategy = describeStrategy(fact);
  const evidence = describeEvidence(fact);
  const source = sourceChain(fact);
  const state = factState(fact);
  const evidenceLevels = evidenceReading(fact);
  const value = fact.corrected_value ?? fact.value;

  return (
    <div className="flex flex-col h-full bg-ink-900 border-l border-ink-800">
      <div className="flex items-start justify-between gap-3 px-4 py-3 border-b border-ink-800">
        <div className="min-w-0">
          <div className="text-sm font-medium text-ink-100 truncate">
            {fact.metric_display || fact.canonical_metric || fact.metric}
          </div>
          <div className="text-2xs text-ink-500 truncate">
            as printed: “{fact.metric}”
          </div>
        </div>
        <button className="btn-ghost btn-sm" onClick={onClose} title="Close">
          <X size={14} />
        </button>
      </div>

      <div className="px-4 py-3 border-b border-ink-800">
        <div className="text-xl num text-ink-100">
          {value === null ? (
            <Unknown>Value could not be read</Unknown>
          ) : (
            formatFull(fact.effective_value, fact.currency, fact.unit, fact.value_type)
          )}
        </div>
        <div className="text-xs text-ink-400 mt-0.5">{period.primary}</div>
        {period.secondary ? (
          <div className="text-2xs text-ink-500 mt-0.5">{period.secondary}</div>
        ) : null}
      </div>

      {/* The evidence layer's verdict leads, because it changes what a reader
          should do next more than any other line in the panel. */}
      {evidence.state === "disputed" ? (
        <div className="mx-4 my-3 rounded border border-amber-700/50 bg-amber-950/30 px-3 py-2">
          <div className="flex items-center gap-1.5 text-2xs uppercase tracking-wide text-amber-400">
            <AlertTriangle size={11} /> Requires review — sources disagree
          </div>
          <div className="mt-1.5 space-y-1">
            {evidence.conflicting.map((c, i) => (
              <SourceLine key={i} record={c} />
            ))}
          </div>
          <div className="mt-1.5 text-2xs text-ink-400">
            Both readings are kept. Neither has been chosen for you.
          </div>
        </div>
      ) : evidence.state === "corroborated" ? (
        <div className="mx-4 my-3 rounded border border-emerald-800/50 bg-emerald-950/20 px-3 py-2">
          <div className="flex items-center gap-1.5 text-2xs uppercase tracking-wide text-emerald-400">
            <Check size={11} /> Corroborated
          </div>
          <div className="mt-1.5 space-y-1">
            {evidence.corroborating.map((c, i) => (
              <SourceLine key={i} record={c} />
            ))}
          </div>
        </div>
      ) : null}

      <div className="px-4 pb-4 overflow-auto">
        {/* ---- what the figure is ---------------------------------------- */}
        <Row label="Metric">{fact.metric || <Unknown>no printed label</Unknown>}</Row>
        <Row label="Canonical metric" mono>
          {fact.canonical_metric || <Unknown>unmatched</Unknown>}
          {fact.metric_display && fact.metric_display !== fact.metric ? (
            <div className="text-2xs text-ink-500 mt-0.5">{fact.metric_display}</div>
          ) : null}
        </Row>
        <Row label="Value">
          {value === null ? <Unknown>not readable</Unknown>
            : formatFull(fact.effective_value, fact.currency, fact.unit, fact.value_type)}
          <div className="text-2xs text-ink-500 mt-0.5 num">
            as printed: {fact.raw_value || "—"}
          </div>
        </Row>
        <Row label="Currency">
          {fact.currency && fact.currency !== "UNKNOWN"
            ? <>{fact.currency}{fact.currency_inferred
                ? <Inferred>not printed with the figure</Inferred> : null}</>
            : <Unknown>not stated</Unknown>}
        </Row>
        <Row label="Unit">
          {fact.unit && fact.unit !== "none"
            ? <>{fact.unit}{fact.unit_inferred
                ? <Inferred>from the table or document heading</Inferred> : null}</>
            : <Unknown>no scale stated</Unknown>}
        </Row>

        {/* ---- when and whose ------------------------------------------- */}
        <Row label="Period">{period.primary}</Row>
        <Row label="Period end">
          {fact.period_end ?? <Unknown>not established</Unknown>}
        </Row>
        <Row label="Period duration">
          {fact.period_type && fact.period_type !== "unknown"
            ? fact.period_type.replace(/_/g, " ")
            : <Unknown>not established</Unknown>}
        </Row>
        <Row label="Period basis">
          {fact.period_basis
            ? fact.period_basis.replace(/_/g, " ")
            : <Unknown>unknown</Unknown>}
        </Row>
        <Row label="Entity">
          {fact.entity ?? <Unknown>not attributed</Unknown>}
        </Row>
        <Row label="Reporting basis">
          {fact.entity_type && !["unknown", "company"].includes(fact.entity_type)
            ? fact.entity_type.replace(/_/g, " ")
            : <Unknown>not stated for this figure</Unknown>}
          <span className="text-ink-500"> · {fact.nature}</span>
        </Row>
        <Row label="Qualification">
          {fact.qualification ? (
            <>
              <span className="text-ink-100">{fact.qualification.qualification_label}</span>
              <div className="text-2xs text-ink-500 mt-0.5">
                {fact.qualification.qualification_type.replace(/_/g, " ")} · from the{" "}
                {fact.qualification.qualification_source.replace(/_/g, " ")}
                {fact.qualification.footnote_reference
                  ? ` (${fact.qualification.footnote_reference})` : ""}
              </div>
            </>
          ) : <span className="text-ink-400">none stated — the plain measure</span>}
        </Row>

        {/* ---- how far to trust it -------------------------------------- */}
        <Row label="Status">
          {state ? (
            <span className={state.chip} title={state.title}>{state.label}</span>
          ) : (
            <span className="text-ink-300">{fact.confidence_band}</span>
          )}
          {state ? (
            <div className="text-2xs text-ink-500 mt-1">{state.title}</div>
          ) : null}
        </Row>
        <Row label="Limiting dimension">
          {evidenceLevels?.limiting ? (
            <>
              <span className="text-warn-400">
                {EVIDENCE_LABEL[evidenceLevels.limiting] ?? evidenceLevels.limiting}
              </span>
              <div className="text-2xs text-ink-500 mt-0.5">
                {LIMITING_EXPLANATION[evidenceLevels.limiting] ?? ""}
              </div>
            </>
          ) : <span className="text-ink-400">none — every dimension is established</span>}
        </Row>
        {/* What is established, dimension by dimension. A single percentage
            cannot say that the digits were read perfectly and the period was
            never found; these can, and that is usually the thing a reader
            needs to know. */}
        {evidenceLevels && evidenceLevels.levels.length ? (
          <Row label="Evidence">
            <div className="flex flex-wrap gap-x-3 gap-y-0.5">
              {evidenceLevels.levels.map((dimension) => (
                <span key={dimension.name} className="text-2xs">
                  <span className="text-ink-500">{dimension.label}</span>{" "}
                  <span className={
                    dimension.level === "HIGH" ? "text-pass-400"
                      : dimension.level === "MEDIUM" ? "text-ink-200"
                        : dimension.level === "LOW" ? "text-warn-400"
                          : "text-fail-400"}>
                    {dimension.level.toLowerCase()}
                  </span>
                </span>
              ))}
            </div>
            {evidenceLevels.notApplicable.length ? (
              <div className="text-3xs text-ink-500 mt-0.5">
                not applicable here: {evidenceLevels.notApplicable.join(", ")}
              </div>
            ) : null}
          </Row>
        ) : null}
        <Row label="Recognition">
          {fact.confidence_band}
          <span className="text-ink-500">
            {" "}· {periodConfidenceLabel(fact.confidence)} — how clearly the source was
            read, not a verdict on the figure
          </span>
        </Row>
        <Row label="Review status">
          {fact.requires_review ? (
            <span className="text-amber-400">
              {/* The safety gate's own sentence comes first: it names the
                  missing field in plain words, where a review reason is an
                  enum a reader has to decode. */}
              {source.gateMessage
                || fact.review_explanation
                || fact.review_reason?.replace(/_/g, " ")
                || "needs a look"}
            </span>
          ) : (
            <span className="text-ink-400">not required</span>
          )}
        </Row>
        <Row label="Accounting validation">
          {fact.validation_status === "FAILED" ? (
            <span className="text-warn-400">
              An accounting consistency check did not agree with this figure.
            </span>
          ) : fact.validation_status === "WARNING" ? (
            <span className="text-warn-400">
              An accounting check came close but did not agree within tolerance.
            </span>
          ) : fact.validation_status === "PASSED" ? (
            <span className="text-pass-400">
              An accounting check computed from other figures agrees with this
              one.
            </span>
          ) : (
            <span className="text-ink-400">no check applied to this figure</span>
          )}
        </Row>
        <Row label="Corroboration">
          {evidence.state === "corroborated"
            ? <span className="text-pass-400">
                read the same by {evidence.corroborating.length} other source
                {evidence.corroborating.length === 1 ? "" : "s"}
              </span>
            : <span className="text-ink-400">no independent reading</span>}
        </Row>
        <Row label="Conflict state">
          {fact.duplicate_resolution === "genuine_conflict"
            ? <span className="text-amber-400">another printing disagrees — both kept</span>
            : fact.duplicate_resolution === "different_qualification"
              ? <span className="text-ink-200">another printing is on a different stated basis</span>
              : fact.duplicate_resolution === "different_definition"
                ? <span className="text-ink-200">another line maps to the same metric</span>
                : <span className="text-ink-400">none</span>}
        </Row>

        {/* ---- where it came from: the chain a reader can check ------------ */}
        <Row label="Extraction strategy">
          {strategy ? strategy.label : <Unknown>not recorded</Unknown>}
        </Row>
        <Row label="Page">page {fact.source_page}</Row>
        <Row label="Table" mono>
          {fact.provenance?.table_id ?? fact.table_id ?? <Unknown>not a table figure</Unknown>}
        </Row>
        <Row label="Row">
          {typeof fact.meta?.["row"] === "number"
            ? <>row {(fact.meta["row"] as number) + 1} · “{fact.metric}”</>
            : <Unknown>—</Unknown>}
        </Row>
        <Row label="Column">
          {typeof fact.meta?.["col"] === "number"
            ? <>column {(fact.meta["col"] as number) + 1}</>
            : <Unknown>—</Unknown>}
        </Row>
        <Row label="Column header" mono>
          {source.columnHeader
            ? <span className="text-ink-100">“{source.columnHeader}”</span>
            : <Unknown>no header read for this column</Unknown>}
        </Row>
        <Row label="Source region">
          {fact.provenance?.bbox ? (
            <button
              className="text-accent-400 hover:underline"
              onClick={() => onOpenSource?.(fact.source_page)}
            >
              show on page {fact.source_page}
            </button>
          ) : (
            <Unknown>no region recorded</Unknown>
          )}
        </Row>

        <button
          className="mt-3 flex items-center gap-1 text-2xs text-ink-500 hover:text-ink-300"
          onClick={() => setShowProvenance((v) => !v)}
        >
          {showProvenance ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          Provenance
        </button>
        {showProvenance ? (
          <div className="mt-1.5">
            <Row label="Source text" mono>
              {fact.original_text || <Unknown>—</Unknown>}
            </Row>
            <Row label="Table shape" mono>
              {source.shape ?? <Unknown>—</Unknown>}
            </Row>
          </div>
        ) : null}
      </div>
    </div>
  );
}
