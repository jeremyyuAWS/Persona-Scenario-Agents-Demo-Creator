"""Optional research enrichment via Tavily.

Called when a company is provided or when the use case is vague. Results are
summarized to a short block before being handed to the LLM prompt. If no key
is configured, the service short-circuits and returns None.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import get_settings
from app.services import tracing

logger = logging.getLogger(__name__)


TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_RESULTS = 5
_VAGUENESS_THRESHOLD_CHARS = 80  # PRD 8.2: enrich if the use case is short/vague


def _is_vague(use_case: str) -> bool:
    stripped = use_case.strip()
    # Short, keyword-ish input counts as vague.
    return len(stripped) < _VAGUENESS_THRESHOLD_CHARS or stripped.count(" ") < 6


def should_enrich(use_case: str, company: Optional[str]) -> bool:
    return bool(company) or _is_vague(use_case)


def _build_query(use_case: str, company: Optional[str]) -> str:
    subject = (company or use_case).strip()
    return f"{subject} workflows challenges operations"


def _summarize(results: list[dict]) -> str:
    lines = []
    for r in results[:MAX_RESULTS]:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or r.get("snippet") or "").strip()
        if not title and not content:
            continue
        snippet = content[:300].replace("\n", " ")
        lines.append(f"- {title}: {snippet}")
    return "\n".join(lines) if lines else ""


@tracing.observe(name="tavily.enrich")
async def enrich(use_case: str, company: Optional[str]) -> Optional[str]:
    settings = get_settings()
    if not settings.use_tavily or not should_enrich(use_case, company):
        tracing.update_observation(
            input={"use_case": use_case, "company": company},
            output=None,
            metadata={"skipped": True, "reason": "disabled or use_case not vague"},
        )
        return None
    query = _build_query(use_case, company)
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": MAX_RESULTS,
        "search_depth": "basic",
        "include_answer": False,
    }
    logger.info("tavily.query", extra={"query": query})
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(TAVILY_ENDPOINT, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:  # network or bad JSON
        logger.warning("tavily.error", extra={"error": str(e)})
        tracing.update_observation(
            input={"query": query},
            level="WARNING",
            status_message=f"tavily request failed: {e}",
            metadata={"skipped": True, "reason": "request_failed"},
        )
        return None
    results = data.get("results", [])
    summary = _summarize(results) or None
    tracing.update_observation(
        input={"query": query},
        output=summary,
        metadata={"num_results": len(results), "skipped": False},
    )
    return summary
