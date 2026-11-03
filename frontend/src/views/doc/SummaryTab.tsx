/**
 * Financial summary: extracted facts organised the way a statement is read --
 * by statement, then category, then metric, with each metric's periods across
 * the row.
 *
 * Two rules the UI must never break, because the backend went to some trouble
 * to preserve them:
 *
 *  - a metric row belongs to exactly one reporting entity, so figures from
 *    different entities are never shown on the same line;
 *  - a calculated figure is labelled as calculated wherever it appears.
 */
import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronRight, Info } from "lucide-react";

import { api } from "../../api/client";
import type { EntityNode, StatementGroup, SummaryMetric, SummaryValue } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";
import { formatFull } from "../../utils/format";
import { describePeriod } from "../../utils/period";

const STATEMENT_LABELS: Record<string, string> = {
  income_statement: "Income statement",
  balance_sheet: "Balance sheet",
  cash_flow: "Cash flow",
  equity: "Equity",
  ratio: "Ratios",
  segment: "Segment",
  operational: "Operational KPIs",
  notes: "Notes",
  unknown: "Unclassified",
};

const CATEGORY_LABELS: Record<string, string> = {
  revenue: "Revenue",
  earnings: "Earnings",
  costs: "Costs",
  margins: "Margins",
  returns: "Returns",
  leverage: "Leverage",
  liquidity: "Liquidity",
  cashflow: "Cash flow",
  investment: "Investment",
  per_share: "Per share",
  balance_sheet: "Balance sheet",
  segment: "Segment",
  operational: "Operational",
  other: "Other",
};

function flattenEntities(nodes: EntityNode[], depth = 0): { node: EntityNode; depth: number }[] {
  return nodes.flatMap((n) => [{ node: n, depth }, ...flattenEntities(n.children ?? [], depth + 1)]);
}

/** A column head, and the words it is shown with. */
interface PeriodColumn {
  /** The label the values are keyed by. */
  key: string;
  /** What the reader sees — the exact date where one was established. */
  head: string;
  /** The long form, for the tooltip. */
  title: string;
}

function periodColumns(group: StatementGroup): PeriodColumn[] {
  const seen = new Map<string, number>();
  const sample = new Map<string, SummaryValue>();
  for (const category of group.categories) {
    for (const metric of category.metrics) {
      for (const v of metric.values) {
        if (!v.period) continue;
        seen.set(v.period, (seen.get(v.period) ?? 0) + 1);
        if (!sample.has(v.period)) sample.set(v.period, v);
      }
    }
  }
  // Most-populated periods first, capped so the table stays readable.
  const keys = [...seen.entries()]
    .sort((a, b) => b[1] - a[1]).slice(0, 5).map(([p]) => p).sort().reverse();
  // A head must say which period it is. Two columns can both be labelled "H1"
  // and end on different days, so the exact date leads here exactly as it does
  // in the figures table — via the same formatter, so they never disagree.
  return keys.map((key) => {
    const v = sample.get(key);
    const d = v ? describePeriod(v) : null;
    return { key, head: d ? d.primary : key, title: d ? d.title : key };
  });
}

/**
 * What a statement looks like when it cannot be laid out as a statement.
 *
 * The figures are real and worth showing; only the grid is not. Each one is
 * given its own line with the context that identifies it — the entity it
 * belongs to, the period where one was established, and the page it came
 * from — which is exactly what the grid would have conveyed by position.
 */
