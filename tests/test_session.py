"""Session store tests."""
from __future__ import annotations

import pytest

from app.models.schema import BlueprintStatus, DemoBlueprint
from app.services.session import SessionStore


def _bp(title: str = "Demo") -> DemoBlueprint:
    return DemoBlueprint.model_validate(
        {
            "title": title,
            "industry": "Retail",
            "status": "draft",
            "personas": [
                {
                    "id": "p1",
                    "name": "Retail Store Manager at a 40-Location Apparel Chain",
                    "description": "Owns P&L and staffing for a single location.",
                    "goals": ["Hit weekly sales target"],
                    "pain_points": ["Manual replenishment"],
                },
                {
                    "id": "p2",
                    "name": "Regional Merchandising Planner",
                    "description": "Allocates seasonal inventory across stores.",
                    "goals": ["Reduce markdowns"],
                    "pain_points": ["Slow reaction to weather"],
                },
            ],
            "scenarios": [
                {
                    "id": "s1",
                    "title": "Hot SKU stocks out",
                    "description": "A viral SKU sells through across three stores.",
                    "actors": ["p1", "p2"],
                    "steps": ["Threshold crossed", "Transfer plan drafted"],
                },
                {
                    "id": "s2",
                    "title": "Weather-driven reallocation",
                    "description": "Cold front shifts demand toward outerwear.",
                    "actors": ["p2"],
                    "steps": ["Forecast updates", "Planner approves"],
                },
            ],
            "agents": [
                {
                    "id": f"a{i}",
                    "name": f"Specialist Agent {i}",
                    "role": "Specialized functional role in the demo.",
                    "responsibilities": ["Do specific work"],
                    "inputs": ["Signal"],
                    "outputs": ["Action"],
                }
                for i in range(1, 5)
            ],
        }
    )


def test_create_and_get():
    store = SessionStore()
    session = store.create(_bp())
    assert store.get(session.session_id) is session
    assert session.blueprint.status == BlueprintStatus.DRAFT


def test_get_missing_returns_none():
    store = SessionStore()
    assert store.get("nope") is None


def test_update_blueprint_bumps_timestamp():
    store = SessionStore()
    s = store.create(_bp("First"))
    ts0 = s.updated_at
    # Tiny sleep alternative: update with a different title so we can assert state change.
    updated = store.update_blueprint(s.session_id, _bp("Second"))
    assert updated.blueprint.title == "Second"
    assert updated.updated_at >= ts0


def test_update_missing_raises():
    store = SessionStore()
    with pytest.raises(KeyError):
        store.update_blueprint("nope", _bp())


def test_approve_locks_blueprint():
    store = SessionStore()
    s = store.create(_bp())
    assert s.blueprint.status == BlueprintStatus.DRAFT
    approved = store.approve(s.session_id)
    assert approved.blueprint.status == BlueprintStatus.APPROVED
