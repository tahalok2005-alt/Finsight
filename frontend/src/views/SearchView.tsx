import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Search } from "lucide-react";
import { api } from "../api/client";
import { Empty, ErrorNote, Panel, Spinner } from "../components/ui";
import { formatFull, titleCase } from "../utils/format";

const EXAMPLES = ["Revenue", "EBITDA", "Q1 FY27", "capex", "net debt", "guidance"];

export function SearchView() {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["search", query],
    queryFn: () => api.search(query),
    enabled: query.length >= 2,
  });

  return (
    <div className="h-full overflow-y-auto">
      <header className="px-6 py-5 border-b border-ink-800">
        <h1 className="text-lg font-semibold">Search</h1>
        <p className="text-sm text-ink-400 mt-0.5">
          Across documents, financial facts, table cells, commentary and guidance
        </p>
      </header>

      <div className="p-6 space-y-5">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setQuery(input.trim());
          }}
          className="flex gap-2"
        >
          <div className="relative flex-1 max-w-xl">
            <Search size={15} className="absolute left-3 top-2.5 text-ink-500" />
            <input
              className="input pl-9 w-full"
              placeholder="Search metrics, periods, table cells, commentary…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              autoFocus
            />
          </div>
          <button className="btn-primary" type="submit" disabled={input.trim().length < 2}>
            Search
          </button>
        </form>

        <div className="flex flex-wrap gap-1.5">
          <span className="text-xs text-ink-500 mr-1">Try:</span>
          {EXAMPLES.map((e) => (
            <button
              key={e}
              className="btn-ghost btn-sm"
              onClick={() => {
                setInput(e);
                setQuery(e);
              }}
            >
              {e}
            </button>
          ))}
        </div>

        {isLoading && <Spinner label="Searching…" />}
        {error && <ErrorNote error={error} onRetry={refetch} />}

        {data && data.total === 0 && (
          <Empty title={`Nothing matched “${data.query}”`} />
        )}

        {data && data.total > 0 && (
          <div className="space-y-5">
            <p className="text-xs text-ink-400 num">{data.total} results</p>

            {data.results.documents?.length > 0 && (
              <Panel title="Documents" bodyClassName="p-0">
                <ul className="divide-y divide-ink-850">
                  {data.results.documents.map((d: any) => (
                    <li key={d.id} className="px-4 py-3">
                      <Link to={`/documents/${d.id}`} className="link font-medium text-sm">
                        {d.company_name ?? d.filename}
                      </Link>
                      <p className="text-2xs text-ink-500 mt-0.5">
                        {d.report_period ?? "—"} · {d.status}
                      </p>
                    </li>
                  ))}
                </ul>
              </Panel>
            )}

            {data.results.facts?.length > 0 && (
              <Panel title="Financial facts" bodyClassName="p-0">
                <table className="w-full">
                  <thead>
                    <tr>
                      <th className="th">Metric</th>
                      <th className="th">Entity</th>
                      <th className="th">Period</th>
                      <th className="th text-right">Value</th>
                      <th className="th">Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.results.facts.map((f: any) => (
                      <tr key={f.fact_id} className="hover:bg-ink-850/60">
                        <td className="td">{titleCase(f.canonical_metric)}</td>
                        <td className="td text-xs text-ink-400">{f.entity ?? "—"}</td>
                        <td className="td text-xs">{f.period ?? "—"}</td>
                        <td className="td text-right num">
                          {formatFull(f.value, f.currency, f.unit, null)}
                        </td>
                        <td className="td">
                          <Link
                            to={`/documents/${f.document_id}/facts`}
                            className="link text-2xs"
                          >
                            page {f.source_page}
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            )}

            {data.results.table_cells?.length > 0 && (
              <Panel title="Table cells" bodyClassName="p-0">
                <ul className="divide-y divide-ink-850">
                  {data.results.table_cells.map((c: any) => (
                    <li key={c.id} className="px-4 py-2.5 flex items-center gap-3">
                      <span className="text-sm flex-1 truncate">{c.text}</span>
                      <span className="text-2xs text-ink-500 num shrink-0">
                        r{c.row} c{c.col}
                      </span>
                      <Link
                        to={`/documents/${c.document_id}/tables`}
                        className="link text-2xs shrink-0"
                      >
                        page {c.page_number}
                      </Link>
                    </li>
                  ))}
                </ul>
              </Panel>
            )}

            {data.results.commentary?.length > 0 && (
              <Panel title="Commentary" bodyClassName="p-0">
                <ul className="divide-y divide-ink-850">
                  {data.results.commentary.map((c: any) => (
                    <li key={c.id} className="px-4 py-3">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="chip bg-accent-600/10 text-accent-300 border border-accent-500/30">
                          {titleCase(c.primary_topic)}
                        </span>
                        <Link
                          to={`/documents/${c.document_id}/commentary`}
                          className="link text-2xs ml-auto"
                        >
                          page {c.page_number}
                        </Link>
                      </div>
                      <p className="text-sm text-ink-300">{c.text}</p>
                    </li>
                  ))}
                </ul>
              </Panel>
            )}

            {data.results.guidance?.length > 0 && (
              <Panel title="Guidance" bodyClassName="p-0">
                <ul className="divide-y divide-ink-850">
                  {data.results.guidance.map((g: any) => (
                    <li key={g.id} className="px-4 py-3">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="chip bg-warn-900 text-warn-400 border border-warn-500/30">
                          {g.nature}
                        </span>
                        <Link
                          to={`/documents/${g.document_id}/guidance`}
                          className="link text-2xs ml-auto"
                        >
                          page {g.page_number}
                        </Link>
                      </div>
                      <p className="text-sm text-ink-300">{g.text}</p>
                    </li>
                  ))}
                </ul>
              </Panel>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