function StatementFallback({
  group, onExplain,
}: {
  group: StatementGroup;
  onExplain: (factId: string) => void;
}) {
  const rows = group.categories.flatMap((c) =>
    c.metrics.flatMap((m) => m.values.map((v) => ({ metric: m, value: v }))),
  );
  return (
    <section className="mb-6">
      <h3 className="text-sm font-semibold text-ink-100 mb-2">
        {STATEMENT_LABELS[group.statement] ?? group.statement}
        <span className="ml-2 text-xs font-normal text-ink-300">
          {group.fact_count} figures
        </span>
      </h3>
      <p className="text-xs text-ink-300 leading-relaxed mb-3 max-w-3xl">
        FinSight identified these figures in this section, but the source
        structure was not sufficient to reconstruct a reliable comparative
        statement.{group.comparative?.reason ? ` ${group.comparative.reason}` : ""}
      </p>
      <div className="border border-ink-750 rounded-lg bg-ink-900 divide-y divide-ink-800">
        {rows.map(({ metric, value }) => {
          const p = describePeriod(value);
          return (
            <div key={value.fact_id}
                 className="flex items-baseline gap-3 px-3 py-2 hover:bg-ink-850">
              <div className="min-w-0 flex-1">
                <div className="text-sm text-ink-100 truncate">
                  {metric.metric_display}
                </div>
                <div className="text-2xs text-ink-300 truncate">
                  {[metric.entity_name, p.primary].filter(Boolean).join(" · ")}
                </div>
              </div>
              <button
                type="button"
                onClick={() => onExplain(value.fact_id)}
                className="num text-sm text-ink-100 tabular-nums hover:underline decoration-dotted"
                title="Explain this figure"
              >
                {formatFull(value.effective_value ?? value.value, value.currency,
                            value.unit, value.value_type)}
              </button>
              <span className="text-2xs text-ink-500 num w-10 text-right shrink-0">
                {value.source_page != null ? `p${value.source_page}` : ""}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function MetricRow({
  metric,
  periods,
  onExplain,
}: {
  metric: SummaryMetric;
  periods: PeriodColumn[];
  onExplain: (factId: string) => void;
}) {
  const byPeriod = new Map(metric.values.filter((v) => v.period).map((v) => [v.period!, v]));
  return (
    <tr className="border-t border-ink-800 hover:bg-ink-850">
      <td className="py-1.5 pr-3 pl-2">
        <div className="text-ink-100">{metric.metric_display}</div>
        {metric.entity_name && (
          <div className="text-[11px] text-ink-300">{metric.entity_name}</div>
        )}
      </td>
      {periods.map((p) => {
        const v = byPeriod.get(p.key);
        if (!v) return <td key={p.key} className="py-1.5 px-3 text-right text-ink-300">—</td>;
        return (
          <td key={p.key} className="py-1.5 px-3 text-right tabular-nums">
            <button
              type="button"
              onClick={() => onExplain(v.fact_id)}
              className="hover:underline decoration-dotted"
              title="Explain this figure"
            >
              {formatFull(v.effective_value ?? v.value, v.currency, v.unit, v.value_type)}
            </button>
            {v.growth != null && (
              <div
                className={`text-[11px] ${v.growth >= 0 ? "text-pass-400" : "text-fail-400"}`}
                title={
                  v.growth_reported
                    ? "Change stated by the document"
                    : "Change computed by FinSight from the two period values"
                }
              >
                {v.growth >= 0 ? "+" : ""}
                {v.growth.toFixed(1)}%{v.growth_reported ? "" : "*"}
              </div>
            )}
            {v.requires_review && (
              <AlertTriangle className="inline h-3 w-3 text-warn-400 ml-1" aria-label="needs review" />
            )}
          </td>
        );
      })}
    </tr>
  );
}

export function SummaryTab({
  documentId,
  onExplain,
}: {
  documentId: string;
  onExplain: (factId: string) => void;
}) {
  const [entityId, setEntityId] = useState<string>("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const entities = useQuery({
    queryKey: ["entities", documentId],
    queryFn: () => api.entities(documentId),
  });
  const summary = useQuery({
    queryKey: ["financial-summary", documentId, entityId],
    queryFn: () => api.financialSummary(documentId, entityId || undefined),
  });

  const flat = useMemo(
    () => flattenEntities(entities.data?.hierarchy ?? []),
    [entities.data],
  );

  if (summary.isLoading) return <Spinner label="Loading financial summary" />;
  if (summary.error) return <ErrorNote error={summary.error} onRetry={() => summary.refetch()} />;

  const groups = (summary.data?.statements ?? []).filter((g) => g.fact_count > 0);
  if (!groups.length) {
    return <Empty
        title="No classified figures"
        hint="No extracted figure could be placed in a financial statement."
      />;
  }

  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <div className="h-full overflow-auto px-6 py-4">
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <label className="text-xs text-ink-300">Reporting entity</label>
        <select
          value={entityId}
          onChange={(e) => setEntityId(e.target.value)}
          className="text-sm border border-ink-750 rounded px-2 py-1 bg-ink-900"
        >
          <option value="">All entities ({summary.data?.total_facts ?? 0} figures)</option>
          {flat.map(({ node, depth }) => (
            <option key={node.entity_id} value={node.entity_id}>
              {" ".repeat(depth * 3)}
              {node.entity_name} ({node.entity_type}) — {node.fact_count}
            </option>
          ))}
        </select>
        <span className="text-[11px] text-ink-300 inline-flex items-center gap-1">
          <Info className="h-3 w-3" />
          An asterisk marks a change FinSight computed rather than one the document stated.
        </span>
      </div>

      {groups.map((group) => {
        const periods = periodColumns(group);
        // A grid promises that reading across a row compares like with like.
        // When the backend says most of the grid would be empty, that promise
        // cannot be kept, and a wall of dashes tells the reader nothing about
        // why. The same figures are shown as a list instead, with the reason.
        if (group.comparative && !group.comparative.renderable) {
          return (
            <StatementFallback key={group.statement} group={group}
                               onExplain={onExplain} />
          );
        }
        return (
          <section key={group.statement} className="mb-6">
            <h3 className="text-sm font-semibold text-ink-100 mb-2">
              {STATEMENT_LABELS[group.statement] ?? group.statement}
              <span className="ml-2 text-xs font-normal text-ink-300">
                {group.fact_count} figures
              </span>
            </h3>
            <div className="overflow-x-auto border border-ink-750 rounded-lg bg-ink-900">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="bg-ink-850 text-xs text-ink-300">
                    <th className="text-left py-2 pl-2 pr-3 font-medium">Metric</th>
                    {periods.map((p) => (
                      <th key={p.key} title={p.title}
                          className="text-right py-2 px-3 font-medium whitespace-nowrap">
                        {p.head}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {group.categories.map((category) => {
                    const key = `${group.statement}:${category.category}`;
                    const isCollapsed = collapsed.has(key);
                    return (
                      // The key belongs on the fragment, not on the first row
                      // inside it: React was asked to track a list whose items
                      // had no identity, and warned on every render.
                      <Fragment key={key}>
                        <tr className="bg-ink-850/60">
                          <td colSpan={periods.length + 1} className="py-1 pl-2">
                            <button
                              type="button"
                              onClick={() => toggle(key)}
                              className="inline-flex items-center gap-1 text-[11px] uppercase tracking-wide text-ink-300"
                            >
                              {isCollapsed ? (
                                <ChevronRight className="h-3 w-3" />
                              ) : (
                                <ChevronDown className="h-3 w-3" />
                              )}
                              {CATEGORY_LABELS[category.category] ?? category.category}
                              <span className="text-ink-300">({category.metric_count})</span>
                            </button>
                          </td>
                        </tr>
                        {!isCollapsed &&
                          category.metrics.map((metric, i) => (
                            <MetricRow
                              // One statement can print the same measure twice
                              // -- a share of associates before and after tax,
                              // the same line on two bases -- so entity and
                              // metric alone do not identify a row.
                              key={`${metric.entity_id}:${metric.canonical_metric}:${i}`}
                              metric={metric}
                              periods={periods}
                              onExplain={onExplain}
                            />
                          ))}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        );
      })}
    </div>
  );
}
