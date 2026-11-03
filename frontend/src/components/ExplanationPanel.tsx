/**
 * "Why does the system believe this figure?"
 *
 * Every line here is read back from what the pipeline recorded: the page and
 * box the number came from, the row label that gave it meaning, the column that
 * dated it, the unit that scaled it, the entity it belongs to, the checks it
 * passed, and each confidence component with the weight it carried.
 *
 * The panel states plainly that no language model produced any of it, because a
 * reviewer needs to know whether they are reading evidence or prose.
 */
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";

import { api } from "../api/client";
import { ErrorNote, Spinner } from "./ui";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-4">
      <h4 className="text-[11px] uppercase tracking-wide text-ink-300 mb-1">{title}</h4>
      <div className="text-sm text-ink-100 space-y-1">{children}</div>
    </section>
  );
}

export function ExplanationPanel({
  documentId,
  factId,
  onClose,
  onOpenPage,
}: {
  documentId: string;
  factId: string;
  onClose: () => void;
  onOpenPage?: (page: number) => void;
}) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["explanation", documentId, factId],
    queryFn: () => api.explanation(documentId, factId),
  });

  return (
    <aside className="w-[26rem] shrink-0 border-l border-ink-750 bg-ink-900 flex flex-col">
      <header className="flex items-center justify-between px-4 py-3 border-b border-ink-750">
        <h3 className="text-sm font-semibold text-ink-100">Why this figure</h3>
        <button type="button" onClick={onClose} aria-label="Close explanation">
          <X className="h-4 w-4 text-ink-300 hover:text-ink-200" />
        </button>
      </header>

      <div className="flex-1 overflow-auto px-4 py-3">
        {isLoading && <Spinner label="Building explanation" />}
        {error && <ErrorNote error={error} onRetry={() => refetch()} />}
        {data && (
          <>
            <div className="mb-4">
              <div className="text-lg text-ink-50 tabular-nums">{data.formatted_value}</div>
              <div className="text-sm text-ink-200">{data.metric_display}</div>
              <div className="text-xs text-ink-300">
                {[data.entity_name, data.period].filter(Boolean).join(" · ")}
              </div>
              {data.requires_review && (
                <div className="mt-2 text-xs text-warn-400 bg-warn-900 border border-amber-200 rounded px-2 py-1">
                  Flagged for human review.
                </div>
              )}
            </div>

            <Section title="Why this metric">{data.why_this_metric}</Section>
            <Section title="Why this period">{data.why_this_period}</Section>
            <Section title="Why this entity">{data.why_this_entity}</Section>
            <Section title="Why this unit">{data.why_this_unit}</Section>

            <Section title="Reported or calculated">
              <p>{data.derivation_note}</p>
              {data.formula && (
                <p className="font-mono text-xs bg-ink-850 border border-ink-750 rounded px-2 py-1">
                  {data.formula}
                </p>
              )}
            </Section>

            <Section title={`Confidence — ${(data.confidence * 100).toFixed(0)}% (${data.confidence_band})`}>
              <ul className="space-y-1.5">
                {data.confidence_components.map((c) => (
                  <li key={c.name}>
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-ink-200">
                        {c.name.replace(/_/g, " ")}
                        {c.weight > 0 && (
                          <span className="text-ink-300"> · weight {(c.weight * 100).toFixed(0)}%</span>
                        )}
                      </span>
                      <span className="tabular-nums text-ink-300">
                        {(c.value * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="h-1 bg-ink-800 rounded overflow-hidden mt-0.5">
                      <div
                        className="h-full bg-ink-600"
                        style={{ width: `${Math.max(0, Math.min(1, c.value)) * 100}%` }}
                      />
                    </div>
                    {c.reason && <p className="text-[11px] text-ink-300 mt-0.5">{c.reason}</p>}
                  </li>
                ))}
              </ul>
            </Section>

            <Section title="Validation">
              <ul className="space-y-1">
                {data.validation_summary.map((v, i) => (
                  <li key={i} className="text-xs">
                    <span className="font-medium text-ink-200">{v.rule}</span>{" "}
                    <span className="text-ink-300">({v.status})</span>
                    <p className="text-ink-300">{v.message}</p>
                  </li>
                ))}
              </ul>
            </Section>

            {data.related_facts.length > 0 && (
              <Section title="Related figures">
                <ul className="space-y-1">
                  {data.related_facts.slice(0, 12).map((r, i) => (
                    <li key={i} className="text-xs text-ink-300">
                      <span className="font-mono">{String(r.fact_id).slice(-8)}</span>{" "}
                      <span className="text-ink-300">{r.kind}</span>
                      {r.basis && <span className="text-ink-300"> · {r.basis}</span>}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {data.provenance && (
              <Section title="Source">
                <p className="text-xs">
                  Page {data.provenance.page_number}
                  {onOpenPage && (
                    <button
                      type="button"
                      className="ml-2 text-brand-600 hover:underline"
                      onClick={() => onOpenPage(data.provenance!.page_number)}
                    >
                      open page
                    </button>
                  )}
                </p>
                {data.provenance.original_text && (
                  <p className="text-xs text-ink-300 italic mt-1">
                    “{data.provenance.original_text}”
                  </p>
                )}
              </Section>
            )}

            <p className="text-[11px] text-ink-300 border-t border-ink-800 pt-2 mt-4">
              Generated by {data.generated_by}.
            </p>
          </>
        )}
      </div>
    </aside>
  );
}
