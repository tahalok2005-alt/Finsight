import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { BBox, DocumentSummary } from "../types";
import { ErrorNote, Spinner, StatusChip, SubNav, Tabs } from "../components/ui";
import { ProvenancePanel } from "../components/ProvenancePanel";
import { ProcessingPanel } from "./doc/ProcessingPanel";
import { FactsTab } from "./doc/FactsTab";
import { InsightsTab } from "./doc/InsightsTab";
import { SummaryTab } from "./doc/SummaryTab";
import { DocumentBrief } from "./doc/DocumentBrief";
import { ExplanationPanel } from "../components/ExplanationPanel";
import { ExportMenu } from "../components/ExportMenu";
import { TablesTab } from "./doc/TablesTab";
import { PagesTab } from "./doc/PagesTab";
import { ChartsTab } from "./doc/ChartsTab";
import { CommentaryTab } from "./doc/CommentaryTab";
import { GuidanceTab } from "./doc/GuidanceTab";
import { ValidationTab } from "./doc/ValidationTab";
import { ReviewTab } from "./doc/ReviewTab";
import { TERMINAL_STATUSES } from "../utils/format";
import { statusMeta } from "../utils/lifecycle";

// Four places, not eleven.
//
// The screen used to open with eleven tabs in one row -- overview, statements,
// financials, insights, tables, pages, charts, commentary, guidance,
// validation, review -- which made the reader choose before they knew what was
// in the document. The same material now sits in four: what the document is,
// the figures, the statements behind them, and the source pages. Everything
// finer is a section inside one of those, reachable once you are already
// there.
type TabId = "overview" | "figures" | "statements" | "source";
const TAB_IDS: TabId[] = ["overview", "figures", "statements", "source"];

// The old tab names stay valid as links: bookmarks, the cards on the overview
// and anything else that still says "/documents/x/validation" lands in the
// right section rather than on a 404 or a silent redirect to the top.
const LEGACY: Record<string, { tab: TabId; section: string }> = {
  brief: { tab: "overview", section: "summary" },
  insights: { tab: "overview", section: "insights" },
  facts: { tab: "figures", section: "all" },
  review: { tab: "figures", section: "review" },
  summary: { tab: "statements", section: "statements" },
  tables: { tab: "statements", section: "tables" },
  validation: { tab: "statements", section: "checks" },
  pages: { tab: "source", section: "pages" },
  charts: { tab: "source", section: "charts" },
  commentary: { tab: "source", section: "commentary" },
  guidance: { tab: "source", section: "guidance" },
};

const DEFAULT_SECTION: Record<TabId, string> = {
  overview: "summary",
  figures: "all",
  statements: "statements",
  source: "pages",
};

// What sits inside each tab. A section with nothing in it is not offered:
// a document with no charts should not be asked whether it wants to see them.
const SECTIONS: Record<
  TabId,
  { id: string; label: string; count?: (d: DocumentSummary) => number | undefined }[]
> = {
  overview: [
    { id: "summary", label: "Summary" },
    { id: "insights", label: "Insights" },
  ],
  figures: [
    { id: "all", label: "All figures", count: (d) => d.fact_count },
    { id: "review", label: "Needs review", count: (d) => d.review_queue_count },
  ],
  statements: [
    { id: "statements", label: "Statements" },
    { id: "tables", label: "Tables", count: (d) => d.table_count },
    { id: "checks", label: "Checks", count: (d) => d.validation_warning_count },
  ],
  source: [
    { id: "pages", label: "Pages", count: (d) => d.page_count },
    { id: "charts", label: "Charts", count: (d) => d.chart_count },
    { id: "commentary", label: "Commentary", count: (d) => d.commentary_count },
    { id: "guidance", label: "Guidance", count: (d) => d.guidance_count },
  ],
};

function resolveTab(raw: string | undefined): { tab: TabId; section: string } {
  if (raw && TAB_IDS.includes(raw as TabId))
    return { tab: raw as TabId, section: DEFAULT_SECTION[raw as TabId] };
  if (raw && LEGACY[raw]) return LEGACY[raw];
  return { tab: "overview", section: "summary" };
}

