"""End-to-end route tests using FastAPI TestClient.

The server runs in stub-LLM mode (no ANTHROPIC_API_KEY), so these tests
exercise the full generate / update / approve flow without network calls.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def retail_use_case() -> dict:
    return {
        "use_case": (
            "We want to pilot AI for a 40-store apparel retailer. Focus on "
            "replenishment and shrink detection at store level."
        ),
        "company": "Acme Apparel",
    }


def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["stub_llm"] is True


def test_generate_returns_valid_blueprint(client: TestClient, retail_use_case):
    resp = client.post("/generate", json=retail_use_case)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "session_id" in body
    bp = body["blueprint"]
    assert bp["status"] == "draft"
    assert bp["industry"] == "Retail"
    assert 2 <= len(bp["personas"]) <= 3
    assert 2 <= len(bp["scenarios"]) <= 3
    assert 4 <= len(bp["agents"]) <= 6


def test_generate_rejects_short_use_case(client: TestClient):
    resp = client.post("/generate", json={"use_case": "too short"})
    assert resp.status_code == 422


def test_retrieve_roundtrip(client: TestClient, retail_use_case):
    gen = client.post("/generate", json=retail_use_case).json()
    session_id = gen["session_id"]
    resp = client.get(f"/blueprint/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == gen["blueprint"]["id"]


def test_retrieve_unknown_session(client: TestClient):
    resp = client.get("/blueprint/does-not-exist")
    assert resp.status_code == 404


def test_update_regenerate_personas(client: TestClient, retail_use_case):
    gen = client.post("/generate", json=retail_use_case).json()
    session_id = gen["session_id"]
    resp = client.post(
        "/update",
        json={
            "session_id": session_id,
            "action": "regenerate_personas",
            "instructions": "",
        },
    )
    assert resp.status_code == 200, resp.text
    bp = resp.json()["blueprint"]
    # Blueprint id is preserved across updates.
    assert bp["id"] == gen["blueprint"]["id"]
    assert bp["status"] == "draft"


def test_update_modify_requires_instructions(client: TestClient, retail_use_case):
    gen = client.post("/generate", json=retail_use_case).json()
    resp = client.post(
        "/update",
        json={
            "session_id": gen["session_id"],
            "action": "modify",
            "instructions": "",
        },
    )
    assert resp.status_code == 422


def test_update_unknown_session(client: TestClient):
    resp = client.post(
        "/update",
        json={
            "session_id": "nope",
            "action": "regenerate_agents",
            "instructions": "",
        },
    )
    assert resp.status_code == 404


def test_approve_locks_blueprint(client: TestClient, retail_use_case):
    gen = client.post("/generate", json=retail_use_case).json()
    session_id = gen["session_id"]
    approve_resp = client.post("/approve", json={"session_id": session_id})
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["status"] == "approved"
    assert body["demo_id"] == gen["blueprint"]["id"]

    # Further updates should now be rejected.
    update_resp = client.post(
        "/update",
        json={
            "session_id": session_id,
            "action": "regenerate_personas",
            "instructions": "",
        },
    )
    assert update_resp.status_code == 409


@pytest.mark.parametrize(
    "use_case,expected_industry",
    [
        ("Our bank wants to automate fraud triage across debit, credit, and ACH.", "Financial Services"),
        ("Clinic operations: reduce no-show rate and streamline insurance pre-auth.", "Healthcare"),
        ("LTL logistics carrier routing, dock orchestration, and driver check-ins.", "Logistics"),
        ("Apparel retailer replenishment and shrink detection across stores.", "Retail"),
    ],
)
def test_generate_across_industries(client: TestClient, use_case, expected_industry):
    resp = client.post("/generate", json={"use_case": use_case})
    assert resp.status_code == 200, resp.text
    assert resp.json()["blueprint"]["industry"] == expected_industry


def test_modify_preserves_industry_and_ids(client: TestClient):
    """Regression: a previous bug caused `modify` to fall through to
    generate() with the schema-contract text, which mis-matched the industry."""
    gen = client.post(
        "/generate",
        json={
            "use_case": "Regional LTL carrier needs AI for day-of routing exceptions and dock orchestration.",
            "company": "Northern Freight",
        },
    ).json()
    session_id = gen["session_id"]
    assert gen["blueprint"]["industry"] == "Logistics"

    updated = client.post(
        "/update",
        json={
            "session_id": session_id,
            "action": "modify",
            "instructions": "Focus on holiday peak-season disruption",
        },
    ).json()["blueprint"]

    assert updated["industry"] == "Logistics"
    assert updated["id"] == gen["blueprint"]["id"]
    # Personas/scenarios/agents should survive a MODIFY in stub mode.
    assert [p["id"] for p in updated["personas"]] == [p["id"] for p in gen["blueprint"]["personas"]]
    assert [a["id"] for a in updated["agents"]] == [a["id"] for a in gen["blueprint"]["agents"]]
