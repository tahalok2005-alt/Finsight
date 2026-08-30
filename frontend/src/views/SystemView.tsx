import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import { api } from "../api/client";
import { ErrorNote, Panel, Spinner } from "../components/ui";
import { titleCase } from "../utils/format";

export function SystemView() {
  const system = useQuery({ queryKey: ["system"], queryFn: api.system });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 10_000,
  });
  const metrics = useQuery({ queryKey: ["metric-taxonomy"], queryFn: api.metrics });

  if (system.isLoading) return <Spinner label="Loading system information…" />;
  if (system.error)
    return (
      <div className="p-6">
        <ErrorNote error={system.error} onRetry={system.refetch} />
      </div>
    );
  const s = system.data!;

  return (
    <div className="h-full overflow-y-auto">
      <header className="px-6 py-5 border-b border-ink-800">
        <h1 className="text-lg font-semibold">System</h1>
        <p className="text-sm text-ink-400 mt-0.5">
          The engines and settings actually in force — these are recorded on every
          processing run for reproducibility
        </p>
      </header>

      <div className="p-6 grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Panel title="Health">
          <dl className="text-sm space-y-2">
            <Row
              label="API"
              value={
                <span className="flex items-center gap-1.5">
                  {health.data?.status === "ok" ? (
                    <CheckCircle2 size={13} className="text-pass-400" />
                  ) : (
                    <XCircle size={13} className="text-fail-400" />
                  )}
                  {health.data?.status ?? "…"}
                </span>
              }
            />
            <Row label="Version" value={s.app.version} />
            <Row label="Environment" value={s.app.environment} />
            <Row
              label="Database"
              value={health.data?.database?.ok ? "connected" : "unavailable"}
            />
            <Row
              label="Workers"
              value={`${health.data?.workers?.active ?? 0} active / ${health.data?.workers?.max ?? 0}`}
            />
            <Row label="Python" value={s.runtime.python} />
            <Row label="Platform" value={s.runtime.platform} />
          </dl>
        </Panel>

        <Panel title="OCR engines" subtitle={`Configured: ${s.ocr.configured}`}>
          <ul className="space-y-3">
            {Object.entries(s.ocr.engines as Record<string, any>).map(([name, info]) => (
              <li key={name} className="border-b border-ink-850 pb-3 last:border-0">
                <div className="flex items-center gap-2">
                  {info.available ? (
                    <CheckCircle2 size={13} className="text-pass-400" />
                  ) : (
                    <XCircle size={13} className="text-ink-500" />
                  )}
                  <span className="text-sm font-medium">{name}</span>
                  {name === s.ocr.configured && (
                    <span className="chip bg-accent-600/10 text-accent-300 border border-accent-500/30">
                      in use
                    </span>
                  )}
                  <span className="text-2xs text-ink-500 num ml-auto">
                    {info.version}
                  </span>
                </div>
                {info.notes && (
                  <p className="text-2xs text-ink-400 mt-1.5 leading-relaxed">
                    {info.notes}
                  </p>
                )}
                {info.error && (
                  <p className="text-2xs text-ink-500 mt-1">{info.error}</p>
                )}
              </li>
            ))}
          </ul>
          <p className="text-2xs text-ink-500 mt-3 font-mono">
            signature: {s.ocr.signature}
          </p>
        </Panel>

        <Panel title="Pipeline configuration">
          <dl className="text-sm space-y-2">
            <Row label="Render DPI" value={String(s.rendering.dpi)} />
            <Row label="Render format" value={s.rendering.format} />
            <Row label="Layout engine" value={s.layout.engine} />
            <Row
              label="Growth tolerance"
              value={`±${s.tolerances.growth_pct} pp`}
            />
            <Row
              label="Margin tolerance"
              value={`±${s.tolerances.margin_pct} pp`}
            />
            <Row
              label="Subtotal tolerance"
              value={`${(s.tolerances.total_rel * 100).toFixed(1)}%`}
            />
            <Row
              label="Confidence bands"
              value={`high ≥ ${s.confidence.high} · medium ≥ ${s.confidence.medium}`}
            />
            <Row
              label="Review threshold"
              value={`< ${s.confidence.review_threshold}`}
            />
            <Row label="Max upload" value={`${s.limits.max_upload_mb} MB`} />
            <Row label="Max pages" value={String(s.limits.max_pages)} />
          </dl>
        </Panel>

        <Panel title="Language model">
          <div className="flex items-center gap-2 mb-2">
            {s.llm.enabled ? (
              <CheckCircle2 size={14} className="text-accent-400" />
            ) : (
              <XCircle size={14} className="text-ink-500" />
            )}
            <span className="text-sm">
              {s.llm.enabled ? `${s.llm.provider} · ${s.llm.model}` : "Disabled"}
            </span>
          </div>
          <p className="text-2xs text-ink-400 leading-relaxed">{s.llm.note}</p>
        </Panel>

        <Panel
          title="Financial taxonomy"
          subtitle={`${metrics.data?.metrics.length ?? 0} canonical metrics`}
          className="lg:col-span-2"
        >
          {metrics.data && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {Object.entries(metrics.data.families).map(([family, keys]) => (
                <div key={family}>
                  <p className="text-2xs uppercase tracking-wider text-ink-400 font-semibold mb-1.5">
                    {titleCase(family)}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {keys.map((k) => (
                      <span
                        key={k}
                        className="chip bg-ink-800 text-ink-300 border border-ink-700 normal-case"
                      >
                        {k}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          <p className="text-2xs text-ink-500 mt-4 leading-relaxed">
            Near-synonyms are kept deliberately distinct. “Revenue”, “Gross
            Revenue”, “Revenue from Operations” and “Net Revenue” are related but
            different measures; collapsing them would silently corrupt any
            comparison.
          </p>
        </Panel>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex gap-3">
      <dt className="text-ink-400 w-40 shrink-0 text-xs">{label}</dt>
      <dd className="text-ink-200 text-xs break-words">{value}</dd>
    </div>
  );
}
