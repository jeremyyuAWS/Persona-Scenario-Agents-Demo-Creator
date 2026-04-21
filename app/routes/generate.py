"""POST /generate - convert a free-form use case into a Demo Blueprint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.schema import GenerateRequest, GenerateResponse
from app.services.blueprint import BlueprintGenerationError, BlueprintService
from app.services.session import SessionStore, get_session_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_blueprint_service(request: Request) -> BlueprintService:
    return request.app.state.blueprint_service


@router.post("/generate", response_model=GenerateResponse)
async def generate(
    body: GenerateRequest,
    store: SessionStore = Depends(get_session_store),
    service: BlueprintService = Depends(_get_blueprint_service),
) -> GenerateResponse:
    logger.info(
        "generate.request",
        extra={"use_case_len": len(body.use_case), "company": body.company},
    )
    try:
        # `trace_hints` flows into the Langfuse trace from inside service.generate
        # (the root observation). Setting it from the route keeps the HTTP layer
        # the source of truth for trace-level metadata.
        blueprint = await service.generate(
            body.use_case,
            body.company,
            trace_hints={
                "name": "generate",
                "tags": ["generate"],
                "metadata": {
                    "use_case_len": len(body.use_case),
                    "has_company": bool(body.company),
                },
            },
        )
    except BlueprintGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM generation failed: {e}")
    session = store.create(blueprint)
    logger.info("generate.ok", extra={"session_id": session.session_id})
    return GenerateResponse(session_id=session.session_id, blueprint=blueprint)
