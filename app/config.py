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

    # --- Auth (optional) ---
    # When enabled, requests must send `Authorization: Bearer <key>` or `X-API-Key`.
    auth_enabled: bool = Field(default=False)
    # Comma-separated list of accepted API keys, e.g. "key1,key2".
    api_keys: str = Field(default="")

    # --- Hardening ---
    rate_limit_enabled: bool = Field(default=False)
    rate_limit_rpm: int = Field(default=120, description="Max requests per minute per client IP.")
    security_headers: bool = Field(default=True)
    cors_origins: str = Field(default="*", description="Comma-separated allowed CORS origins.")

    # --- SearXNG ---
    # Internal docker DNS name by default; override to http://localhost:8080 for local runs.
    searxng_url: str = Field(default="http://searxng:8080")
    searxng_timeout: float = Field(default=15.0)

    # --- LLM (litellm provider strings, e.g. "openai/gpt-4o-mini", "anthropic/claude-...") ---
    llm_model: str = Field(default="openai/gpt-4o-mini")
    # Optional dedicated model for long-form report writing; falls back to llm_model.
    llm_report_model: str = Field(default="")
    llm_temperature: float = Field(default=0.2)
    llm_max_tokens: int = Field(default=4096)
    # Provider keys are read by litellm directly from the environment
    # (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, ...).

    # --- Crawl defaults ---
    crawl_max_concurrency: int = Field(default=5)
    crawl_timeout: float = Field(default=30.0)

    # --- Research defaults (deep-research engine) ---
    research_default_breadth: int = Field(default=4)
    research_default_depth: int = Field(default=2)
    research_concurrency: int = Field(default=2)

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]

    @property
    def report_model(self) -> str:
        return self.llm_report_model or self.llm_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
