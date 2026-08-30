import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Crosshair, Search } from "lucide-react";
import clsx from "clsx";
import { api } from "../../api/client";
import type { BBox, TableDto } from "../../types";
import { Empty, ErrorNote, Spinner } from "../../components/ui";

export function TablesTab({
  documentId, onOpenSource,
}: {
  documentId: string;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const [filter, setFilter] = useState("");
  const [activeId, setActiveId] = useState<string | null>(null);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["tables", documentId],
    queryFn: () => api.tables(documentId),
  });

  const tables = useMemo(() => {
    if (!data) return [];
    if (!filter.trim()) return data.items;
    const needle = filter.toLowerCase();
    return data.items.filter(
      (t) =>
        t.title.toLowerCase().includes(needle) ||
        (t.entity ?? "").toLowerCase().includes(needle) ||
        t.grid.some((row) => row.some((c) => c.toLowerCase().includes(needle))),
    );
  }, [data, filter]);

  const active = tables.find((t) => t.id === activeId) ?? tables[0] ?? null;

  if (isLoading) return <Spinner label="Loading reconstructed tables…" />;
  if (error)
    return (
      <div className="p-6">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );
  if (!data?.items.length)
    return (
      <Empty
        title="No tables were reconstructed"
        hint="A band of text is only recorded as a table when its column structure can actually be rebuilt — a page of prose correctly yields none."
      />
    );

  return (
    <div className="h-full flex min-h-0">
      <aside className="w-[280px] shrink-0 border-r border-ink-800 flex flex-col min-h-0">
        <div className="p-3 border-b border-ink-800 shrink-0">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-2.5 text-ink-500" />
            <input
              className="input pl-8 w-full"
              placeholder="Search tables…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
        </div>
        <ul className="flex-1 overflow-y-auto divide-y divide-ink-850 min-h-0">
          {tables.map((t) => (
            <li key={t.id}>
              <button
                className={clsx(
                  "w-full text-left px-3 py-2.5 hover:bg-ink-850 transition-colors",
                  active?.id === t.id && "bg-ink-850 border-l-2 border-accent-500",
                )}
                onClick={() => setActiveId(t.id)}
              >
                <p className="text-xs font-medium truncate">
                  {t.title || `Table on page ${t.page_number}`}
                </p>
                <div className="flex items-center gap-2 mt-1 text-2xs text-ink-500">
                  <span className="num">p{t.page_number}</span>
                  <span className="num">{t.n_rows}×{t.n_cols}</span>
                  {t.entity && <span className="truncate">{t.entity}</span>}
                  {t.continued_from_table_id && (
                    <span className="text-accent-400">cont.</span>
                  )}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <div className="flex-1 min-w-0 overflow-y-auto">
        {active && <TableDetail table={active} onOpenSource={onOpenSource} />}
      </div>
    </div>
  );
}

function TableDetail({
  table: t, onOpenSource,
}: {
  table: TableDto;
  onOpenSource: (page: number, bbox: BBox | null) => void;
}) {
  const headerRows = useMemo(() => {
    const rows = new Set<number>();
    for (const c of t.cells) {
      if (c.role === "column_header" || c.role === "corner") rows.add(c.row);
    }
    return rows;
  }, [t]);

  const cellAt = useMemo(() => {
    const m = new Map<string, (typeof t.cells)[number]>();
    for (const c of t.cells) m.set(`${c.row}:${c.col}`, c);
    return m;
  }, [t]);

  return (
    <div className="p-6">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">
            {t.title || `Table on page ${t.page_number}`}
          </h2>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-2xs text-ink-400">
            <span className="num">page {t.page_number}</span>
            <span className="num">{t.n_rows} rows × {t.n_cols} cols</span>
            <span>detector: {t.detector}</span>
            <span className="num">confidence {(t.confidence * 100).toFixed(0)}%</span>
            {t.unit_hint && <span>unit: {t.unit_hint}</span>}
            {t.currency_hint && <span>currency: {t.currency_hint}</span>}
            {t.entity && <span>entity: {t.entity}</span>}
            {t.has_ruling_lines && <span>ruled</span>}
          </div>
          {t.continued_from_table_id && (
            <p className="text-2xs text-accent-400 mt-1">
              Continues an earlier table; column semantics were inherited.
            </p>
          )}
          {t.warnings.length > 0 && (
            <ul className="mt-2 space-y-0.5">
              {t.warnings.map((w, i) => (
                <li key={i} className="text-2xs text-warn-400">{w}</li>
              ))}
            </ul>
          )}
        </div>
        <button
          className="btn-ghost btn-sm shrink-0"
          onClick={() => onOpenSource(t.page_number, t.bbox_relative)}
        >
          <Crosshair size={12} /> View on page
        </button>
      </div>

      <div className="panel overflow-x-auto">
        <table className="w-full text-sm">
          <tbody>
            {Array.from({ length: t.n_rows }, (_, r) => (
              <tr key={r} className={headerRows.has(r) ? "bg-ink-850" : ""}>
                {Array.from({ length: t.n_cols }, (_, c) => {
                  const cell = cellAt.get(`${r}:${c}`);
                  const isHeader = headerRows.has(r);
                  return (
                    <td
                      key={c}
                      title={
                        cell
                          ? `${cell.role} · confidence ${(cell.confidence * 100).toFixed(0)}%`
                          : undefined
                      }
                      className={clsx(
                        "px-3 py-1.5 border-b border-ink-850 whitespace-nowrap",
                        isHeader
                          ? "font-semibold text-ink-200 text-2xs uppercase tracking-wide"
                          : cell?.is_numeric
                            ? "num text-right"
                            : "text-ink-300",
                        cell && cell.confidence < 0.6 && "text-warn-400",
                      )}
                    >
                      {cell?.text ?? ""}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-4 panel p-4">
        <h3 className="text-xs font-semibold mb-2">Column interpretation</h3>
        <table className="w-full text-2xs">
          <thead>
            <tr className="text-ink-400">
              <th className="text-left py-1">#</th>
              <th className="text-left py-1">Header as read</th>
              <th className="text-left py-1">Resolved period</th>
              <th className="text-left py-1">Role</th>
              <th className="text-right py-1">Numeric</th>
            </tr>
          </thead>
          <tbody>
            {t.columns.map((c) => (
              <tr key={c.index} className="border-t border-ink-850">
                <td className="py-1 num text-ink-500">{c.index}</td>
                <td className="py-1 text-ink-300">{c.header || "—"}</td>
                <td className="py-1">
                  {c.normalized_period ? (
                    <span className="chip bg-ink-800 border border-ink-700 text-ink-300">
                      {c.normalized_period}
                    </span>
                  ) : (
                    <span className="text-ink-600">—</span>
                  )}
                </td>
                <td className="py-1 text-ink-400">
                  {c.is_growth_column ? `growth (${c.growth_type ?? "?"})` : "data"}
                </td>
                <td className="py-1 num text-right text-ink-400">
                  {(c.numeric_ratio * 100).toFixed(0)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