export function DocumentView() {
  const { id = "", tab } = useParams<{ id: string; tab?: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const initial = resolveTab(tab);
  const [activeTab, setActiveTab] = useState<TabId>(initial.tab);
  const [section, setSection] = useState<string>(initial.section);
  const [provenanceFactId, setProvenanceFactId] = useState<string | null>(null);
  const [explainFactId, setExplainFactId] = useState<string | null>(null);
  const [pageFocus, setPageFocus] = useState<{ page: number; bbox: BBox | null } | null>(
    null,
  );

  const doc = useQuery({
    queryKey: ["document", id],
    queryFn: () => api.getDocument(id),
    enabled: !!id,
    // The document row carries the status *and* the control flags, so it has
    // to be refetched alongside the stage records.  Polling only the status
    // endpoint left the header and the pause/resume buttons showing the state
    // the page was opened with, which is worse than not polling at all.
    refetchInterval: (query) =>
      query.state.data && !TERMINAL_STATUSES.has(query.state.data.status)
        ? 2000 : false,
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

  // One way in and out of every part of the document, so a card on the
  // overview, a link in a panel and the tab bar itself all behave the same.
  const go = useCallback(
    (t: TabId, sec?: string) => {
      setActiveTab(t);
      setSection(sec ?? DEFAULT_SECTION[t]);
      navigate(`/documents/${id}/${t}`, { replace: true });
    },
    [id, navigate],
  );

  const goLegacy = useCallback(
    (raw: string) => {
      const r = resolveTab(raw);
      go(r.tab, r.section);
    },
    [go],
  );

  const openSource = useCallback((page: number, bbox: BBox | null) => {
    setPageFocus({ page, bbox });
    setActiveTab("source");
    setSection("pages");
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
  const phase = statusMeta(d.status).phase;
  // A document that has not produced results yet gets the processing view in
  // full, not a strip above ten empty tabs.  Empty tabs answer the wrong
  // question: the user wants to know what is happening, not to browse nothing.
  const showProcessing = phase !== "done";

  // A section the document has nothing for is not offered at all -- an empty
  // "Charts 0" button is a dead end dressed as a choice. The tab's own default
  // always stays, so there is never a tab with no sections under it.
  const sections = SECTIONS[activeTab]
    .map((it) => ({ ...it, count: it.count ? it.count(d) : undefined }))
    .filter((it) => it.id === DEFAULT_SECTION[activeTab] || (it.count ?? 1) > 0);
  const shownSection = sections.some((it) => it.id === section)
    ? section
    : DEFAULT_SECTION[activeTab];

  return (
    <div className="h-full flex">
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="px-6 py-4 border-b border-ink-800 shrink-0">
          <Link to="/documents"
                className="text-xs text-ink-300 hover:text-ink-100 inline-flex items-center gap-1">
            <ArrowLeft size={12} /> Documents
          </Link>
          <div className="flex items-start justify-between gap-4 mt-2">
            <div className="min-w-0">
              <h1 className="text-lg font-semibold truncate">
                {d.company_name || d.original_filename}
              </h1>
              {/* The header carries identity only.  Period, currency, unit,
                  duration, source and mode all live in the Overview tab; a
                  second copy here was the "redundant metadata" that made this
                  bar noisy without making anything clearer. */}
              <div className="flex items-center gap-2 mt-1 text-xs text-ink-300 min-w-0">
                <span className="truncate">{d.original_filename}</span>
                <span className="text-ink-600">·</span>
                <span className="num shrink-0">{d.page_count} pages</span>
                {status.data?.processing_mode === "VISUAL_ONLY" ? (
                  <span
                    className="chip-neutral shrink-0"
                    title="Extracted from rendered page images only; the PDF text layer was blocked from reaching extraction."
                  >
                    visual-only
                  </span>
                ) : null}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <StatusChip status={d.status} />
              <ExportMenu documentId={id} />
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

        {showProcessing ? (
          <div className="flex-1 min-h-0 overflow-auto">
            <ProcessingPanel doc={d} status={status.data} />
          </div>
        ) : (
        <>
        <div className="px-6 pt-4 shrink-0">
          <Tabs
            active={activeTab}
            onChange={(t) => go(t)}
            tabs={[
              { id: "overview", label: "Overview" },
              { id: "figures", label: "Figures", count: d.fact_count },
              { id: "statements", label: "Statements", count: d.table_count },
              { id: "source", label: "Source", count: d.page_count },
            ]}
          />
        </div>

        {sections.length > 1 && (
          <div className="px-6 pt-4 shrink-0">
            <SubNav items={sections} active={shownSection} onChange={setSection} />
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-hidden pt-2">
          {activeTab === "overview" && shownSection === "summary" && (
            <DocumentBrief doc={d} status={status.data} onNavigate={goLegacy} />
          )}
          {activeTab === "overview" && shownSection === "insights" && (
            <InsightsTab
              documentId={id}
              onExplain={setExplainFactId}
              onOpenPage={(page) => openSource(page, null)}
            />
          )}

          {activeTab === "figures" && shownSection === "all" && (
            <FactsTab
              documentId={id}
              onShowProvenance={setProvenanceFactId}
              onOpenSource={openSource}
            />
          )}
          {activeTab === "figures" && shownSection === "review" && (
            <ReviewTab documentId={id} onShowProvenance={setProvenanceFactId} />
          )}

          {activeTab === "statements" && shownSection === "statements" && (
            <SummaryTab documentId={id} onExplain={setExplainFactId} />
          )}
          {activeTab === "statements" && shownSection === "tables" && (
            <TablesTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "statements" && shownSection === "checks" && (
            <ValidationTab documentId={id} onShowProvenance={setProvenanceFactId} />
          )}

          {activeTab === "source" && shownSection === "pages" && (
            <PagesTab
              documentId={id}
              pageCount={d.page_count}
              focus={pageFocus}
              onFocusHandled={() => setPageFocus(null)}
            />
          )}
          {activeTab === "source" && shownSection === "charts" && (
            <ChartsTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "source" && shownSection === "commentary" && (
            <CommentaryTab documentId={id} onOpenSource={openSource} />
          )}
          {activeTab === "source" && shownSection === "guidance" && (
            <GuidanceTab documentId={id} onOpenSource={openSource} />
          )}
        </div>
        </>
        )}
      </div>

      {explainFactId && (
        <ExplanationPanel
          documentId={id}
          factId={explainFactId}
          onClose={() => setExplainFactId(null)}
          onOpenPage={(page) => openSource(page, null)}
        />
      )}

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
