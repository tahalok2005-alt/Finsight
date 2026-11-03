import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowRight, ClipboardCheck, FileText, Layers, Lightbulb,
  Sigma, Table2,
} from "lucide-react";
import type {
  DocumentSummary, DocumentUnderstanding, ProcessingStatus,
} from "../../types";
import { api } from "../../api/client";
import { dateTime, duration, statusMeta } from "../../utils/lifecycle";

/**
 * The brief that opens a finished document.
 *
 * It answers three questions in order, because that is the order a person asks
 * them: what is this document, what did the system do to it, and what should I
 * do next. Every figure shown is attached to somewhere you can go -- a count
 * with no destination is a decoration, and this screen used to be full of them.
 */

type Props = {
  doc: DocumentSummary;
  status?: ProcessingStatus;
  onNavigate: (tab: string) => void;
};

function Field({ label, value, title }:
  { label: string; value: React.ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="label">{label}</dt>
      <dd className="text-xs text-ink-100 mt-1 truncate" title={title}>{value}</dd>
    </div>
  );
}

/**
 * What FinSight actually understood, and where it did not.
 *
 * Deliberately not a dashboard. Each line is a gap a reader would otherwise
 * have to discover by scrolling the figures table, and each is phrased as the
 * shortfall rather than as a score -- "18 have no reporting period" is
 * actionable in a way that "83 % period coverage" is not.
 *
 * A line is shown only when it has something to say. A document with no
 * contradictions does not need a row reading zero, and an earlier version of
 * this screen was full of counts that led nowhere.
 */
function Understanding({ u, onNavigate }: {
  u: DocumentUnderstanding;
  onNavigate: (tab: string) => void;
}) {
  const total = u.figures_extracted;
  if (!total) return null;

  const gaps: { text: string; tone: "warn" | "plain"; tab?: string }[] = [];
  if (u.contradictions > 0) {
    gaps.push({
      text: `${u.contradictions} figure${u.contradictions === 1 ? "" : "s"} `
        + "the document contradicts elsewhere",
      tone: "warn", tab: "review",
    });
  }
  if (u.figures_corroborated > 0) {
    gaps.push({
      text: `${u.figures_corroborated} confirmed by a second reading`,
      tone: "plain", tab: "facts",
    });
  }
  if (u.figures_without_a_period > 0) {
    gaps.push({
      text: `${u.figures_without_a_period} carry no reporting period`,
      tone: "plain", tab: "facts",
    });
  }
  if (u.figures_without_an_entity > 0) {
    gaps.push({
      text: `${u.figures_without_an_entity} are not attributed to an entity`,
      tone: "plain", tab: "facts",
    });
  }
  if (u.charts_detected > 0) {
    gaps.push({
      text: u.charts_analytically_readable > 0
        ? `${u.charts_analytically_readable} of ${u.charts_detected} charts `
          + "carry printed values that can be read"
        : `${u.charts_detected} charts found, none with printed values that `
          + "can be read safely",
      tone: "plain", tab: "charts",
    });
  }

  return (
    <section className="panel overflow-hidden">
      <div className="panel-head">
        <span className="panel-title">What FinSight understood</span>
        <span className="text-2xs text-ink-400">
          {u.pages_processed} pages · {u.tables_understood} tables read
        </span>
      </div>
      <ul className="px-4 py-3 space-y-1.5">
        {gaps.map((g, i) => (
          <li key={i}>
            <button
              className={`text-xs text-left hover:underline ${
                g.tone === "warn" ? "text-warn-400" : "text-ink-300"}`}
              onClick={() => g.tab && onNavigate(g.tab)}
            >
              {g.text}
            </button>
          </li>
        ))}
      </ul>
      <p className="px-4 pb-3 text-2xs text-ink-500">
        A figure with no period or no entity is still a figure that was read
        correctly; what is missing is the context around it, and it is reported
        here rather than filled in.
      </p>
    </section>
  );
}


function Destination({
  icon: Icon, label, count, hint, tone = "default", onClick,
}: {
  icon: typeof Sigma; label: string; count?: number | null; hint: string;
  tone?: "default" | "warn"; onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="group flex items-center gap-3 w-full text-left px-3.5 h-14
                 border-b border-ink-800 last:border-b-0 hover:bg-ink-850
                 transition-colors"
    >
      <span className={`h-7 w-7 rounded flex items-center justify-center shrink-0
        ${tone === "warn" ? "bg-warn-900 text-warn-400" : "bg-ink-800 text-ink-300"}`}>
        <Icon size={14} />
      </span>
      <span className="flex-1 min-w-0">
        <span className="flex items-baseline gap-2">
          <span className="text-xs font-medium text-ink-100">{label}</span>
          {count !== null && count !== undefined ? (
            <span className={`num text-xs ${tone === "warn"
              ? "text-warn-400 font-semibold" : "text-ink-300"}`}>
              {count.toLocaleString()}
            </span>
          ) : null}
        </span>
        <span className="block text-2xs text-ink-400 truncate mt-0.5">{hint}</span>
      </span>
      <ArrowRight size={13}
        className="text-ink-600 group-hover:text-ink-200 shrink-0 transition-colors" />
    </button>
  );
}

