"""Schema validation tests - enforces the PRD quality bars."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schema import Agent, DemoBlueprint, Persona, Scenario


def _sample_persona(idx: int = 1, name: str = "Claims Adjuster at a Regional Health Insurer") -> dict:
    return {
        "id": f"p{idx}",
        "name": name,
        "description": "Reviews inbound claims for medical necessity.",
        "goals": ["Close clean claims in 48h"],
        "pain_points": ["Manual EHR lookups"],
    }


def _sample_scenario(idx: int = 1, actors=("p1",)) -> dict:
    return {
        "id": f"s{idx}",
        "title": "High-dollar claim missing documentation",
        "description": "A large inpatient claim arrives without the full operative report.",
        "actors": list(actors),
        "steps": ["Claim is ingested", "Gap is detected"],
    }


def _sample_agent(idx: int = 1, name: str = "Claims Completeness Agent") -> dict:
    return {
        "id": f"a{idx}",
        "name": name,
        "role": "Validates that each incoming claim has all required documents.",
        "responsibilities": ["Parse claims"],
        "inputs": ["Claim JSON"],
        "outputs": ["Completeness report"],
    }


def _sample_blueprint() -> dict:
    return {
        "title": "Healthcare Claims Demo",
        "industry": "Healthcare",
        "status": "draft",
        "personas": [_sample_persona(1), _sample_persona(2, "Care Coordinator at a Multi-Specialty Clinic")],
        "scenarios": [_sample_scenario(1, actors=["p1"]), _sample_scenario(2, actors=["p2"])],
        "agents": [_sample_agent(i, f"Agent {i} Role") for i in range(1, 5)],
    }


class TestPersona:
    def test_rejects_vague_role_name(self):
        with pytest.raises(ValidationError, match="too generic"):
            Persona(**_sample_persona(name="Manager"))

    def test_accepts_specific_role(self):
        p = Persona(**_sample_persona(name="Fraud Operations Analyst at a Mid-Market Bank"))
        assert "Analyst" in p.name

    def test_requires_persona_id_shape(self):
        data = _sample_persona()
        data["id"] = "persona-1"
        with pytest.raises(ValidationError):
            Persona(**data)


class TestAgent:
    @pytest.mark.parametrize("bad_name", ["AI Assistant", "assistant", "General Agent", "chatbot"])
    def test_rejects_generic_agent_names(self, bad_name):
        with pytest.raises(ValidationError, match="too generic"):
            Agent(**_sample_agent(name=bad_name))

    def test_accepts_functional_agent(self):
        a = Agent(**_sample_agent(name="Fraud Triage Agent"))
        assert a.name == "Fraud Triage Agent"


class TestScenario:
    def test_requires_at_least_two_steps(self):
        data = _sample_scenario()
        data["steps"] = ["Only one step"]
        with pytest.raises(ValidationError):
            Scenario(**data)

    def test_rejects_bad_actor_id(self):
        data = _sample_scenario(actors=["persona-1"])
        with pytest.raises(ValidationError, match="persona id shape"):
            Scenario(**data)


class TestBlueprint:
    def test_valid_blueprint_passes(self):
        bp = DemoBlueprint(id="00000000-0000-0000-0000-000000000001", **_sample_blueprint())
        assert bp.status.value == "draft"
        assert len(bp.personas) == 2
        assert len(bp.agents) == 4

    def test_rejects_scenario_referencing_unknown_persona(self):
        data = _sample_blueprint()
        data["scenarios"][0]["actors"] = ["p9"]
        with pytest.raises(ValidationError, match="unknown persona ids"):
            DemoBlueprint(id="00000000-0000-0000-0000-000000000002", **data)

    def test_rejects_duplicate_ids(self):
        data = _sample_blueprint()
        data["personas"][1]["id"] = "p1"
        data["scenarios"][1]["actors"] = ["p1"]
        with pytest.raises(ValidationError, match="Duplicate persona ids"):
            DemoBlueprint(id="00000000-0000-0000-0000-000000000003", **data)

    def test_enforces_agent_count_bounds(self):
        data = _sample_blueprint()
        data["agents"] = data["agents"][:3]  # only 3 agents, PRD requires 4-6
        with pytest.raises(ValidationError):
            DemoBlueprint(id="00000000-0000-0000-0000-000000000004", **data)

    def test_enforces_persona_count_bounds(self):
        data = _sample_blueprint()
        data["personas"] = [data["personas"][0]]  # only 1 persona
        data["scenarios"][1]["actors"] = ["p1"]
        with pytest.raises(ValidationError):
            DemoBlueprint(id="00000000-0000-0000-0000-000000000005", **data)
