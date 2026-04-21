"""POST /approve - lock the blueprint and return a demo_id."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.models.schema import ApproveRequest, ApproveResponse
from app.services.session import SessionStore, get_session_store

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/approve", response_model=ApproveResponse)
async def approve(
    body: ApproveRequest,
    store: SessionStore = Depends(get_session_store),
) -> ApproveResponse:
    session = store.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session_id {body.session_id}")
    approved = store.approve(body.session_id)
    logger.info(
        "approve.ok",
        extra={"session_id": body.session_id, "demo_id": approved.blueprint.id},
    )
    return ApproveResponse(status=approved.status, demo_id=approved.blueprint.id)
