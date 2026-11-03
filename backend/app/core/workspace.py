"""Private workspaces: strangers on one public instance never see each other.

With ``FINSIGHT_WORKSPACES_ENABLED`` on, every browser is given a random
token in a cookie, and every document is stamped with the workspace that
uploaded it. A visitor can list, open, search, compare and delete only their
own documents; anyone else's answer exactly as a document that does not exist
would -- 404, not 403, so even the existence of another person's upload is not
disclosed.

There are no accounts. The token is the only credential, so it is long
(256 bits), it lives in an HttpOnly cookie the dashboard's JavaScript cannot
read, and the database stores only its SHA-256: a copy of the database does not
let anyone become a workspace.

The check is made in two places, and those two are the whole of it:

* ``get_document_or_404`` -- every route that takes a document id resolves the
  document through it, so one check covers them all;
* ``owns(model)`` -- the condition the routes that span documents (the list,
  the overview, search) add to every query they make.

Both fail closed: with workspaces on and no workspace in context, they match
nothing at all.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import false, select, true

from app.config.settings import settings

COOKIE = "finsight_ws"
#: Sent by the dashboard on every request that changes something. A form or a
#: script on another site cannot add a custom header to a cross-site request
#: without a CORS preflight this API refuses, so a request without it did not
#: come from the dashboard.
CSRF_HEADER = "X-FinSight-Request"

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
#: The cookie outlives the documents in it, so a returning visitor finds an
#: empty workspace rather than a new one; the documents themselves expire on
#: the retention schedule, not with the cookie.
_COOKIE_DAYS = 30

_current: ContextVar[str | None] = ContextVar("finsight_workspace", default=None)


def enabled() -> bool:
    return settings.workspaces_enabled


def current() -> str | None:
    """The calling browser's workspace id (a hash), or None."""
    return _current.get()


def workspace_id_for(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def visible(doc) -> bool:
    """Whether the caller may see ``doc``. Always true with workspaces off."""
    if not enabled():
        return True
    ws = current()
    return ws is not None and doc is not None and doc.workspace_id == ws


def owns(model):
    """A SQL condition limiting ``model``'s rows to the caller's workspace.

    ``model`` is the Document model or any table with a ``document_id``
    column. With workspaces off it is simply true.
    """
    if not enabled():
        return true()
    ws = current()
    if ws is None:
        return false()
    from app.database.models import Document  # local: models import settings

    if model is Document:
        return Document.workspace_id == ws
    return model.document_id.in_(
        select(Document.id).where(Document.workspace_id == ws))


def _cookie_header(token: str, request: Request) -> str:
    parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly",
             f"Max-Age={_COOKIE_DAYS * 86400}"]
    if request.url.scheme == "https":
        # SameSite=None so the cookie survives the dashboard being embedded
        # in another page (a Hugging Face Space shows it in an iframe);
        # Partitioned so that, embedded, it is keyed to that one host page
        # and cannot follow the visitor anywhere else.
        parts += ["Secure", "SameSite=None", "Partitioned"]
    else:
        parts.append("SameSite=Lax")
    return "; ".join(parts)


def install_workspaces(app: FastAPI) -> None:
    """Give every request its browser's workspace.

    Installed unconditionally and decided per request, so the mode can be
    switched by configuration (and by tests) without rebuilding the app.
    """
    api = settings.api_prefix

    @app.middleware("http")
    async def assign_workspace(request: Request, call_next):
        if not enabled():
            return await call_next(request)

        if (request.method in _UNSAFE and request.url.path.startswith(api)
                and request.headers.get(CSRF_HEADER) != "1"):
            return JSONResponse(status_code=403, content={"error": {
                "code": "missing_request_header",
                "message": "Changes are accepted only from the FinSight dashboard."}})

        token = request.cookies.get(COOKIE)
        issued = None
        if not token or not _TOKEN.fullmatch(token):
            token = issued = secrets.token_urlsafe(32)  # 43 characters

        reset = _current.set(workspace_id_for(token))
        try:
            response = await call_next(request)
        finally:
            _current.reset(reset)
        if issued is not None:
            response.headers.append("set-cookie", _cookie_header(issued, request))
        return response
