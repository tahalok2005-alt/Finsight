import { useCallback, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertTriangle, CheckCircle2, FileUp, Loader2, X } from "lucide-react";
import clsx from "clsx";
import { ApiError, api } from "../api/client";
import { Panel, ProgressBar } from "../components/ui";
import { formatBytes } from "../utils/format";

type Phase = "idle" | "uploading" | "validating" | "queued" | "error";

interface Job {
  file: File;
  phase: Phase;
  progress: number;
  documentId?: string;
  error?: string;
  detail?: Record<string, unknown>;
  pages?: number;
  warnings?: string[];
}

const MAX_MB = 200;

export function UploadView() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const update = useCallback((file: File, patch: Partial<Job>) => {
    setJobs((prev) =>
      prev.map((j) => (j.file === file ? { ...j, ...patch } : j)),
    );
  }, []);

  const send = useCallback(
    async (file: File) => {
      // client-side pre-checks mirror the server's, so obvious problems are
      // reported instantly rather than after a round trip
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        update(file, { phase: "error", error: "Only PDF files are supported." });
        return;
      }
      if (file.size === 0) {
        update(file, { phase: "error", error: "The file is empty." });
        return;
      }
      if (file.size > MAX_MB * 1024 * 1024) {
        update(file, {
          phase: "error",
          error: `The file is larger than the ${MAX_MB} MB limit.`,
        });
        return;
      }

      update(file, { phase: "uploading", progress: 0 });
      try {
        const doc = await api.upload(file, (pct) => {
          update(file, {
            progress: pct,
            phase: pct >= 100 ? "validating" : "uploading",
          });
        });
        update(file, {
          phase: "queued",
          progress: 100,
          documentId: doc.id,
          pages: doc.page_count,
          warnings: (doc as any).warnings ?? [],
        });
      } catch (err) {
        const apiErr = err as ApiError;
        update(file, {
          phase: "error",
          error: apiErr.message ?? "Upload failed",
          detail: apiErr.detail,
        });
      }
    },
    [update],
  );

  const accept = useCallback(
    (files: FileList | null) => {
      if (!files?.length) return;
      const added = Array.from(files).map<Job>((file) => ({
        file, phase: "idle", progress: 0,
      }));
      setJobs((prev) => [...added, ...prev]);
      added.forEach((j) => void send(j.file));
    },
    [send],
  );

  return (
    <div className="h-full overflow-y-auto">
      <header className="px-6 py-5 border-b border-ink-800">
        <h1 className="text-lg font-semibold">Upload a report</h1>
        <p className="text-sm text-ink-400 mt-0.5">
          Earnings releases, quarterly results, annual reports — digital or scanned
        </p>
      </header>

      <div className="p-6 max-w-3xl space-y-5">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            accept(e.dataTransfer.files);
          }}
          onClick={() => inputRef.current?.click()}
          className={clsx(
            "border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-colors",
            dragging
              ? "border-accent-500 bg-accent-600/10"
              : "border-ink-700 hover:border-ink-600 bg-ink-900/50",
          )}
        >
          <FileUp
            size={30}
            className={clsx("mx-auto mb-3", dragging ? "text-accent-400" : "text-ink-500")}
          />
          <p className="text-sm font-medium">
            Drop PDF files here, or click to choose
          </p>
          <p className="text-xs text-ink-500 mt-1.5">
            PDF only · up to {MAX_MB} MB · scanned documents are supported
          </p>
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            multiple
            className="hidden"
            onChange={(e) => {
              accept(e.target.files);
              e.target.value = "";
            }}
          />
        </div>

        {jobs.length > 0 && (
          <Panel title="Uploads" bodyClassName="p-0">
            <ul className="divide-y divide-ink-850">
              {jobs.map((job, i) => (
                <li key={`${job.file.name}-${i}`} className="p-4">
                  <div className="flex items-start gap-3">
                    <div className="mt-0.5 shrink-0">
                      {job.phase === "queued" && (
                        <CheckCircle2 size={16} className="text-pass-400" />
                      )}
                      {job.phase === "error" && (
                        <AlertTriangle size={16} className="text-fail-400" />
                      )}
                      {(job.phase === "uploading" || job.phase === "validating") && (
                        <Loader2 size={16} className="text-accent-400 animate-spin" />
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <p className="text-sm font-medium truncate">{job.file.name}</p>
                        <span className="text-2xs text-ink-500 shrink-0">
                          {formatBytes(job.file.size)}
                        </span>
                      </div>

                      {job.phase === "uploading" && (
                        <>
                          <ProgressBar value={job.progress / 100} className="mt-2" />
                          <p className="text-2xs text-ink-500 mt-1">
                            Uploading… {job.progress}%
                          </p>
                        </>
                      )}
                      {job.phase === "validating" && (
                        <p className="text-2xs text-ink-400 mt-1.5">
                          Validating the PDF structure…
                        </p>
                      )}
                      {job.phase === "queued" && (
                        <div className="mt-1.5">
                          <p className="text-2xs text-pass-400">
                            Accepted · {job.pages} page{job.pages === 1 ? "" : "s"} ·
                            queued for processing
                          </p>
                          {job.warnings && job.warnings.length > 0 && (
                            <ul className="mt-1 space-y-0.5">
                              {job.warnings.slice(0, 3).map((w, k) => (
                                <li key={k} className="text-2xs text-warn-400">
                                  {w}
                                </li>
                              ))}
                            </ul>
                          )}
                          <button
                            className="btn-primary btn-sm mt-2"
                            onClick={() => navigate(`/documents/${job.documentId}`)}
                          >
                            Track processing
                          </button>
                        </div>
                      )}
                      {job.phase === "error" && (
                        <div className="mt-1.5">
                          <p className="text-2xs text-fail-400">{job.error}</p>
                          {typeof job.detail?.document_id === "string" && (
                            <button
                              className="btn-ghost btn-sm mt-2"
                              onClick={() =>
                                navigate(`/documents/${job.detail!.document_id}`)
                              }
                            >
                              Open the existing document
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                    <button
                      className="text-ink-500 hover:text-ink-300 shrink-0"
                      onClick={() => setJobs((p) => p.filter((_, k) => k !== i))}
                      aria-label="Dismiss"
                    >
                      <X size={14} />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        )}

        <Panel title="What happens next">
          <ol className="text-sm text-ink-300 space-y-2 list-decimal list-inside">
            <li>The PDF is validated — a corrupt or encrypted file is rejected immediately.</li>
            <li>Every page is rendered to an image at 200 dpi; the image is the canonical representation.</li>
            <li>Local OCR reads the pixels and keeps a bounding box for every token.</li>
            <li>Layout analysis finds tables, charts, headings and KPI blocks from geometry and ruling lines.</li>
            <li>Tables are structurally reconstructed, then mapped to a canonical financial taxonomy.</li>
            <li>Units, currencies and fiscal periods are normalised without discarding what the document said.</li>
            <li>Every figure is arithmetically validated and given a confidence score with full provenance.</li>
          </ol>
          <p className="text-2xs text-ink-500 mt-4 leading-relaxed">
            Processing runs on local models only. No document content is sent to
            any external service.
          </p>
        </Panel>
      </div>
    </div>
  );
}
