"""Application configuration loaded from environment / .env.

All settings are overridable via environment variables. See `.env.example`.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bucket every unlabeled caller shares when auth is off. Downstream code (rate
# limiting, cost attribution) always has something to key on, whether or not a
# key was presented.
ANONYMOUS_LABEL = "anonymous"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "Unified Search API"
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    log_format: Literal["json", "text"] = Field(
        default="json",
        description=(
            "'json' emits one JSON object per log record (request_id, latency and "
            "access lines included) for a log shipper; 'text' restores the plain "
            "one-line format for a human reading a local run."
        ),
    )

    # --- Observability (§7.3) ---
    otel_enabled: bool = Field(
        default=False,
        description=(
            "Enable OpenTelemetry tracing of FastAPI and httpx. The exporter is "
            "configured through the OTel SDK's own environment variables "
            "(OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS)."
        ),
    )

    # --- Auth (on by default) ---
    # When enabled, requests must send `Authorization: Bearer <key>` or `X-API-Key`.
    # `/v1/research` spends real money on provider keys, so an unauthenticated
    # instance is an open tab on someone else's bill: the app refuses to start
    # with auth enabled and no key (see `validate_startup`).
    auth_enabled: bool = Field(default=True)
    # Comma-separated list of accepted API keys (§6.2). Each entry is either
    # "label:key" or a bare "key" (which gets a stable derived label). Labels —
    # never keys — are what logs, rate-limit buckets and cost counters record.
    api_keys: str = Field(default="")

    # --- Hardening ---
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_rpm: int = Field(
        default=120,
        description=(
            "Points per 60s window per caller. Endpoints cost different amounts "
            "(§6.3): /v1/search costs 1, /v1/research costs 100."
        ),
    )
    rate_limit_rpm_overrides: str = Field(
        default="",
        description="Comma-separated label:rpm overrides, e.g. 'app-mobile:500,test:20'.",
    )
    rate_limit_max_clients: int = Field(
        default=10_000,
        description="Max distinct clients tracked by the limiter (oldest evicted first).",
    )
    # Set to true only when behind a trusted reverse-proxy that sets X-Forwarded-For;
    # otherwise an attacker can spoof the header to bypass the rate limiter.
    trusted_proxy: bool = Field(default=False)
    security_headers: bool = Field(default=True)
    cors_origins: str = Field(default="*", description="Comma-separated allowed CORS origins.")

    # --- SearXNG ---
    # Internal docker DNS name by default; override to http://localhost:8080 for local runs.
    searxng_url: str = Field(default="http://searxng:8080")
    searxng_timeout: float = Field(default=15.0)
    searxng_max_connections: int = Field(
        default=64, description="Max pooled HTTP connections to SearXNG."
    )

    # --- Search cache (memoizes identical queries to cut redundant round-trips) ---
    search_cache_enabled: bool = Field(default=True)
    search_cache_ttl: float = Field(
        default=300.0, description="Seconds an identical search result is reused."
    )
    search_cache_max_size: int = Field(
        default=512, description="Max distinct queries kept (LRU eviction)."
    )

    # --- Answer cache (§5.7): identical questions must not re-run the pipeline ---
    answer_cache_enabled: bool = Field(default=True)
    answer_cache_ttl: float = Field(
        default=600.0, description="Seconds an identical /v1/answer response is reused."
    )
    answer_cache_max_size: int = Field(
        default=256, description="Max distinct answers kept (LRU eviction)."
    )

    # --- Jobs ---
    # §6.1: "sqlite" survives a restart (a finished report is worth minutes and
    # cents); "memory" keeps everything in-process and loses it all on restart.
    job_backend: Literal["sqlite", "memory"] = Field(
        default="sqlite",
        description="Where jobs and their progress events are stored.",
    )
    job_db_path: str = Field(
        default="/srv/api/data/jobs.db",
        description="SQLite file for JOB_BACKEND=sqlite. Its directory is created on first use.",
    )
    job_vacuum_interval_seconds: float = Field(
        default=86_400.0,
        description=(
            "How often the SQLite job database is VACUUMed. Coarser than the evict "
            "sweep on purpose: VACUUM rewrites the whole file. 0 disables it."
        ),
    )
    job_retention_seconds: float = Field(
        default=3600.0, description="Seconds a finished job is kept before eviction."
    )
    job_evict_interval_seconds: float = Field(
        default=300.0,
        description="How often the background sweeper drops aged-out jobs. 0 disables it.",
    )
    job_max_progress_events: int = Field(
        default=500,
        description="Progress events retained per job (oldest dropped). Bounds the SSE replay.",
    )
    job_subscriber_queue_size: int = Field(
        default=1000,
        description="Max buffered events per SSE subscriber; a slow client loses the oldest.",
    )

    # --- LLM (litellm provider strings, e.g. "openai/gpt-4o-mini", "anthropic/claude-...") ---
    llm_model: str = Field(default="openai/gpt-4o-mini")
    # Optional dedicated model for long-form report writing; falls back to llm_model.
    llm_report_model: str = Field(default="")
    llm_temperature: float = Field(default=0.2)
    llm_max_tokens: int = Field(default=4096)
    llm_report_max_tokens: int = Field(
        default=8192,
        description=(
            "Max output tokens for the final research report. Separate from "
            "LLM_MAX_TOKENS, which would cap a multi-page report at ~3000 words."
        ),
    )
    llm_fallback_models: str = Field(
        default="",
        description=(
            "Comma-separated litellm model strings tried in order if the primary model fails."
        ),
    )
    llm_timeout: float = Field(
        default=120.0, description="Max seconds for a single non-streaming LLM call."
    )
    llm_stream_timeout: float = Field(
        default=30.0, description="Max seconds a streaming LLM call may stall before it is retried."
    )
    llm_max_retries: int = Field(
        default=3, description="Retries per model before the next fallback model is tried."
    )
    llm_max_concurrency: int = Field(
        default=10,
        description=(
            "Process-wide cap on in-flight LLM calls, to avoid saturating the provider quota."
        ),
    )
    # Provider keys are read by litellm directly from the environment
    # (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, ...).

    # --- Crawl defaults ---
    crawl_max_concurrency: int = Field(
        default=5, description="Hard cap on concurrent browser sessions, process-wide."
    )
    crawl_timeout: float = Field(default=30.0)
    crawl_per_host_delay: float = Field(
        default=1.0,
        description="Minimum seconds between two hits on the same host. 0 disables it.",
    )
    # --- Document ingestion (§15) ---
    crawl_enable_documents: bool = Field(
        default=True,
        description=(
            "Route document URLs (Word, PowerPoint, Excel, OpenDocument, RTF, EPUB, "
            "CSV, PDF) through anydoc instead of the browser. Off means those URLs go "
            "to Chromium, which gets nothing out of them."
        ),
    )
    crawl_doc_max_mb: float = Field(
        default=25.0,
        description=(
            "Refuse documents larger than this many megabytes. Caps the DOWNLOAD; "
            "decompression has its own limits inside anydoc."
        ),
    )
    crawl_pdf_fallback: bool = Field(
        default=True,
        description=(
            "Fall back to pypdf when anydoc fails on a PDF. Worth keeping on while "
            "anydoc is still 0.1.x."
        ),
    )
    convert_max_mb: float = Field(
        default=25.0, description="Max size of a file uploaded to POST /v1/convert."
    )
    # Superseded by the two settings above. Kept working for one version because
    # they are already deployed; `deprecation_warnings` says so at startup.
    crawl_enable_pdf: bool = Field(
        default=True,
        description="Deprecated alias for CRAWL_ENABLE_DOCUMENTS.",
    )
    crawl_pdf_max_mb: float = Field(
        default=25.0, description="Deprecated alias for CRAWL_DOC_MAX_MB."
    )

    # --- URL discovery (/v1/map) ---
    map_timeout: float = Field(
        default=5.0,
        description=(
            "Max seconds per sitemap.xml / robots.txt fetch. Kept short: /v1/map exists "
            "to answer in about a second."
        ),
    )

    # --- Crawl network guard (SSRF) ---
    # Every URL the crawler is asked to fetch is resolved and checked before the
    # browser is handed the address. See `app.core.net.guard`.
    crawl_allow_private_networks: bool = Field(
        default=False,
        description="Allow crawling loopback/private/link-local addresses. Never enable in prod.",
    )
    crawl_allowed_ports: str = Field(
        default="80,443,8080",
        description="Comma-separated ports the crawler may connect to. Empty = any port.",
    )
    crawl_domain_allowlist: str = Field(
        default="",
        description="If set, only these domains (and subdomains) may be crawled.",
    )
    crawl_domain_denylist: str = Field(
        default="", description="Domains (and subdomains) the crawler must never fetch."
    )

    # --- Research defaults (deep-research engine) ---
    research_default_breadth: int = Field(default=4)
    research_default_depth: int = Field(default=2)
    research_concurrency: int = Field(default=2)
    # Max wall-clock seconds for a synchronous /v1/research call. 0 = no limit.
    # Long runs should use background=true instead.
    research_sync_timeout: float = Field(default=600.0)
    research_report_token_budget: int = Field(
        default=12_000,
        description="Token budget for the learnings fed to the final-report prompt.",
    )
    research_results_per_query: int = Field(
        default=10,
        description=(
            "SERP results requested per query. Widened on purpose: titles and snippets "
            "are free, and only the reranked top-k is actually crawled."
        ),
    )
    research_crawl_top_k: int = Field(
        default=3,
        description="How many reranked results per SERP query are actually crawled.",
    )
    research_rerank: Literal["off", "lexical", "llm"] = Field(
        default="llm",
        description=(
            "Pre-crawl relevance filter: 'off' keeps the SERP order, 'lexical' scores "
            "titles/snippets locally, 'llm' asks a model (falls back to lexical on failure)."
        ),
    )
    research_rerank_model: str = Field(
        default="",
        description="Model used by the LLM reranker. Empty = LLM_MODEL. Use a fast, cheap one.",
    )
    research_rerank_cache_ttl: float = Field(
        default=600.0,
        description="Seconds a rerank decision is reused across overlapping SERPs. 0 disables.",
    )
    research_per_page_token_budget: int = Field(
        default=2000, description="Max tokens kept from a single crawled page."
    )
    research_process_context_token_budget: int = Field(
        default=8000,
        description="Total token budget for the page contents fed to one extraction call.",
    )
    research_num_learnings: int = Field(
        default=3, description="Max learnings extracted per SERP query."
    )
    research_num_followups: int = Field(
        default=3, description="Max follow-up questions extracted per SERP query."
    )

    # --- Cost circuit breakers (§6.4) ---
    research_max_cost_usd: float = Field(
        default=0.0,
        description=(
            "Abort a research run once its cumulative cost passes this, writing the "
            "report with what was gathered so far. 0 disables."
        ),
    )
    daily_cost_cap_usd: float = Field(
        default=0.0,
        description=(
            "Max USD spend per API-key label per UTC day, across /v1/research and "
            "/v1/answer. Durable with JOB_BACKEND=sqlite, in-memory otherwise. 0 disables."
        ),
    )

    # --- MCP server (Phase 14) ---
    # The MCP surface is mounted in *this* process, on MCP_PATH, so it shares the
    # browser pool, the SearXNG cache, the cost ledger and the JobManager with the
    # HTTP API. See MCP_REFERENCE.md.
    mcp_enabled: bool = Field(default=True)
    mcp_path: str = Field(default="/mcp", description="Path the streamable-HTTP transport serves.")
    mcp_allowed_hosts: str = Field(
        default="",
        description=(
            "Comma-separated Host values the MCP transport accepts (DNS-rebinding "
            "protection). In a container the request Host is not localhost, so an "
            "empty list here would refuse every MCP request: empty therefore means "
            "'accept any Host', which is safe behind a reverse proxy that already "
            "pins it. Set it explicitly when the API is exposed directly."
        ),
    )
    mcp_max_result_tokens: int = Field(
        default=4000,
        description=(
            "Token ceiling on ONE tool result. This is agent context, not "
            "bandwidth: whatever a tool returns is re-sent on every following turn."
        ),
    )
    mcp_max_urls_per_call: int = Field(
        default=10, description="Max URLs a single fetch_url call may take."
    )
    mcp_artifact_ttl: float = Field(
        default=1800.0, description="Seconds a full tool payload stays readable as a resource."
    )
    mcp_artifact_max_items: int = Field(
        default=256, description="Max artefacts kept in memory (least-recently-used evicted)."
    )
    mcp_image_mode: Literal["off", "resource", "inline"] = Field(
        default="off",
        description=(
            "Screenshots and PDF renders are never inlined by default: base64 in a "
            "tool result is context an agent pays for on every turn. 'resource' "
            "returns a link the client may load; 'inline' embeds the image."
        ),
    )
    mcp_research_max_concurrent: int = Field(
        default=2,
        description=(
            "Simultaneous deep_research runs per API-key label. DAILY_COST_CAP_USD "
            "is a finish-line limit; this one stops an agent loop from reaching it "
            "in thirty seconds."
        ),
    )

    # --- Webhooks (§5.8) ---
    webhook_secret: str = Field(
        default="",
        description=(
            "HMAC-SHA256 key for the X-Webhook-Signature header. Webhooks are refused "
            "while this is empty: an unsigned callback cannot be trusted by its receiver."
        ),
    )
    webhook_timeout: float = Field(
        default=10.0, description="Max seconds for the job-completion callback POST."
    )

    @property
    def api_key_labels(self) -> dict[str, str]:
        """Raw key -> label (§6.2).

        Accepts ``label:key`` and bare ``key``. A bare key gets a stable label
        derived from a truncated SHA-256 of the key: unique in practice, constant
        across restarts, and not reversible to the key — unlike a prefix or a
        suffix of it, which would leak key material into every log line.
        """
        labels: dict[str, str] = {}
        for raw in self.api_keys.split(","):
            entry = raw.strip()
            if not entry:
                continue
            label, sep, key = entry.partition(":")
            label, key = label.strip(), key.strip()
            if not sep or not label or not key:
                # A bare key (or a malformed entry): the whole entry is the key.
                key = entry
                label = f"key-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
            labels[key] = label
        return labels

    @property
    def api_key_set(self) -> set[str]:
        return set(self.api_key_labels)

    @property
    def rate_limit_overrides(self) -> dict[str, int]:
        """Label -> points-per-window, overriding ``rate_limit_rpm``."""
        overrides: dict[str, int] = {}
        for raw in self.rate_limit_rpm_overrides.split(","):
            entry = raw.strip()
            if not entry:
                continue
            label, sep, value = entry.partition(":")
            label, value = label.strip(), value.strip()
            if not sep or not label or not value.isdigit():
                continue
            overrides[label] = int(value)
        return overrides

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]

    @property
    def mcp_allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def report_model(self) -> str:
        return self.llm_report_model or self.llm_model

    @property
    def llm_fallback_model_list(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def documents_enabled(self) -> bool:
        """CRAWL_ENABLE_DOCUMENTS, honouring the CRAWL_ENABLE_PDF alias (§15.7).

        The new name wins whenever it was actually provided; the old one is only
        consulted when it was set and the new one was not, so an operator who has
        `CRAWL_ENABLE_PDF=false` deployed keeps the behaviour they asked for
        instead of silently gaining fourteen formats on upgrade.
        """
        if self._alias_in_effect("crawl_enable_documents", "crawl_enable_pdf"):
            return self.crawl_enable_pdf
        return self.crawl_enable_documents

    @property
    def doc_max_mb(self) -> float:
        """CRAWL_DOC_MAX_MB, honouring the CRAWL_PDF_MAX_MB alias (§15.7)."""
        if self._alias_in_effect("crawl_doc_max_mb", "crawl_pdf_max_mb"):
            return self.crawl_pdf_max_mb
        return self.crawl_doc_max_mb

    def _alias_in_effect(self, current: str, legacy: str) -> bool:
        """Whether the deprecated name is the one that should be read.

        ``model_fields_set`` is what makes this work: it holds the fields an
        operator actually provided, so an unset new setting is distinguishable
        from one explicitly set to its default value.
        """
        return current not in self.model_fields_set and legacy in self.model_fields_set

    def deprecation_warnings(self) -> list[str]:
        """Settings that still work but are on their way out (§15.7).

        Returned rather than logged so the caller decides where they go, and so a
        test can assert on them without capturing log records.
        """
        warnings: list[str] = []
        for legacy, current in (
            ("crawl_enable_pdf", "crawl_enable_documents"),
            ("crawl_pdf_max_mb", "crawl_doc_max_mb"),
        ):
            if legacy in self.model_fields_set:
                warnings.append(
                    f"{legacy.upper()} is deprecated and will be removed in a future "
                    f"version: use {current.upper()} instead."
                    + (
                        f" {current.upper()} is also set and takes precedence."
                        if current in self.model_fields_set
                        else ""
                    )
                )
        return warnings

    @property
    def crawl_allowed_port_set(self) -> frozenset[int]:
        ports: set[int] = set()
        for raw in self.crawl_allowed_ports.split(","):
            raw = raw.strip()
            if raw.isdigit():
                ports.add(int(raw))
        return frozenset(ports)

    @property
    def crawl_domain_allowlist_set(self) -> frozenset[str]:
        return _domain_set(self.crawl_domain_allowlist)

    @property
    def crawl_domain_denylist_set(self) -> frozenset[str]:
        return _domain_set(self.crawl_domain_denylist)

    def validate_startup(self) -> None:
        """Fail fast on configurations that would silently expose the API.

        Raises ``RuntimeError`` rather than warning: an instance that accepts every
        request is worse than one that does not boot.
        """
        if self.auth_enabled and not self.api_key_set:
            raise RuntimeError(
                "AUTH_ENABLED is true but API_KEYS is empty: every request would be "
                "rejected with 401 and no client could ever authenticate. Generate a key "
                "with `openssl rand -hex 32` and set API_KEYS, or set AUTH_ENABLED=false "
                "for a local development run."
            )


def _domain_set(raw: str) -> frozenset[str]:
    return frozenset(d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
