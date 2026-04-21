"""Langfuse tracing wrapper.

Design goals:
  - If no Langfuse keys are configured, every hook in this module is a no-op
    and nothing is imported from the `langfuse` package — so the service
    still works even if the library isn't installed.
  - If keys ARE configured, we expose `observe` as the real Langfuse
    decorator and `update_observation` / `update_trace` as thin wrappers
    around `langfuse_context`.
  - The rest of the codebase imports from `app.services.tracing` only —
    it never touches `langfuse` directly. That keeps the swap-in/swap-out
    surface small.

Usage:
    from app.services.tracing import observe, update_observation

    @observe(as_type="generation", name="anthropic.messages.create")
    async def _call(...):
        ...
        update_observation(model=..., usage=..., input=..., output=...)

Startup:
    tracing.init()     # once, from lifespan
    tracing.flush()    # on shutdown, or at end of a background job
"""
from __future__ import annotations

import functools
import logging
import os
from typing import Any, Callable

from app.config import get_settings

logger = logging.getLogger(__name__)

# Module-level flag set by init(). Until init() runs, tracing is off and
# every helper is a no-op — this covers test collection, import-time
# decoration, and the case where the user never set keys.
_ENABLED = False

# Populated by init() when the real SDK loads successfully.
_langfuse_context: Any = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    return _ENABLED


def init() -> bool:
    """Initialize Langfuse if keys are configured.

    Maps our settings onto the env vars the Langfuse SDK reads at import
    time, then imports the SDK. Returns True if tracing came up, False if
    it stayed off (missing keys, import failure, etc.).

    Safe to call more than once; subsequent calls are no-ops.
    """
    global _ENABLED, _langfuse_context
    if _ENABLED:
        return True

    settings = get_settings()
    if not settings.langfuse_enabled:
        logger.info("tracing.disabled", extra={"reason": "langfuse keys not set"})
        return False

    # The SDK reads these at import time. Set them explicitly so users can
    # keep using LANGFUSE_BASE_URL in their .env without the SDK caring.
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key or ""
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key or ""
    if settings.langfuse_host:
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host

    try:
        from langfuse.decorators import langfuse_context  # type: ignore
    except Exception as e:  # pragma: no cover — install-time misconfig only
        logger.warning("tracing.import_failed", extra={"error": str(e)})
        return False

    _langfuse_context = langfuse_context
    _ENABLED = True
    logger.info(
        "tracing.enabled",
        extra={"host": settings.langfuse_host or "cloud-default"},
    )
    return True


def flush() -> None:
    """Flush pending spans. Call on shutdown / before a short-lived
    process exits so nothing is dropped."""
    if not _ENABLED or _langfuse_context is None:
        return
    try:
        _langfuse_context.flush()
    except Exception as e:  # never let a flush bug break shutdown
        logger.warning("tracing.flush_failed", extra={"error": str(e)})


def observe(**observe_kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that becomes a real Langfuse @observe when tracing is on.

    When tracing is off (no keys, or init() never ran), it returns the
    wrapped function untouched. We resolve the decision at *call time* —
    not at import time — because `init()` runs inside FastAPI's lifespan,
    which fires after module import.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        # Cache the real decorated version so we only pay the wrapping
        # cost once per function.
        _decorated: dict[str, Callable[..., Any]] = {}

        def _resolve() -> Callable[..., Any]:
            if not _ENABLED:
                return fn
            if "real" not in _decorated:
                from langfuse.decorators import observe as _observe  # type: ignore

                _decorated["real"] = _observe(**observe_kwargs)(fn)
            return _decorated["real"]

        import inspect

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _resolve()(*args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return _resolve()(*args, **kwargs)

        return sync_wrapper

    return decorator


def update_observation(**fields: Any) -> None:
    """Attach metadata to the current observation (model, usage, input,
    output, model_parameters, level, status_message, ...)."""
    if not _ENABLED or _langfuse_context is None:
        return
    try:
        _langfuse_context.update_current_observation(**fields)
    except Exception as e:  # don't let tracing break the request
        logger.debug("tracing.update_observation_failed", extra={"error": str(e)})


def update_trace(**fields: Any) -> None:
    """Attach metadata to the current trace (name, session_id, user_id,
    tags, metadata, release, ...)."""
    if not _ENABLED or _langfuse_context is None:
        return
    try:
        _langfuse_context.update_current_trace(**fields)
    except Exception as e:
        logger.debug("tracing.update_trace_failed", extra={"error": str(e)})
