"""Serve the built dashboard from the API's own origin.

In development Vite serves the dashboard and forwards ``/api`` to this process.
Deployed, there is no Vite: this process serves the built files itself, so the
whole application is one container on one address. That keeps the dashboard's
relative ``/api/v1`` URLs and its page-image ``<img>`` tags working unchanged,
with no CORS configuration and no API address baked into the build.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config.settings import settings


def resolve_static_file(root: Path, path: str) -> Path | None:
    """The file under ``root`` that ``path`` names, or None.

    Resolved and then checked to still be inside ``root``, so ``../`` in a
    request can never read anything outside the build directory.
    """
    if not path:
        return None
    candidate = (root / path).resolve()
    if candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return None


def mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve ``static_dir`` at ``/``, with client-side routes falling back to
    ``index.html``.

    The dashboard routes in the browser (``/documents/<id>``), so a path that
    is not a file is the dashboard's to interpret -- except a path under the
    API, which gets an honest 404 rather than a page of HTML that a client
    would then fail to parse as JSON.
    """
    root = static_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        raise RuntimeError(
            f"FINSIGHT_STATIC_DIR={static_dir} has no index.html: "
            "build the dashboard first (cd frontend && npm run build)")

    if (root / "assets").is_dir():
        # content-hashed file names, so these never go stale
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    reserved = {settings.api_prefix.strip("/").split("/")[0],
                "docs", "redoc", "openapi.json"}

    @app.get("/{path:path}", include_in_schema=False)
    def dashboard(path: str):
        if path.split("/", 1)[0] in reserved:
            return JSONResponse(status_code=404, content={"error": {
                "code": "not_found", "message": f"No such endpoint: /{path}"}})
        found = resolve_static_file(root, path)
        if found is not None:
            return FileResponse(found)
        # index.html names the current hashed bundles, so it must never be
        # served from a cache after a redeploy
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
