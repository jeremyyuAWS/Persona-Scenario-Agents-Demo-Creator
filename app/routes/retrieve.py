"""GET /blueprint/{session_id} - fetch the current blueprint for a session."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.models.schema import DemoBlueprint
from app.services.session import SessionStore, get_session_store

router = APIRouter()


@router.get("/blueprint/{session_id}", response_model=DemoBlueprint)
async def retrieve(
    session_id: str,
    store: SessionStore = Depends(get_session_store),
) -> DemoBlueprint:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session_id {session_id}")
    return session.blueprint
