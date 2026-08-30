import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FileSearch, History, Pencil, X } from "lucide-react";
import { api } from "../../api/client";
import {
  ConfidenceChip, Empty, ErrorNote, Spinner, ValidationChip,
} from "../../components/ui";
import { formatFull } from "../../utils/format";

/**
 * The human-in-the-loop queue.
 *
 * An edit records a *correction* alongside the extracted value; the machine's
 * reading is never overwritten, so the two can always be compared afterwards.
 */
export function ReviewTab({
  documentId, onShowProvenance,
}: {
  documentId: string;
  onShowProvenance: (factId: string) => void;
}) {
  const qc = useQueryClient();
  const [includeResolved, setIncludeResolved] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const [historyFor, setHistoryFor] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["review", documentId, includeResolved],
    queryFn: () =>
      api.reviewQueue(documentId, { limit: 200, include_resolved: includeResolved }),
  });

  const act = useMutation({
    mutationFn: (p: {
      target_id: string;
      action: string;
      note?: string;
      new_value?: Record<string, unknown>;
    }) => api.submitReview(documentId, p),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review", documentId] });
      qc.invalidateQueries({ queryKey: ["facts", documentId] });
      qc.invalidateQueries({ queryKey: ["document", documentId] });
      qc.invalidateQueries({ queryKey: ["overview"] });
      setEditing(null);
      setDraft("");
      setNote("");
    },
  });

  const history = useQuery({
    queryKey: ["review-history", documentId, historyFor],
    queryFn: () => api.reviewHistory(documentId, historyFor ?? undefined),
    enabled: !!historyFor,
  });

  if (isLoading) return <Spinner label="Loading review queue…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 py-3 border-b border-ink-800 flex items-center gap-3">
        <p className="text-sm">
          <span className="num font-semibold">{data?.total ?? 0}</span>{" "}
          <span className="text-ink-400">
            item{data?.total === 1 ? "" : "s"} awaiting a decision
          </span>
        </p>
        <label className="ml-auto flex items-center gap-2 text-xs text-ink-400">
          <input
            type="checkbox"
            checked={includeResolved}
            onChange={(e) => setIncludeResolved(e.target.checked)}
          />
          Show already reviewed
        </label>
      </div>

      {!data?.items.length ? (
        <Empty
          title="Nothing needs review"
          hint="Facts enter this queue when their confidence is low or an arithmetic check disagreed with the document."
        />
      ) : (
        <ul className="divide-y divide-ink-850">
          {data.items.map((item) => (
            <li key={item.fact_id} className="p-4">
              <div className="flex items-start gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">
                      {item.metric_display || item.metric}
                    </span>
                    {item.entity && (
                      <span className="text-2xs text-ink-400">{item.entity}</span>
                    )}
                    {item.period && (
                      <span className="chip bg-ink-800 border border-ink-700 text-ink-300">
                        {item.period}
                      </span>
                    )}
                    <ConfidenceChip
                      band={item.confidence_band}
                      value={item.confidence}
                    />
                    <ValidationChip status={item.validation_status} />
                    {item.review_state !== "pending" && (
                      <span className="chip bg-pass-900 text-pass-400 border border-pass-500/30">
                        {item.review_state}
                      </span>
                    )}
                  </div>

                  <div className="mt-2 flex items-baseline gap-3">
                    <span className="num text-lg font-semibold">
                      {formatFull(
                        item.corrected_value ?? item.value,
                        item.currency,
                        item.unit,
                        null,
                      )}
                    </span>
                    {item.corrected_value !== null && (
                      <span className="text-2xs text-warn-400">
                        corrected from{" "}
                        {formatFull(item.value, item.currency, item.unit, null)}
                      </span>
                    )}
                    <span className="text-2xs text-ink-500 font-mono">
                      read as “{item.raw_value}”
                    </span>
                  </div>

                  {item.original_text && (
                    <p className="text-2xs text-ink-400 mt-1.5 font-mono line-clamp-2">
                      {item.original_text}
                    </p>
                  )}

                  {item.confidence_breakdown?.notes?.length ? (
                    <ul className="mt-2 space-y-0.5">
                      {item.confidence_breakdown.notes.slice(0, 3).map((n, i) => (
                        <li key={i} className="text-2xs text-ink-500">
                          • {n}
                        </li>
                      ))}
                    </ul>
                  ) : null}

                  {editing === item.fact_id && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <input
                        className="input w-40 num"
                        placeholder="Corrected value"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        autoFocus
                      />
                      <input
                        className="input flex-1 min-w-[180px]"
                        placeholder="Note (optional)"
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                      />
                      <button
                        className="btn-primary btn-sm"
                        disabled={!draft.trim() || act.isPending}
                        onClick={() =>
                          act.mutate({
                            target_id: item.fact_id,
                            action: "edit",
                            note: note || undefined,
                            new_value: { value: Number(draft) },
                          })
                        }
                      >
                        Save correction
                      </button>
                      <button
                        className="btn-ghost btn-sm"
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>

                <div className="flex flex-col gap-1.5 shrink-0">
                  <button
                    className="btn-ghost btn-sm"
                    onClick={() => onShowProvenance(item.fact_id)}
                  >
                    <FileSearch size={12} /> Source
                  </button>
                  <button
                    className="btn-ghost btn-sm !text-pass-400"
                    disabled={act.isPending}
                    onClick={() =>
                      act.mutate({ target_id: item.fact_id, action: "approve" })
                    }
                  >
                    <Check size={12} /> Approve
                  </button>
                  <button
                    className="btn-ghost btn-sm"
                    onClick={() => {
                      setEditing(item.fact_id);
                      setDraft(String(item.corrected_value ?? item.value ?? ""));
                    }}
                  >
                    <Pencil size={12} /> Edit
                  </button>
                  <button
                    className="btn-ghost btn-sm !text-fail-400"
                    disabled={act.isPending}
                    onClick={() =>
                      act.mutate({ target_id: item.fact_id, action: "reject" })
                    }
                  >
                    <X size={12} /> Reject
                  </button>
                  <button
                    className="btn-ghost btn-sm"
                    onClick={() =>
                      setHistoryFor(historyFor === item.fact_id ? null : item.fact_id)
                    }
                  >
                    <History size={12} /> History
                  </button>
                </div>
              </div>

              {historyFor === item.fact_id && (
                <div className="mt-3 ml-1 border-l-2 border-ink-800 pl-3">
                  {history.isLoading && (
                    <p className="text-2xs text-ink-500">Loading history…</p>
                  )}
                  {history.data?.items.length === 0 && (
                    <p className="text-2xs text-ink-500">No review actions yet.</p>
                  )}
                  <ul className="space-y-1.5">
                    {history.data?.items.map((h: any) => (
                      <li key={h.id} className="text-2xs text-ink-400">
                        <span className="text-ink-200 font-medium">{h.action}</span>{" "}
                        by {h.reviewer}
                        {h.note && <span className="text-ink-500"> — {h.note}</span>}
                        {h.new_value?.corrected_value != null && (
                          <span className="num text-warn-400">
                            {" "}→ {h.new_value.corrected_value}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {act.error && (
        <div className="p-4">
          <ErrorNote error={act.error} />
        </div>
      )}
    </div>
  );
}
