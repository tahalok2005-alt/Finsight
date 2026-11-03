import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownUp, Crosshair, FileSearch, Filter } from "lucide-react";
import clsx from "clsx";
import { api, type FactQuery } from "../../api/client";
import type { BBox, FinancialFact, ReviewReason } from "../../types";
import {
  ConfidenceChip, Empty, ErrorNote, Spinner, ValidationChip,
} from "../../components/ui";
import { FigureDetail } from "../../components/FigureDetail";
import { formatFull, formatGrowth } from "../../utils/format";
import { describeEvidence } from "../../utils/evidence";
import { describePeriod, describeStrategy } from "../../utils/period";
import {
  EVIDENCE_LABEL, evidenceReading, factState,
} from "../../utils/factState";

/**
 * Why a figure is in front of a person, in words rather than a code.
 *
 * "requires review" on its own tells a reader to look without telling them
 * what at. Each of these names the actual gap, so a reviewer can decide in a
 * glance whether it is theirs to settle.
 */
const REVIEW_REASON_TEXT: Record<string, string> = {
  validation_failed: "An arithmetic check did not agree",
  conflicting_sources: "Sources disagree",
  value_unparsed: "Value could not be read",
  value_disputed: "A second read of the digits disagrees",
  unit_undetermined: "Scale not stated",
  ambiguous_period: "Period ambiguous",
  low_confidence: "Read with low confidence",
  metric_ambiguous: "Which measure is not certain",
  narrative_unconfirmed: "From text; no table confirms it",
};

/** The span of a period, in the words a reader uses. */
const DURATION_TEXT: Record<string, string> = {
  quarter: "Quarter", half_year: "Half year", nine_month: "Nine months",
  fiscal_year: "Year", calendar_year: "Year", ttm: "Trailing 12 months",
  point_in_time: "Balance (at a date)", month: "Month",
};

/** The reporting basis, from the entity type the backend recorded. */
function basisText(f: FinancialFact): string {
  const t = (f.entity_type ?? "").toLowerCase();
  if (t === "standalone") return "Standalone";
  if (t === "consolidated") return "Consolidated";
  if (t === "segment") return "Segment";
  if (t === "subsidiary") return "Subsidiary";
  return "Basis not stated";
}

