"""Application configuration loaded from environment / .env.

All settings are overridable via environment variables. See `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Auth (on by default) ---
    # When enabled, requests must send `Authorization: Bearer <key>` or `X-API-Key`.
    # `/v1/research` spends real money on provider keys, so an unauthenticated
    # instance is an open tab on someone else's bill: the app refuses to start
    # with auth enabled and no key (see `validate_startup`).
    auth_enabled: bool = Field(default=True)
    # Comma-separated list of accepted API keys, e.g. "key1,key2".
    api_keys: str = Field(default="")

    # --- Hardening ---
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_rpm: int = Field(default=120, description="Max requests per minute per client IP.")
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

    # --- Jobs ---
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

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]

    @property
    def report_model(self) -> str:
        return self.llm_report_model or self.llm_model

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
