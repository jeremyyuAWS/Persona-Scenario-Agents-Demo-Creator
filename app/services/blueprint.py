"""Blueprint orchestration: call LLM (+ optional Tavily), validate, repair.

This is the seam between the HTTP layer and the LLM / research layer.
It owns:
  - building prompts
  - running the schema retry loop
  - assigning the top-level `id` and `status`
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from pydantic import ValidationError

from app.config import get_settings
from app.models.schema import (
    BlueprintStatus,
    DemoBlueprint,
    UpdateAction,
)
from app.services.llm import InvalidLLMJSON, LLMClient, build_llm_client
from app.services.prompts import build_generate_prompt, build_update_prompt
from app.services import tavily, tracing

logger = logging.getLogger(__name__)


class BlueprintGenerationError(Exception):
    """Raised when we cannot produce a schema-valid blueprint after retries."""


class BlueprintService:
    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self._llm = llm or build_llm_client()
        self._max_retries = get_settings().llm_max_json_retries

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------

    @tracing.observe(name="blueprint.generate")
    async def generate(
        self,
        use_case: str,
        company: Optional[str],
        trace_hints: Optional[Dict[str, Any]] = None,
    ) -> DemoBlueprint:
        if trace_hints:
            tracing.update_trace(**trace_hints)
        tracing.update_observation(
            input={"use_case": use_case, "company": company},
        )
        research = await tavily.enrich(use_case, company)
        base_prompt = build_generate_prompt(use_case, company, research)
        data = await self._call_with_schema_retry(
            is_update=False, user_prompt=base_prompt
        )
        blueprint = self._finalize(
            data, preserve_id=None, preserve_status=BlueprintStatus.DRAFT
        )
        tracing.update_observation(
            output={
                "blueprint_id": blueprint.id,
                "industry": blueprint.industry,
                "title": blueprint.title,
                "counts": {
                    "personas": len(blueprint.personas),
                    "scenarios": len(blueprint.scenarios),
                    "agents": len(blueprint.agents),
                },
            }
        )
        return blueprint

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    @tracing.observe(name="blueprint.update")
    async def update(
        self,
        blueprint: DemoBlueprint,
        action: UpdateAction,
        instructions: str,
        trace_hints: Optional[Dict[str, Any]] = None,
    ) -> DemoBlueprint:
        if trace_hints:
            tracing.update_trace(**trace_hints)
        tracing.update_observation(
            input={
                "blueprint_id": blueprint.id,
                "action": action.value,
                "instructions": instructions,
            }
        )
        base_prompt = build_update_prompt(blueprint, action, instructions)
        data = await self._call_with_schema_retry(
            is_update=True, user_prompt=base_prompt
        )
        # Preserve blueprint id + status across updates
        updated = self._finalize(
            data, preserve_id=blueprint.id, preserve_status=blueprint.status
        )
        tracing.update_observation(
            output={
                "blueprint_id": updated.id,
                "industry": updated.industry,
                "title": updated.title,
            }
        )
        return updated

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _call_with_schema_retry(
        self, *, is_update: bool, user_prompt: str
    ) -> Dict[str, Any]:
        """Call the LLM, validate against DemoBlueprint, retry with a corrective
        nudge if validation fails. JSON-extraction retries happen inside the
        LLM client; this loop handles SEMANTIC validation failures."""
        prompt = user_prompt
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                raw = (
                    await self._llm.update(prompt)
                    if is_update
                    else await self._llm.generate(prompt)
                )
            except InvalidLLMJSON as e:
                raise BlueprintGenerationError(str(e)) from e

            # Drop any server-owned fields the LLM tried to set.
            raw.pop("id", None)
            raw.pop("status", None)

            try:
                # Temporarily assign a placeholder id so validation runs; we
                # replace it in _finalize.
                candidate = {"id": str(uuid.uuid4()), "status": "draft", **raw}
                DemoBlueprint.model_validate(candidate)
                return raw
            except ValidationError as e:
                last_error = e
                logger.warning(
                    "blueprint.validation_failed",
                    extra={"attempt": attempt, "errors": e.errors()[:3]},
                )
                prompt = (
                    f"{user_prompt}\n\n"
                    "Your previous response failed schema validation with these "
                    f"errors: {json.dumps(e.errors()[:5])}. "
                    "Fix every field mentioned and return ONLY raw JSON."
                )
        raise BlueprintGenerationError(
            f"Blueprint failed schema validation after {self._max_retries} attempts: {last_error}"
        )

    def _finalize(
        self,
        data: Dict[str, Any],
        *,
        preserve_id: Optional[str],
        preserve_status: BlueprintStatus,
    ) -> DemoBlueprint:
        payload = {
            "id": preserve_id or str(uuid.uuid4()),
            "status": preserve_status.value,
            **data,
        }
        return DemoBlueprint.model_validate(payload)
