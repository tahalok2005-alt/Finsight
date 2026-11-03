"""A shared password for a deployed instance.

FinSight has no user accounts: on a laptop there is nobody to keep out. On a
public URL there is, because every document is visible to every visitor and
any visitor can delete any of them. ``FINSIGHT_ACCESS_PASSWORD`` closes that
with the least machinery that works.

It is HTTP Basic authentication on purpose. The browser asks once and then
attaches the credential to every same-origin request on its own -- the API
calls, and also the ``<img>`` tags that load page images and evidence crops,
which no header the dashboard could set would reach. So the frontend needed no
change at all, and nothing about the password ever lives in JavaScript.

Basic authentication sends the password with every request, so it is only as
private as the connection: a deployment must be served over HTTPS. Every host
the deployment notes recommend terminates HTTPS in front of the container.
"""
from __future__ import annotations

import base64
import binascii
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config.settings import settings

_CHALLENGE = 'Basic realm="FinSight", charset="UTF-8"'


def password_from_header(header: str | None) -> str | None:
    """The password in a Basic ``Authorization`` header, or None.

    The username is ignored: there is one shared password, and asking people
    to also agree on a username would add a way to fail and no protection.
    """
    if not header or header[:6].lower() != "basic ":
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _user, sep, password = decoded.partition(":")
    return password if sep else None


def install_access_control(app: FastAPI) -> bool:
    """Require the shared password on every request, if one is configured.

    Returns whether it did. Two things stay open: CORS preflights, which a
    browser never sends credentials with, and the health check, which a
    hosting platform polls to decide whether the container is alive and which
    says nothing about any document.
    """
    secret = settings.access_password
    if secret is None or not secret.get_secret_value():
        return False
    expected = secret.get_secret_value().encode("utf-8")
    health = f"{settings.api_prefix}/health"

    @app.middleware("http")
    async def require_password(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path == health:
            return await call_next(request)
        given = password_from_header(request.headers.get("authorization"))
        # constant-time: a password compared byte by byte leaks its prefix
        # through response timing
        if given is None or not hmac.compare_digest(given.encode("utf-8"), expected):
            return JSONResponse(
                status_code=401, headers={"WWW-Authenticate": _CHALLENGE},
                content={"error": {"code": "unauthorized",
                                   "message": "This FinSight instance needs a password."}})
        return await call_next(request)

    return True
