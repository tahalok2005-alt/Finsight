/**
 * Insights, anomalies and risk statements.
 *
 * Everything on this screen is source-backed: an insight lists the facts it was
 * computed from, an anomaly states the threshold it crossed, and a risk is a
 * verbatim quotation with its page.  Nothing here was written by a language
 * model, and the UI says so rather than leaving the reader to wonder.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowDownRight, ArrowUpRight, Calculator, FileText, Quote,
} from "lucide-react";

import { api } from "../../api/client";
import type { AnomalyDto, InsightDto, RiskDto, Severity } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";
import { titleCase } from "../../utils/format";

type Section = "insights" | "anomalies" | "risks";

const SEVERITY_STYLE: Record<Severity, string> = {
  high: "bg-fail-900 text-fail-400 border-rose-200",
  medium: "bg-warn-900 text-warn-400 border-amber-200",
  low: "bg-sky-50 text-sky-700 border-sky-200",
  info: "bg-ink-850 text-ink-200 border-ink-750",
};

function InsightCard({
  insight,
  onOpenFact,
}: {
  insight: InsightDto;
  onOpenFact: (factId: string) => void;
}) {
  const up = (insight.change ?? 0) >= 0;
  return (
    <li className="border border-ink-750 rounded-lg bg-ink-900 p-3">
      <div className="flex items-start gap-2">
        {up ? (
          <ArrowUpRight className="h-4 w-4 text-pass-400 mt-0.5 shrink-0" />
        ) : (
          <ArrowDownRight className="h-4 w-4 text-fail-400 mt-0.5 shrink-0" />
        )}
        <div className="min-w-0">
          <p className="text-sm text-ink-100">{insight.headline}</p>
          <p className="text-xs text-ink-300 mt-1">{insight.detail}</p>
          <div className="flex flex-wrap items-center gap-2 mt-2 text-[11px]">
            <span
              className={`px-1.5 py-0.5 rounded border ${
                insight.derivation === "reported"
                  ? "bg-ink-850 text-ink-200 border-ink-750"
                  : "bg-indigo-50 text-indigo-700 border-indigo-200"
              }`}
              title={
                insight.derivation === "reported"
                  ? "The document stated this change"
                  : "FinSight computed this from reported figures"
              }
            >
              {insight.derivation === "reported" ? "reported" : "calculated"}
            </span>
            <span className="text-ink-300">{titleCase(insight.insight_type)}</span>
            {insight.entity_name && (
              <span className="text-ink-300">· {insight.entity_name}</span>
            )}
            {insight.source_pages.length > 0 && (
              <span className="text-ink-300">
                · p{insight.source_pages.join(", p")}
              </span>
            )}
          </div>
          {insight.supporting_fact_ids.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              <span className="text-[11px] text-ink-300">from</span>
              {insight.supporting_fact_ids.slice(0, 6).map((fid) => (
                <button
                  key={fid}
                  type="button"
                  onClick={() => onOpenFact(fid)}
                  className="text-[11px] px-1.5 py-0.5 rounded bg-ink-800 hover:bg-ink-800 text-ink-200 font-mono"
                  title="Explain this supporting figure"
                >
                  {fid.slice(-8)}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

function AnomalyCard({
  anomaly,
  onOpenFact,
}: {
  anomaly: AnomalyDto;
  onOpenFact: (factId: string) => void;
}) {
  return (
    <li className="border border-ink-750 rounded-lg bg-ink-900 p-3">
      <div className="flex items-start gap-2">
        <AlertTriangle
          className={`h-4 w-4 mt-0.5 shrink-0 ${
            anomaly.severity === "high" ? "text-fail-400" : "text-warn-400"
          }`}
        />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`text-[11px] px-1.5 py-0.5 rounded border ${SEVERITY_STYLE[anomaly.severity]}`}
            >
              {anomaly.severity}
            </span>
            <span className="text-[11px] text-ink-300">
              {titleCase(anomaly.anomaly_type)}
            </span>
          </div>
          <p className="text-sm text-ink-100 mt-1">{anomaly.message}</p>
          <p className="text-xs text-ink-300 mt-1">{anomaly.reason}</p>
          {anomaly.threshold != null && (
            <p className="text-[11px] text-ink-300 mt-1">
              Threshold {anomaly.threshold.toLocaleString()}
              {anomaly.deviation != null && ` · observed deviation ${anomaly.deviation.toLocaleString()}`}
            </p>
          )}
          {anomaly.fact_ids.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {anomaly.fact_ids.filter(Boolean).slice(0, 6).map((fid) => (
                <button
                  key={fid}
                  type="button"
                  onClick={() => onOpenFact(fid)}
                  className="text-[11px] px-1.5 py-0.5 rounded bg-ink-800 hover:bg-ink-800 text-ink-200 font-mono"
                >
                  {fid.slice(-8)}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

function RiskCard({ risk, onOpenPage }: { risk: RiskDto; onOpenPage: (p: number) => void }) {
  return (
    <li className="border border-ink-750 rounded-lg bg-ink-900 p-3">
      <div className="flex items-start gap-2">
        <Quote className="h-4 w-4 text-ink-300 mt-0.5 shrink-0" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`text-[11px] px-1.5 py-0.5 rounded border ${SEVERITY_STYLE[risk.severity]}`}
            >
              {risk.severity}
            </span>
            <span className="text-[11px] text-ink-300">{titleCase(risk.category)}</span>
            {risk.is_forward_looking && (
              <span className="text-[11px] text-ink-300">· forward-looking</span>
            )}
            <button
              type="button"
              onClick={() => onOpenPage(risk.page_number)}
              className="text-[11px] text-brand-600 hover:underline inline-flex items-center gap-1"
            >
              <FileText className="h-3 w-3" /> page {risk.page_number}
            </button>
          </div>
          <p className="text-sm text-ink-100 mt-1 italic">“{risk.text}”</p>
          {risk.trigger_phrase && (
            <p className="text-[11px] text-ink-300 mt-1">
              matched on “{risk.trigger_phrase}”
            </p>
          )}
        </div>
      </div>
    </li>
  );
}

export function InsightsTab({
  documentId,
  onExplain,
  onOpenPage,
}: {
  documentId: string;
  onExplain: (factId: string) => void;
  onOpenPage: (page: number) => void;
}) {
  const [section, setSection] = useState<Section>("insights");

  const insights = useQuery({
    queryKey: ["insights", documentId],
    queryFn: () => api.insights(documentId, { limit: 100 }),
    enabled: section === "insights",
  });
  const anomalies = useQuery({
    queryKey: ["anomalies", documentId],
    queryFn: () => api.anomalies(documentId),
    enabled: section === "anomalies",
  });
  const risks = useQuery({
    queryKey: ["risks", documentId],
    queryFn: () => api.risks(documentId),
    enabled: section === "risks",
  });
  const derived = useQuery({
    queryKey: ["derived", documentId],
    queryFn: () => api.derivedMetrics(documentId),
  });

  const active =
    section === "insights" ? insights : section === "anomalies" ? anomalies : risks;

  return (
    <div className="h-full overflow-auto px-6 py-4">
      <div className="flex flex-wrap items-center gap-2 mb-4">
        {(["insights", "anomalies", "risks"] as Section[]).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSection(s)}
            className={`text-sm px-3 py-1 rounded-full border ${
              section === s
                ? "bg-ink-800 text-white border-ink-700"
                : "bg-ink-900 text-ink-200 border-ink-750 hover:bg-ink-850"
            }`}
          >
            {titleCase(s)}
          </button>
        ))}
        {derived.data && (
          <span className="ml-auto text-[11px] text-ink-300 inline-flex items-center gap-1">
            <Calculator className="h-3 w-3" />
            {derived.data.total} ratios computed,{" "}
            {derived.data.disagreements} disagreeing with the document
          </span>
        )}
      </div>

      {active.isLoading && <Spinner label={`Loading ${section}`} />}
      {active.error && <ErrorNote error={active.error} onRetry={() => active.refetch()} />}

      {section === "insights" && insights.data && (
        <>
          <p className="text-[11px] text-ink-300 mb-3">{insights.data.note}</p>
          {insights.data.items.length === 0 ? (
            <Empty
              title="No insights"
              hint="An insight needs at least two comparable figures for the same metric and entity."
            />
          ) : (
            <ul className="space-y-2">
              {insights.data.items.map((i) => (
                <InsightCard key={i.id} insight={i} onOpenFact={onExplain} />
              ))}
            </ul>
          )}
        </>
      )}

      {section === "anomalies" && anomalies.data && (
        <>
          <p className="text-[11px] text-ink-300 mb-3">
            Each anomaly names the rule it broke and the threshold it crossed, so it
            can be checked against the page.
          </p>
          {anomalies.data.items.length === 0 ? (
            <Empty title="No anomalies" hint="No extracted figure crossed a detection threshold." />
          ) : (
            <ul className="space-y-2">
              {anomalies.data.items.map((a) => (
                <AnomalyCard key={a.id} anomaly={a} onOpenFact={onExplain} />
              ))}
            </ul>
          )}
        </>
      )}

      {section === "risks" && risks.data && (
        <>
          <p className="text-[11px] text-ink-300 mb-3">{risks.data.note}</p>
          {risks.data.items.length === 0 ? (
            <Empty
              title="No cautionary statements"
              hint="The document's narrative contained no risk language, or none outside the standard safe-harbour notice."
            />
          ) : (
            <ul className="space-y-2">
              {risks.data.items.map((r) => (
                <RiskCard key={r.id} risk={r} onOpenPage={onOpenPage} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
