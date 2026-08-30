import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Crosshair, Quote } from "lucide-react";
import clsx from "clsx";
import { api } from "../../api/client";
import type { BBox } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";
import { titleCase } from "../../utils/format";

const SENTIMENT_STYLE: Record<string, string> = {
  positive: "bg-pass-900 text-pass-400 border border-pass-500/30",
  negative: "bg-fail-900 text-fail-400 border border-fail-500/30",
  mixed: "bg-warn-900 text-warn-400 border border-warn-500/30",
  neutral: "bg-ink-800 text-ink-400 border border-ink-700",
};

export function CommentaryTab({
  documentId, onOpenSource,
}: {
  documentId: string;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const [topic, setTopic] = useState("");
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["commentary", documentId, topic],
    queryFn: () => api.commentary(documentId, { topic }),
  });

  if (isLoading) return <Spinner label="Loading commentary…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );
  if (!data?.items.length && !topic)
    return <Empty title="No management commentary was extracted" />;

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 py-3 border-b border-ink-800 flex flex-wrap items-center gap-1.5">
        <button
          className={clsx("btn-ghost btn-sm", !topic && "!text-accent-400")}
          onClick={() => setTopic("")}
        >
          All
        </button>
        {data?.topics
          ?.sort((a, b) => b.count - a.count)
          .map((t) => (
            <button
              key={t.topic}
              className={clsx("btn-ghost btn-sm", topic === t.topic && "!text-accent-400")}
              onClick={() => setTopic(topic === t.topic ? "" : t.topic)}
            >
              {titleCase(t.topic)}
              <span className="num text-2xs text-ink-500 ml-1">{t.count}</span>
            </button>
          ))}
      </div>

      {!data?.items.length ? (
        <Empty title="No commentary for this topic" />
      ) : (
        <ul className="divide-y divide-ink-850">
          {data.items.map((c) => (
            <li key={c.id} className="p-5 hover:bg-ink-900/40 group">
              <div className="flex items-start justify-between gap-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="chip bg-accent-600/10 text-accent-300 border border-accent-500/30">
                    {titleCase(c.primary_topic)}
                  </span>
                  <span className={clsx("chip", SENTIMENT_STYLE[c.sentiment])}>
                    {c.sentiment}
                  </span>
                  {c.is_quote && (
                    <span className="chip bg-ink-800 text-ink-300 border border-ink-700">
                      <Quote size={9} /> quote
                    </span>
                  )}
                  {c.speaker && (
                    <span className="text-2xs text-ink-400">— {c.speaker}</span>
                  )}
                </div>
                <button
                  className="btn-ghost btn-sm opacity-0 group-hover:opacity-100 shrink-0"
                  onClick={() => onOpenSource(c.page_number, c.bbox_relative)}
                >
                  <Crosshair size={12} /> p{c.page_number}
                </button>
              </div>

              <p className="text-sm text-ink-200 mt-2.5 leading-relaxed whitespace-pre-wrap">
                {c.text}
              </p>

              {c.mentioned_metrics.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2.5">
                  {c.mentioned_metrics.map((m) => (
                    <span
                      key={m}
                      className="chip bg-ink-800 text-ink-400 border border-ink-700"
                    >
                      {titleCase(m)}
                    </span>
                  ))}
                </div>
              )}

              {c.topics.length > 1 && (
                <p className="text-2xs text-ink-500 mt-2">
                  Also touches: {c.topics.slice(1).map(titleCase).join(", ")}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
