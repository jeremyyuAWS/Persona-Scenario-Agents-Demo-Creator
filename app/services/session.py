"""In-memory session store (PRD v1 section 10).

Each session owns exactly one blueprint and its status. Swap this out for a
SQLite/Supabase-backed implementation in v2 - the interface is intentionally
narrow.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from app.models.schema import BlueprintStatus, DemoBlueprint


@dataclass
class Session:
    session_id: str
    blueprint: DemoBlueprint
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def status(self) -> BlueprintStatus:
        return self.blueprint.status


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}

    def create(self, blueprint: DemoBlueprint) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(session_id=session_id, blueprint=blueprint)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def update_blueprint(self, session_id: str, blueprint: DemoBlueprint) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.blueprint = blueprint
            session.updated_at = datetime.now(timezone.utc)
            return session

    def approve(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.blueprint = session.blueprint.model_copy(
                update={"status": BlueprintStatus.APPROVED}
            )
            session.updated_at = datetime.now(timezone.utc)
            return session

    def clear(self) -> None:
        """Test helper."""
        with self._lock:
            self._sessions.clear()


# Module-level singleton. FastAPI dependency functions return this.
_store = SessionStore()


def get_session_store() -> SessionStore:
    return _store
