"""Centralized configuration loaded from environment variables.

The service is designed to run without any API keys by falling back to
a deterministic stub LLM. This keeps local development and tests fast.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # LLM
    anthropic_api_key: str | None
    anthropic_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_max_json_retries: int

    # Tavily
    tavily_api_key: str | None

    # Langfuse
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None

    # Server
    host: str
    port: int

    @property
    def use_stub_llm(self) -> bool:
        return not self.anthropic_api_key

    @property
    def use_tavily(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def langfuse_enabled(self) -> bool:
        # Both keys required — secret alone won't authenticate, and public
        # alone can't sign. Host is optional (SDK defaults to cloud).
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Accept LANGFUSE_BASE_URL as an alias for LANGFUSE_HOST so the .env
    # can use whichever name the user recognizes. LANGFUSE_HOST wins if set
    # (that's what the Langfuse SDK itself reads).
    langfuse_host = (
        os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or None
    )
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4000")),
        llm_max_json_retries=int(os.getenv("LLM_MAX_JSON_RETRIES", "3")),
        tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY") or None,
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY") or None,
        langfuse_host=langfuse_host,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )
