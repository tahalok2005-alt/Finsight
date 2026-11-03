"""FastAPI application entry point."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    routes_data, routes_documents, routes_intelligence, routes_system,
)
from app.api.frontend import mount_frontend
from app.config.settings import settings
from app.core.access import install_access_control
from app.core.errors import FinSightError
from app.core.logging import configure_logging, get_logger
from app.database.session import init_db
from app.ocr.registry import probe_engines
from app.services.reconcile import (
    reconcile_interrupted_runs, sweep_orphaned_files,
)
from app.workers.queue import queue

configure_logging()
log = get_logger("api")

DESCRIPTION = """
FinSight turns financial PDFs into structured, validated, traceable data.

Every page is **rendered to an image** and analysed visually -- OCR, layout,
ruling lines and column geometry -- because a financial PDF encodes meaning in
its layout, not in its glyph stream.  The PDF text layer is used only as a
corroborating signal, so scanned documents work the same way.

Every extracted figure carries its unit, currency, period, confidence, the
arithmetic checks it passed or failed, and the exact page region it came from.
No paid LLM API is involved at any point.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    init_db()
    reclaimed = reconcile_interrupted_runs()
    if reclaimed:
        log.info("api.reconciled_interrupted", documents=reclaimed)
    swept = sweep_orphaned_files()
    if swept:
        log.info("api.swept_orphan_files", directories=swept)
    queue.start()
    engines = probe_engines()
    usable = [n for n, i in engines.items() if i.get("available")]
    log.info("api.startup", version=settings.app_version,
             environment=settings.environment, ocr_engines=usable,
             configured_engine=settings.ocr_engine,
             password_required=PASSWORD_REQUIRED,
             serving_dashboard=settings.static_dir is not None)
    if settings.ocr_engine not in usable:
        # Loud, but not fatal: uploads will fail honestly at the OCR stage.
        log.error("api.ocr_engine_unavailable",
                  configured=settings.ocr_engine, available=usable)
    yield
    queue.shutdown(wait=False)
    log.info("api.shutdown")


app = FastAPI(
    title=f"{settings.app_name} API",
    description=DESCRIPTION,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

PASSWORD_REQUIRED = install_access_control(app)


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    response.headers["X-Process-Time-Ms"] = str(duration_ms)
    if not request.url.path.endswith(("/image", "/thumbnail", "/crop")):
        log.info("http", method=request.method, path=request.url.path,
                 status=response.status_code, duration_ms=duration_ms)
    return response


@app.exception_handler(FinSightError)
async def finsight_error_handler(request: Request, exc: FinSightError):
    log.warning("api.domain_error", code=exc.code, message=exc.message,
                path=request.url.path)
    return JSONResponse(status_code=exc.status_code,
                        content={"error": exc.to_dict()})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {
        "code": "request_validation_failed",
        "message": "The request payload is invalid.",
        "detail": {"errors": exc.errors()[:20]}}})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    # Never leak internals to the client, but always record the full trace.
    log.exception("api.unhandled", path=request.url.path, error=str(exc),
                  error_type=type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": {
        "code": "internal_error",
        "message": "An unexpected error occurred. The incident has been logged.",
        "detail": {"type": type(exc).__name__}}})


api = settings.api_prefix
app.include_router(routes_system.router, prefix=api)
app.include_router(routes_documents.router, prefix=api)
app.include_router(routes_data.router, prefix=api)
app.include_router(routes_intelligence.router, prefix=api)
app.include_router(routes_data.compare_router, prefix=api)
app.include_router(routes_data.search_router, prefix=api)
app.include_router(routes_data.meta_router, prefix=api)


if settings.static_dir is not None:
    # registered last: its catch-all route must not shadow any API route
    mount_frontend(app, settings.static_dir)
else:
    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"name": settings.app_name, "version": settings.app_version,
                "docs": "/docs", "api": settings.api_prefix,
                "health": f"{settings.api_prefix}/health"}
