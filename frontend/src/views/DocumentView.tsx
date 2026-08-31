import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, RefreshCw, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { BBox } from "../types";
import { ErrorNote, Spinner, StatusChip, Tabs } from "../components/ui";
import { ProvenancePanel } from "../components/ProvenancePanel";
import { ProcessingPanel } from "./doc/ProcessingPanel";
import { FactsTab } from "./doc/FactsTab";
import { TablesTab } from "./doc/TablesTab";
import { PagesTab } from "./doc/PagesTab";
import { ChartsTab } from "./doc/ChartsTab";
import { CommentaryTab } from "./doc/CommentaryTab";
import { GuidanceTab } from "./doc/GuidanceTab";
import { ValidationTab } from "./doc/ValidationTab";
import { ReviewTab } from "./doc/ReviewTab";
import { TERMINAL_STATUSES, formatDuration } from "../utils/format";

type TabId =
  | "facts" | "tables" | "pages" | "charts" | "commentary"
  | "guidance" | "validation" | "review";

const TAB_IDS: TabId[] = [
  "facts", "tables", "pages", "charts", "commentary",
  "guidance", "validation", "review",
];

export function DocumentView() {
  const { id = "", tab } = useParams<{ id: string; tab?: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<TabId>(
    TAB_IDS.includes(tab as TabId) ? (tab as TabId) : "facts",
  );
  const [provenanceFactId, setProvenanceFactId] = useState<string | null>(null);
  const [pageFocus, setPageFocus] = useState<{ page: number; bbox: BBox | null } | null>(
    null,
  );

  const doc = useQuery({
    queryKey: ["document", id],
    queryFn: () => api.getDocument(id),
    enabled: !!id,
  });

  const isProcessing =
    doc.data && !TERMINAL_STATUSES.has(doc.data.status);

  const status = useQuery({
    queryKey: ["status", id],
    queryFn: () => api.getStatus(id),
    enabled: !!id,
    refetchInterval: isProcessing ? 1500 : false,
  });

  // once processing finishes, refresh every derived view exactly once
  useEffect(() => {
    if (status.data && TERMINAL_STATUSES.has(status.data.status)) {
      qc.invalidateQueries({ queryKey: ["document", id] });
      qc.invalidateQueries({ queryKey: ["facts", id] });
      qc.invalidateQueries({ queryKey: ["overview"] });
    }
  }, [status.data?.status, id, qc]);

  const changeTab = useCallback(
    (t: TabId) => {
      setActiveTab(t);
      navigate(`/documents/${id}/${t}`, { replace: true });
    },
    [id, navigate],
  );

  const openSource = useCallback((page: number, bbox: BBox | null) => {
    setPageFocus({ page, bbox });
    setActiveTab("pages");
  }, []);

  if (doc.isLoading) return <Spinner label="Loading document…" />;
  if (doc.error)
    return (
      <div className="p-6">
        <ErrorNote error={doc.error} onRetry={doc.refetch} />
      </div>
    );
  if (!doc.data) return null;

  const d = doc.data;
  const showProcessing = isProcessing || d.status === "FAILED";

  return (
    <div className="h-full flex">
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="px-6 py-4 border-b border-ink-800 shrink-0">
          <Link to="/" className="text-xs text-ink-400 hover:text-ink-200 inline-flex items-center gap-1">
            <ArrowLeft size={12} /> Overview
          </Link>
          <div className="flex items-start justify-between gap-4 mt-2">
            <div className="min-w-0">
              <h1 className="text-lg font-semibold truncate">
                {d.company_name ?? d.original_filename}
              </h1>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-xs text-ink-400">
                <span className="truncate max-w-[380px]">{d.original_filename}</span>
                {d.report_period && (
                  <span className="chip bg-ink-800 border border-ink-700 text-ink-300">
                    {d.report_period}
                  </span>
                )}
                {d.report_type && <span>{d.report_type.replace(/_/g, " ")}</span>}
                <span>{d.page_count} pages</span>
                {d.document_currency && d.document_unit && (
                  <span className="num">
                    {d.document_currency} · {d.document_unit}
                  </span>
                )}
                {d.is_scanned && (
                  <span className="chip bg-warn-900 text-warn-400 border border-warn-500/30">
                    scanned
                  </span>
                )}
                <span>{formatDuration(d.processing_ms)}</span>
                {status.data?.processing_mode === "VISUAL_ONLY" && (
                  <span
                    className="chip bg-accent-600/10 text-accent-300 border border-accent-500/30"
                    title="Extracted from rendered page images only; the PDF text layer was blocked from reaching extraction."
                  >
                    visual-only
                  </span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <StatusChip status={d.status} />
              <div className="relative group">
                <button className="btn-ghost btn-sm">
                  <Download size={13} /> Export
                </button>
                <div className="absolute right-0 top-full mt-1 hidden group-hover:block z-20 panel py-1 min-w-[120px]">
                  {(["json", "csv", "xlsx"] as const).map((f) => (
                    <a
                      key={f}
                      href={api.exportUrl(id, f)}
                      className="block px-3 py-1.5 text-xs hover:bg-ink-800 uppercase"
                    >
                      {f}
                    </a>
                  ))}
                </div>
              </div>
              <button
                className="btn-ghost btn-sm"
                onClick={async () => {
                  await api.reprocess(id);
                  qc.invalidateQueries({ queryKey: ["status", id] });
                  qc.invalidateQueries({ queryKey: ["document", id] });
                }}
              >
                <RefreshCw size={13} /> Reprocess
              </button>
              <button
                className="btn-ghost btn-sm text-fail-400"
                onClick={async () => {
                  if (!confirm("Delete this document and all extracted data?")) return;
                  await api.deleteDocument(id);
                  qc.invalidateQueries({ queryKey: ["overview"] });
                  navigate("/");
                }}
              >
                <Trash2 size={13} />
              </button>
            </div>
          </div>
          {d.error && (
            <p className="mt-3 text-xs text-fail-400 bg-fail-900/30 border border-fail-500/30 rounded px-3 py-2">
              {d.error}
            </p>
          )}
        </header>

        {showProcessing && status.data && (
          <div className="px-6 py-4 border-b border-ink-800 shrink-0">
            <ProcessingPanel status={status.data} />
          </div>
        )}

        <div className="px-6 pt-3 shrink-0">
          <Tabs
            active={activeTab}
            onChange={changeTab}
            tabs={[
              { id: "facts", label: "Financials", count: d.fact_count },
              { id: "tables", label: "Tables", count: d.table_count },
              { id: "pages", label: "Pages", count: d.page_count },
              { id: "charts", label: "Charts", count: d.chart_count },
              { id: "commentary", label: "Commentary", count: d.commentary_count },
              { id: "guidance", label: "Guidance", count: d.guidance_count },
              {
                id: "validation",
                label: "Validation",
                count: d.validation_warning_count,
              },
              { id: "review", label: "Review", count: d.review_queue_count },
            ]}
          />
        </div>

        <div className="flex-1 min-h-0 overflow-hidden">
          {activeTab === "facts" && (
            <FactsTab
              documentId={id}
              onShowProvenance={setProvenanceFactId}
              onOpenSource={openSource}
            />
          )}
          {activeTab === "tables" && (
            <TablesTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "pages" && (
            <PagesTab
              documentId={id}
              pageCount={d.page_count}
              focus={pageFocus}
              onFocusHandled={() => setPageFocus(null)}
            />
          )}
          {activeTab === "charts" && (
            <ChartsTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "commentary" && (
            <CommentaryTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "guidance" && (
            <GuidanceTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "validation" && (
            <ValidationTab documentId={id} onShowProvenance={setProvenanceFactId} />
          )}
          {activeTab === "review" && (
            <ReviewTab documentId={id} onShowProvenance={setProvenanceFactId} />
          )}
        </div>
      </div>

      {provenanceFactId && (
        <ProvenancePanel
          documentId={id}
          factId={provenanceFactId}
          onClose={() => setProvenanceFactId(null)}
          onOpenPage={openSource}
        />
      )}
    </div>
  );
}
