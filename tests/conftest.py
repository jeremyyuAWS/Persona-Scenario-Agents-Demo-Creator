"""Shared pytest fixtures.

Tests must be deterministic and never hit external APIs. Real keys in a
local .env would otherwise make `/generate` call Claude (slow) and return
outputs that differ from the stub templates the tests assert against.

The env-clearing block runs at the very top of this module so that it
executes before `app.main` is imported (which chains into `app.config`
and calls `load_dotenv`). `load_dotenv()` without `override=True` will
not overwrite keys that are already set in `os.environ` — even if set
to the empty string — so this effectively disables .env for test runs.
"""
from __future__ import annotations

import os

# Force stub mode *before* any `app.*` module is imported.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""
# Disable Langfuse in tests so we never accidentally hit a running instance
# or block on network during collection.
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["LANGFUSE_HOST"] = ""
os.environ["LANGFUSE_BASE_URL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.session import get_session_store  # noqa: E402

# If anything populated the settings cache before the env override landed
# (unlikely, but cheap insurance), drop it so the first use in a test
# rebuilds settings with our cleared keys.
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _assert_stub_mode() -> None:
    """Guardrail: fail loudly if a test somehow escapes stub mode."""
    settings = get_settings()
    assert settings.use_stub_llm, (
        "Tests must run in stub mode. ANTHROPIC_API_KEY leaked in via .env "
        "or the environment — check tests/conftest.py."
    )
    assert not settings.langfuse_enabled, (
        "Tests must run with Langfuse disabled. LANGFUSE_* keys leaked in via "
        ".env or the environment — check tests/conftest.py."
    )


@pytest.fixture
def client() -> TestClient:
    store = get_session_store()
    store.clear()
    with TestClient(app) as c:
        yield c
    store.clear()
