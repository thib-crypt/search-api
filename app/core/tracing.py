"""Optional OpenTelemetry tracing (§7.3).

Off by default (``OTEL_ENABLED=false``). When on, it instruments the two layers
that make a research run legible end to end:

* **FastAPI** — one server span per request, so a slow ``/v1/research`` can be
  opened up instead of guessed at.
* **httpx** — a client span per outbound call, which covers the SearXNG queries
  and (since litellm calls providers over httpx) most model calls too. litellm
  has its own callback-based instrumentation which is deliberately *not* wired
  here; the httpx spans are enough to see the search → crawl → LLM cascade, and
  §6.5's `UsageTracker` already reports the part litellm knows and httpx does not
  (tokens and cost).

The exporter is configured entirely through the OTel SDK's own environment
variables — ``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_HEADERS``,
``OTEL_RESOURCE_ATTRIBUTES`` — rather than through `Settings`. Re-plumbing a
convention every collector, sidecar and Helm chart already speaks would only add
a second name for the same thing.

Best-effort by construction: a missing package, an unreachable collector or an
instrumentation library that has moved on all end in a warning, never in a
failed startup. Tracing that can take the API down is worse than no tracing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("unified_search_api.tracing")

_started = False


def setup_tracing(app: Any, *, service_name: str = "unified-search-api") -> bool:
    """Wire FastAPI + httpx tracing. Returns whether it was actually enabled.

    Idempotent: instrumenting twice would double every span, and both the app
    object and the httpx instrumentation are process-wide.
    """
    global _started
    if _started:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        # Batched and backgrounded: a collector that is slow or down must not add
        # latency to a request, and BatchSpanProcessor drops spans rather than block.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    except Exception:  # noqa: BLE001 - tracing is never worth a failed startup
        logger.warning("OTEL_ENABLED is true but tracing could not be set up", exc_info=True)
        return False

    _started = True
    logger.info("OpenTelemetry tracing enabled (service.name=%s)", service_name)
    return True
