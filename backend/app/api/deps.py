"""Shared API dependencies and response helpers."""
from __future__ import annotations

from typing import Annotated, Any, Iterator

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.core import workspace
from app.core.errors import DocumentNotFound
from app.database import repository as repo
from app.database.models import Document
from app.database.session import get_db

DbSession = Annotated[Session, Depends(get_db)]


def get_document_or_404(document_id: str, session: Session) -> Document:
    doc = repo.get_document(session, document_id)
    # Another workspace's document is answered exactly as a missing one, so a
    # visitor cannot even learn that it exists.
    if doc is None or not workspace.visible(doc):
        raise DocumentNotFound(f"No document with id {document_id!r}",
                               detail={"document_id": document_id})
    return doc


class Pagination:
    def __init__(self,
                 limit: int = Query(100, ge=1, le=1000),
                 offset: int = Query(0, ge=0)):
        self.limit = limit
        self.offset = offset


PaginationDep = Annotated[Pagination, Depends(Pagination)]


def page_envelope(items: list[Any], total: int, pagination: Pagination) -> dict:
    return {"items": items, "total": total, "limit": pagination.limit,
            "offset": pagination.offset,
            "has_more": pagination.offset + len(items) < total}