export function FactsTab({
  documentId, onShowProvenance, onOpenSource,
}: {
  documentId: string;
  onShowProvenance: (factId: string) => void;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const [query, setQuery] = useState<FactQuery>({
    limit: 300, sort: "page", order: "asc",
  });
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["facts", documentId, query],
    queryFn: () => api.financials(documentId, query),
  });

  const rows = useMemo(() => {
    if (!data) return [];
    if (!text.trim()) return data.items;
    const needle = text.toLowerCase();
    return data.items.filter(
      (f) =>
        f.metric.toLowerCase().includes(needle) ||
        f.canonical_metric.toLowerCase().includes(needle) ||
        (f.entity ?? "").toLowerCase().includes(needle) ||
        (f.period ?? "").toLowerCase().includes(needle),
    );
  }, [data, text]);

  const detail = useMemo(
    () => rows.find((f) => f.fact_id === selected) ?? null,
    [rows, selected],
  );

  const set = (patch: Partial<FactQuery>) =>
    setQuery((q) => ({ ...q, ...patch, offset: 0 }));

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="flex items-center gap-2 px-4 py-2 border-b border-ink-800 flex-wrap">
        <div className="relative">
          <Filter
            size={12}
            className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-500"
          />
          <input
            className="input pl-7 w-56"
            placeholder="Filter metric, entity, period…"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <button
          className="btn-ghost btn-sm"
          onClick={() =>
            set({ sort: query.sort === "confidence" ? "page" : "confidence" })
          }
        >
          <ArrowDownUp size={12} />
          {query.sort === "confidence" ? "By confidence" : "By page"}
        </button>
        <button
          className={clsx("btn-ghost btn-sm",
            query.requires_review && "text-warn-400")}
          onClick={() =>
            set({ requires_review: query.requires_review ? undefined : true })}
        >
          Needs review
        </button>
        <span className="ml-auto text-xs text-ink-400 num">
          {rows.length}
          {data && rows.length !== data.total && ` of ${data.total}`} figures
        </span>
      </div>

      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0 overflow-auto">
          {isLoading && <Spinner label="Loading financial figures…" />}
          {error && (
            <div className="p-6">
              <ErrorNote error={error} onRetry={refetch} />
            </div>
          )}
          {data && rows.length === 0 && (
            <Empty
              title="No figures match these filters"
              hint="Clear a filter, or check the Validation tab if extraction reported problems."
            />
          )}
          {rows.length > 0 && (
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th w-[26%]">Metric</th>
                  <th className="th w-[16%]">Period</th>
                  <th className="th text-right w-[16%]">Value</th>
                  <th className="th w-[14%]">Entity</th>
                  <th className="th w-[17%]">Status</th>
                  <th className="th w-[11%]">Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((f) => (
                  <FactRow
                    key={f.fact_id}
                    fact={f}
                    selected={f.fact_id === selected}
                    onSelect={setSelected}
                    onShowProvenance={onShowProvenance}
                    onOpenSource={onOpenSource}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>
        {detail ? (
          <div className="w-[350px] shrink-0 min-h-0 overflow-hidden">
            <FigureDetail
              fact={detail}
              onClose={() => setSelected(null)}
              onOpenSource={(page) =>
                onOpenSource(page, detail.provenance?.bbox_relative ?? null)}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FactRow({
  fact: f, selected, onSelect, onShowProvenance, onOpenSource,
}: {
  fact: FinancialFact;
  selected: boolean;
  onSelect: (id: string) => void;
  onShowProvenance: (id: string) => void;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const corrected = f.corrected_value !== null;
  const period = describePeriod(f);
  const strategy = describeStrategy(f);
  const evidence = describeEvidence(f);
  const state = factState(f);
  const evidenceLevels = evidenceReading(f);
  const sourceLabel = f.metric || f.metric_display || "Unlabelled row";
  const canonicalLabel = f.metric_display || f.canonical_metric;
  const mappedDifferently = Boolean(canonicalLabel)
    && canonicalLabel.toLowerCase() !== sourceLabel.toLowerCase();
  const changeBps = f.meta?.change_bps as number | undefined;
  const reportedBps = f.meta?.reported_change_bps as number | undefined;
  const reviewText = f.review_reason
    ? REVIEW_REASON_TEXT[f.review_reason as ReviewReason] ?? null
    : null;

  return (
    <tr
      className={clsx("group cursor-pointer",
        selected ? "bg-ink-800/70" : "hover:bg-ink-850/60")}
      onClick={() => onSelect(f.fact_id)}
    >
      {/* The line item as the document printed it leads. The canonical name is
          a mapping this system made, and several distinct source lines share
          one — "Other current assets" and "Total current assets" both map to
          Current Assets. Showing only the canonical name puts identically
          labelled rows carrying different values side by side. */}
      <td className="td align-top py-2">
        <div className="font-medium text-ink-100 leading-snug"
             title={mappedDifferently
               ? `Printed as "${sourceLabel}". Recognised as ${canonicalLabel}.`
               : sourceLabel}>
          {sourceLabel}
        </div>
        {mappedDifferently ? (
          <div className="text-2xs text-ink-400 truncate max-w-[240px] mt-0.5">
            {canonicalLabel}
          </div>
        ) : null}
        {/* A figure stated on a basis the source names -- "excluding" a
            one-off gain, "Adjusted" -- is a different figure from the plain
            measure beside it, and says so here. */}
        {f.qualification ? (
          <div className="text-2xs text-accent-400 truncate max-w-[240px] mt-0.5"
               title={f.qualification.source_text ?? ""}>
            {f.qualification.qualification_label}
          </div>
        ) : null}
      </td>

      <td className="td align-top py-2">
        <div
          className={clsx(
            "text-xs leading-snug",
            period.certainty === "none" && "text-warn-400",
            period.certainty === "not-applicable" && "text-ink-400",
            period.certainty === "low" && "text-ink-300",
          )}
          title={period.title}
        >
          {period.primary}
        </div>
        {/* The span sits under the period rather than in a column of its own,
            and how the period was arrived at moved to the detail panel: both
            were a second line on every row for a question most rows never
            raise. */}
        <div className="text-2xs mt-0.5">
          {f.period_type && DURATION_TEXT[f.period_type] ? (
            <span className="text-ink-500">{DURATION_TEXT[f.period_type]}</span>
          ) : (
            <span className="text-warn-400">Span not established</span>
          )}
        </div>
      </td>

      <td className="td text-right align-top py-2">
        <div className="num font-medium text-ink-100 whitespace-nowrap">
          {formatFull(f.effective_value, f.currency, f.unit, f.value_type)}
        </div>
        {corrected && (
          <div className="text-2xs text-warn-400">
            corrected · extracted{" "}
            {formatFull(f.value, f.currency, f.unit, f.value_type)}
          </div>
        )}
        {/* Scale and currency are part of the figure, so they are printed with
            it. Only what is missing or carried from elsewhere needs saying;
            the raw characters and the rest live in the detail panel. */}
        {f.value_type === "monetary"
          && (!f.unit || f.unit === "none"
              || !f.currency || f.currency === "UNKNOWN" || f.currency_inferred) ? (
          <div className="text-2xs text-warn-400">
            {!f.unit || f.unit === "none" ? "scale not stated" : null}
            {(!f.unit || f.unit === "none")
              && (!f.currency || f.currency === "UNKNOWN") ? " · " : null}
            {!f.currency || f.currency === "UNKNOWN"
              ? "currency unknown"
              : f.currency_inferred ? "currency inferred" : null}
          </div>
        ) : null}
        {f.growth !== null ? (
          <div className="text-2xs text-ink-500 whitespace-nowrap">
            {formatGrowth(f.growth)} {f.growth_type ?? ""}{" "}
            {f.growth_reported ? "reported" : "calculated"}
          </div>
        ) : reportedBps !== undefined || changeBps !== undefined ? (
          <div className="text-2xs text-ink-500">
            {(reportedBps ?? changeBps)!.toFixed(0)} bps{" "}
            {reportedBps !== undefined ? "reported" : "calculated"}
          </div>
        ) : null}
      </td>

      <td className="td align-top py-2 text-xs">
        <div className="text-ink-200 truncate max-w-[140px]">
          {f.entity ?? <span className="text-warn-400 italic">Not attributed</span>}
        </div>
        <div className="text-2xs text-ink-500">{basisText(f)}</div>
      </td>

      {/* Confidence, what the document's own arithmetic made of it, and what
          the rest of the document said about the same figure. The last of
          those is the one that changes what a reader should do next. */}
      <td className="td align-top py-2">
        <div className="flex items-center gap-1 flex-wrap">
          {/* The evidence model's verdict leads, because it answers the
              question a reader has. The recognition score stays beside it --
              it is still what orders a review queue -- but it no longer
              speaks first, since a figure can be read perfectly and still be
              unusable for want of a period. */}
          {state ? (
            <span className={state.chip} title={state.title}>{state.label}</span>
          ) : (
            <ConfidenceChip band={f.confidence_band} value={f.confidence} />
          )}
          {f.validation_status !== "NOT_CHECKED" && (
            <ValidationChip status={f.validation_status} />
          )}
        </div>
        {/* One line at most: what a reader would have to act on. Which
            dimension limited the verdict, and what corroborated it, are in the
            detail panel one click away -- on the row they were two more lines
            on every figure, including the ones nobody needs to look at. */}
        {reviewText ? (
          <div className="text-2xs text-warn-400 mt-1">{reviewText}</div>
        ) : evidence.state === "corroborated" ? (
          <div className="text-2xs text-pass-400 mt-1" title={evidence.summary ?? ""}>
            Confirmed by a second source
          </div>
        ) : state && evidenceLevels?.limiting ? (
          <div className="text-2xs text-ink-500 mt-1">
            limited by {EVIDENCE_LABEL[evidenceLevels.limiting]?.toLowerCase()
              ?? evidenceLevels.limiting}
          </div>
        ) : null}
      </td>

      <td className="td align-top py-2">
        <div className="flex items-center gap-1">
          <button
            className="btn-ghost btn-sm opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={(e) => { e.stopPropagation(); onShowProvenance(f.fact_id); }}
            title="Show the exact source"
          >
            <FileSearch size={12} />
          </button>
          <button
            className="btn-ghost btn-sm opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={(e) => {
              e.stopPropagation();
              onOpenSource(f.source_page, f.provenance?.bbox_relative ?? null);
            }}
            title="Open the source page"
          >
            <Crosshair size={12} />
          </button>
          <span className="text-2xs text-ink-500 num ml-1">p{f.source_page}</span>
        </div>
        {/* How this number was found. The same metric can arrive from a
            statement table, a KPI tile or a sentence, and which route it took
            is part of knowing how far to lean on it. */}
        {strategy ? (
          <div className="text-2xs text-ink-400 mt-0.5" title={strategy.title}>
            {strategy.label}
          </div>
        ) : null}
      </td>
    </tr>
  );
}
