"""Structured logging and request correlation (§7.1).

Two things this module provides, both built on the standard library alone (no
``structlog`` / ``python-json-logger``: a formatter and a filter is all that is
actually needed here):

* **A JSON formatter.** One object per line, so a log shipper can index
  ``level``/``logger``/``request_id`` instead of regex-ing a text line. Anything
  passed via ``extra={...}`` at the call site is merged into the object, which is
  how the access log carries ``method``/``path``/``status_code``/``duration_ms``.
  ``LOG_FORMAT=text`` keeps the previous human-readable one-liner.

* **Request correlation.** ``request_id_var`` is set once per request by
  `RequestContextMiddleware` and read back by `RequestIdFilter`, which stamps it
  onto *every* record emitted while that request is on the stack — route
  handlers, services, the research engine, the crawler — without a single one of
  those call sites having to thread the id through. A ``contextvars.ContextVar``
  is the right carrier: asyncio copies the context per task, so concurrent
  requests on the same worker never see each other's id.

Nothing here may break a request. A formatter that raises is caught by
``logging.Handler.handleError`` and printed to stderr, never propagated to the
caller — which is the behaviour we want from an observability layer.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# The id of the HTTP request currently being served, or None outside one
# (startup, background sweeps, jobs that outlive their submitting request).
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# Attributes every LogRecord carries. Anything else on a record came from an
# `extra={...}` at the call site and belongs in the JSON payload.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))) | {
    "asctime",
    "message",
    "taskName",
}

# Marks the handler this module installed, so a second `configure_logging` call
# (a test, a reload) replaces it instead of stacking a duplicate on the root.
_MARKER = "_unified_search_api_handler"

_TEXT_FORMAT = "%(levelname)s:%(name)s:%(message)s"


class RequestIdFilter(logging.Filter):
    """Copy the ambient request id onto every record that lacks one."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record, on a single line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key != "request_id":
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # `default=str` so an unserializable value in an `extra` degrades to its
        # repr rather than losing the whole line.
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """The previous plain format, with the request id appended when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        request_id = getattr(record, "request_id", None)
        return f"{line} [request_id={request_id}]" if request_id else line


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Install the root handler. Idempotent: re-running replaces our handler.

    Applied to the root logger so that libraries (litellm, httpx, crawl4ai) are
    formatted the same way as the application's own loggers. Uvicorn keeps its
    own handlers on its own loggers, which is fine — those never reach here.
    """
    formatter: logging.Formatter = (
        JsonFormatter() if log_format == "json" else TextFormatter(_TEXT_FORMAT)
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())
    setattr(handler, _MARKER, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _MARKER, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    try:
        root.setLevel(level.upper())
    except ValueError:  # pragma: no cover - an unknown LOG_LEVEL must not abort startup
        root.setLevel(logging.INFO)
