"""Structured logging.

Every pipeline event carries document_id / page_number / stage so processing can
be reconstructed after the fact.  Never swallow exceptions silently: use
``log.exception`` or the ``stage_timer`` context manager.
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from time import perf_counter
from typing import Any, Iterator

import structlog

from app.config.settings import settings

_CONFIGURED = False


def configure_logging(level: str | None = None, json_logs: bool | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or settings.log_level).upper()
    use_json = settings.log_json if json_logs is None else json_logs

    try:
        # A legacy code page cannot encode every character OCR produces; a log
        # line is written with escapes instead of raising a logging error.
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, lvl, logging.INFO),
    )
    for noisy in ("httpx", "httpcore", "watchfiles", "multipart", "python_multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if use_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, lvl, logging.INFO)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str = "finsight") -> Any:
    configure_logging()
    return structlog.get_logger(name)


@contextmanager
def stage_timer(log: Any, stage: str, **ctx: Any) -> Iterator[dict]:
    """Times a pipeline stage and always emits a terminal event."""
    payload: dict[str, Any] = {}
    t0 = perf_counter()
    log.info("stage.start", stage=stage, **ctx)
    try:
        yield payload
    except Exception as exc:  # noqa: BLE001 - re-raised, never swallowed
        log.exception("stage.failed", stage=stage,
                      duration_ms=round((perf_counter() - t0) * 1000, 1),
                      error=str(exc), error_type=type(exc).__name__, **ctx)
        raise
    else:
        log.info("stage.done", stage=stage,
                 duration_ms=round((perf_counter() - t0) * 1000, 1),
                 **payload, **ctx)
