import type {
  DocumentDiagnostics,
  AnomalyDto, ChartDto, CommentaryDto, ComparisonRow, DerivedMetricDto,
  ControlAck, DocumentControl, DocumentSummary, EntityDto, EntityNode,
  FactExplanationDto, FinancialFact,
  GuidanceDto, InsightDto, Overview, PageDetail, PageInfo, Paged,
  ProcessingStatus, RelationDto, ReviewItem, RiskDto, StatementGroup, TableDto,
  TableSemanticsDto, ValidationDto,
} from "../types";

export const API = "/api/v1";

/** Sent with every change. A public instance refuses changes without it: a
 *  page on another site cannot add a custom header to a cross-site request,
 *  so this is what tells the server a request came from this dashboard. */
const REQUEST_HEADER = { "X-FinSight-Request": "1" };

export class ApiError extends Error {
  status: number;
  code: string;
  detail?: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(init?.method && init.method !== "GET" ? REQUEST_HEADER : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    let code = "http_error";
    let message = `${res.status} ${res.statusText}`;
    let detail: Record<string, unknown> | undefined;
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        detail = body.error.detail;
      }
    } catch {
      /* the response had no JSON body; keep the status text */
    }
    throw new ApiError(res.status, code, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const qs = (params: Record<string, unknown>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

export interface FactQuery {
  metric?: string;
  entity?: string;
  period?: string;
  nature?: string;
  kind?: string;
  band?: string;
  validation_status?: string;
  requires_review?: boolean;
  min_confidence?: number;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
}

export interface FactsResponse extends Paged<FinancialFact> {
  facets: { metrics: string[]; entities: string[]; periods: string[] };
}

export const api = {
  overview: () => request<Overview>("/overview"),
  system: () => request<Record<string, any>>("/system"),
  health: () => request<Record<string, any>>("/health"),
  metrics: () =>
    request<{ metrics: any[]; families: Record<string, string[]> }>("/metrics"),

  listDocuments: (p: { limit?: number; offset?: number; status?: string; search?: string } = {}) =>
    request<Paged<DocumentSummary>>(`/documents${qs(p)}`),
  getDocument: (id: string) => request<DocumentSummary>(`/documents/${id}`),
  getStatus: (id: string) => request<ProcessingStatus>(`/documents/${id}/status`),
  deleteDocument: (id: string) =>
    request<void>(`/documents/${id}`, { method: "DELETE" }),
  // ------------------------------------------------------------- lifecycle
  getControl: (id: string) =>
    request<DocumentControl>(`/documents/${id}/control`),
  pauseDocument: (id: string) =>
    request<ControlAck>(`/documents/${id}/pause`, { method: "POST" }),
  resumeDocument: (id: string) =>
    request<ControlAck>(`/documents/${id}/resume`, { method: "POST" }),
  cancelDocument: (id: string) =>
    request<ControlAck>(`/documents/${id}/cancel`, { method: "POST" }),

  reprocess: (id: string, forceOcr = false) =>
    request<{ job_id: string }>(
      `/documents/${id}/reprocess${qs({ force_ocr: forceOcr })}`,
      { method: "POST" },
    ),

  upload: (file: File, onProgress?: (pct: number) => void) =>
    new Promise<DocumentSummary>((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API}/documents`);
      xhr.setRequestHeader("X-FinSight-Request", REQUEST_HEADER["X-FinSight-Request"]);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else
            reject(
              new ApiError(
                xhr.status,
                body?.error?.code ?? "upload_failed",
                body?.error?.message ?? "Upload failed",
                body?.error?.detail,
              ),
            );
        } catch {
          reject(new ApiError(xhr.status, "upload_failed", "Upload failed"));
        }
      };
      xhr.onerror = () =>
        reject(new ApiError(0, "network_error", "Network error during upload"));
      xhr.send(form);
    }),

  /** What the run understood, and what it cost. Read by the document brief. */
  diagnostics: (id: string) =>
    request<DocumentDiagnostics>(`/documents/${id}/diagnostics`),
  pages: (id: string) => request<Paged<PageInfo>>(`/documents/${id}/pages`),
  page: (id: string, n: number, opts: { ocr?: boolean; regions?: boolean } = {}) =>
    request<PageDetail>(
      `/documents/${id}/pages/${n}${qs({
        include_ocr: opts.ocr ?? false,
        include_regions: opts.regions ?? true,
      })}`,
    ),
  pageImageUrl: (id: string, n: number) => `${API}/documents/${id}/pages/${n}/image`,
  thumbnailUrl: (id: string, n: number) =>
    `${API}/documents/${id}/pages/${n}/thumbnail`,

  financials: (id: string, q: FactQuery = {}) =>
    request<FactsResponse>(`/documents/${id}/financials${qs(q as any)}`),
  fact: (id: string, factId: string) =>
    request<FinancialFact & { validations: ValidationDto[]; review_history: any[] }>(
      `/documents/${id}/financials/${factId}`,
    ),
  provenance: (id: string, factId: string) =>
    request<Record<string, any>>(`/documents/${id}/provenance/${factId}`),

  tables: (id: string, page?: number) =>
    request<Paged<TableDto>>(`/documents/${id}/tables${qs({ page })}`),
  charts: (id: string) => request<Paged<ChartDto>>(`/documents/${id}/charts`),
  commentary: (id: string, q: { topic?: string; sentiment?: string } = {}) =>
    request<Paged<CommentaryDto> & { topics: { topic: string; count: number }[] }>(
      `/documents/${id}/commentary${qs(q)}`,
    ),
  guidance: (id: string) =>
    request<Paged<GuidanceDto> & { note: string }>(`/documents/${id}/guidance`),
  validation: (id: string, q: { status?: string; family?: string } = {}) =>
    request<
      Paged<ValidationDto> & {
        summary: Record<string, number>;
        by_family: Record<string, Record<string, number>>;
      }
    >(`/documents/${id}/validation${qs(q)}`),

  reviewQueue: (id: string, q: { limit?: number; offset?: number; include_resolved?: boolean } = {}) =>
    request<{ total: number; items: ReviewItem[] }>(`/documents/${id}/reviews${qs(q)}`),
  submitReview: (
    id: string,
    payload: {
      target_type?: string;
      target_id: string;
      action: string;
      reviewer?: string;
      note?: string;
      new_value?: Record<string, unknown>;
    },
  ) =>
    request<Record<string, any>>(`/documents/${id}/reviews`, {
      method: "POST",
      body: JSON.stringify({ target_type: "fact", reviewer: "analyst", ...payload }),
    }),
  reviewHistory: (id: string, targetId?: string) =>
    request<Paged<any>>(`/documents/${id}/reviews/history${qs({ target_id: targetId })}`),

  search: (q: string, documentId?: string) =>
    request<{ query: string; total: number; results: Record<string, any[]> }>(
      `/search${qs({ q, document_id: documentId })}`,
    ),

  compare: (p: {
    left: string;
    right: string;
    left_period?: string;
    right_period?: string;
    entity?: string;
    metrics?: string;
  }) =>
    request<{
      left: Record<string, any>;
      right: Record<string, any>;
      entities: string[];
      comparisons: ComparisonRow[];
      guidance_changes: Record<string, any>;
    }>(`/compare${qs(p)}`),

  /** Delete several documents at once, reusing the single-delete lifecycle. */
  bulkDelete: (documentIds: string[]) =>
    request<{
      requested: number;
      deleted: string[];
      missing: string[];
      busy: string[];
      failed: { document_id: string; error: string }[];
    }>("/documents/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ document_ids: documentIds }),
    }),

  exportUrl: (id: string, format: "json" | "csv" | "xlsx") =>
    `${API}/documents/${id}/export?format=${format}`,

  /** Fetch an export as a blob, so a failure surfaces instead of a blank tab. */
  exportFile: async (id: string, format: "json" | "csv" | "xlsx") => {
    const res = await fetch(`${API}/documents/${id}/export?format=${format}`);
    if (!res.ok) {
      let detail = `Export failed (${res.status})`;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch { /* a non-JSON error body leaves the status message */ }
      throw new ApiError(res.status, "export_failed", detail);
    }
    // Prefer the name the server chose; it carries the document's own title.
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const named = /filename="?([^"]+)"?/.exec(disposition);
    return {
      blob: await res.blob(),
      filename: named?.[1] ?? `finsight-${id}.${format}`,
    };
  },

  // ---------------------------------------------------- financial intelligence
  entities: (id: string) =>
    request<{ items: EntityDto[]; hierarchy: EntityNode[]; total: number }>(
      `/documents/${id}/entities`,
    ),
  financialSummary: (id: string, entityId?: string) =>
    request<{ statements: StatementGroup[]; total_facts: number }>(
      `/documents/${id}/financial-summary${qs({ entity_id: entityId })}`,
    ),
  derivedMetrics: (id: string, q: { period?: string; entity_id?: string } = {}) =>
    request<{
      items: DerivedMetricDto[];
      total: number;
      disagreements: number;
      note: string;
    }>(`/documents/${id}/derived-metrics${qs(q)}`),
  insights: (id: string, q: { insight_type?: string; limit?: number } = {}) =>
    request<{ items: InsightDto[]; total: number; note: string }>(
      `/documents/${id}/insights${qs(q)}`,
    ),
  anomalies: (id: string, q: { severity?: string } = {}) =>
    request<{
      items: AnomalyDto[];
      total: number;
      by_severity: Record<string, number>;
      by_type: Record<string, number>;
    }>(`/documents/${id}/anomalies${qs(q)}`),
  risks: (id: string, q: { category?: string } = {}) =>
    request<{
      items: RiskDto[];
      total: number;
      by_category: Record<string, number>;
      by_severity: Record<string, number>;
      forward_looking: number;
      note: string;
    }>(`/documents/${id}/risks${qs(q)}`),
  relations: (id: string, q: { fact_id?: string; kind?: string } = {}) =>
    request<{
      items: RelationDto[];
      total: number;
      by_kind: Record<string, number>;
    }>(`/documents/${id}/relations${qs(q)}`),
  explanation: (id: string, factId: string) =>
    request<FactExplanationDto>(
      `/documents/${id}/financials/${factId}/explanation`,
    ),
  tableSemantics: (id: string, statement?: string) =>
    request<{ items: TableSemanticsDto[]; total: number }>(
      `/documents/${id}/table-semantics${qs({ statement })}`,
    ),
};
