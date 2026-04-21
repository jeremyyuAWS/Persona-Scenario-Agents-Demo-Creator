"""POST /update - refine an existing blueprint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.schema import BlueprintStatus, UpdateRequest, UpdateResponse
from app.services.blueprint import BlueprintGenerationError, BlueprintService
from app.services.session import SessionStore, get_session_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_blueprint_service(request: Request) -> BlueprintService:
    return request.app.state.blueprint_service


@router.post("/update", response_model=UpdateResponse)
async def update(
    body: UpdateRequest,
    store: SessionStore = Depends(get_session_store),
    service: BlueprintService = Depends(_get_blueprint_service),
) -> UpdateResponse:
    session = store.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session_id {body.session_id}")
    if session.blueprint.status == BlueprintStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail="Blueprint is already approved and cannot be modified.",
        )
    logger.info(
        "update.request",
        extra={"session_id": body.session_id, "action": body.action.value},
    )
    try:
        updated = await service.update(
            session.blueprint,
            body.action,
            body.instructions,
            trace_hints={
                "name": "update",
                "session_id": body.session_id,
                "tags": ["update", body.action.value],
                "metadata": {
                    "action": body.action.value,
                    "has_instructions": bool(body.instructions),
                },
            },
        )
    except BlueprintGenerationError as e:
        raise HTTPException(status_code=502, detail=f"LLM update failed: {e}")
    store.update_blueprint(body.session_id, updated)
    logger.info("update.ok", extra={"session_id": body.session_id})
    return UpdateResponse(blueprint=updated)