export function DocumentBrief({ doc, status, onNavigate }: Props) {
  // Loaded separately from the summary: the counts come from the run's
  // own diagnostics, which the document row does not carry.
  const understanding = useQuery({
    queryKey: ["diagnostics", doc.id],
    queryFn: () => api.diagnostics(doc.id),
  });
  const meta = statusMeta(doc.status);
  const review = doc.review_queue_count ?? 0;
  const warnings = doc.validation_warning_count ?? 0;
  const visualOnly = status?.processing_mode === "VISUAL_ONLY";
  const pagesFailed = doc.pages_failed ?? 0;

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-5xl mx-auto p-5 space-y-4">

        {/* ------------------------------------------- what is this document */}
        <section className="panel">
          <div className="panel-head">
            <span className="panel-title">Document</span>
            <span className={meta.chip} title={meta.hint}>{meta.label}</span>
          </div>
          <div className="px-4 py-3.5">
            <h2 className="text-base font-semibold text-ink-50 leading-snug break-words">
              {doc.company_name || doc.original_filename}
            </h2>
            {doc.company_name ? (
              <p className="text-2xs text-ink-400 mt-0.5 break-all">
                {doc.original_filename}
              </p>
            ) : null}

            <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-3.5 mt-4">
              <Field label="Pages" value={
                <span className="num">
                  {doc.page_count ?? "—"}
                  {pagesFailed > 0 ? (
                    <span className="text-warn-400"> · {pagesFailed} failed</span>
                  ) : null}
                </span>} />
              <Field label="Period" value={doc.report_period ?? "Not stated"} />
              <Field label="Currency / unit"
                     value={<span className="num">
                       {doc.document_currency ?? "—"}
                       {doc.document_unit ? ` · ${doc.document_unit}` : ""}
                     </span>} />
              <Field label="Uploaded" value={dateTime(doc.created_at)}
                     title={doc.created_at} />
              <Field label="Processing mode"
                     value={visualOnly ? "Visual only" : status?.processing_mode ?? "Hybrid"}
                     title={visualOnly
                       ? "Extracted from rendered page images only; the PDF text layer was blocked from reaching extraction."
                       : "Pixels first, with the PDF text layer used only to corroborate."} />
              <Field label="Source"
                     value={doc.is_scanned ? "Scanned / image" : "Digital PDF"} />
              <Field label="Duration" value={duration(doc.processing_ms)} />
              <Field label="Size" value={doc.size_bytes
                ? `${(doc.size_bytes / 1_048_576).toFixed(1)} MB` : "—"} />
            </dl>
          </div>
        </section>

        {/* --------------------------------------------- what needs a person */}
        {review > 0 || warnings > 0 || pagesFailed > 0 ? (
          <section className="panel border-warn-500/30">
            <div className="px-4 py-3 flex items-start gap-3">
              <AlertTriangle size={15} className="text-warn-400 mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-ink-50">
                  {review > 0
                    ? `${review} figure${review === 1 ? "" : "s"} need confirming`
                    : warnings > 0
                      ? `${warnings} validation warning${warnings === 1 ? "" : "s"}`
                      : `${pagesFailed} page${pagesFailed === 1 ? "" : "s"} could not be processed`}
                </p>
                <p className="text-2xs text-ink-300 mt-1 leading-relaxed">
                  {review > 0
                    ? "These were extracted with low confidence or failed an arithmetic check. Confirming or correcting them takes a few seconds each."
                    : warnings > 0
                      ? "Reported figures that did not reconcile with the system's own recomputation. Nothing was silently corrected."
                      : "Their figures are missing from the results. The rest of the document processed normally."}
                </p>
              </div>
              <button className="btn-ghost shrink-0"
                      onClick={() => onNavigate(review > 0 ? "review" : "validation")}>
                {review > 0 ? "Open review queue" : "See warnings"}
              </button>
            </div>
          </section>
        ) : null}

        {understanding.data?.understanding ? (
          <Understanding
            u={understanding.data.understanding}
            onNavigate={onNavigate}
          />
        ) : null}

        {/* ------------------------------------------------ where the results are */}
        <section className="panel overflow-hidden">
          <div className="panel-head">
            <span className="panel-title">Results</span>
            <span className="text-2xs text-ink-400">
              {meta.phase === "done" ? "Extraction complete" : meta.label}
            </span>
          </div>
          <Destination
            icon={Sigma} label="Financial figures" count={doc.fact_count}
            hint="Every extracted value, with its period, unit and source"
            onClick={() => onNavigate("facts")}
          />
          <Destination
            icon={Table2} label="Tables" count={doc.table_count}
            hint="Reconstructed statement tables, cell by cell"
            onClick={() => onNavigate("tables")}
          />
          <Destination
            icon={Lightbulb} label="Insights and anomalies" count={null}
            hint="Derived metrics, relationships and figures that stand out"
            onClick={() => onNavigate("insights")}
          />
          <Destination
            icon={ClipboardCheck} label="Validation" count={warnings || null}
            hint="Arithmetic and consistency checks the figures were put through"
            tone={warnings > 0 ? "warn" : "default"}
            onClick={() => onNavigate("validation")}
          />
          <Destination
            icon={Layers} label="Review queue" count={review || null}
            hint="Figures awaiting a person's confirmation"
            tone={review > 0 ? "warn" : "default"}
            onClick={() => onNavigate("review")}
          />
          <Destination
            icon={FileText} label="Source pages" count={doc.page_count}
            hint="The rendered document, with every figure traceable to its pixels"
            onClick={() => onNavigate("pages")}
          />
        </section>

        <p className="text-2xs text-ink-500 text-center pb-2">
          Every figure links back to the exact region of the page it came from.
          {" "}
          <Link to="/documents" className="text-ink-400 hover:text-ink-200 underline">
            All documents
          </Link>
        </p>
      </div>
    </div>
  );
}
