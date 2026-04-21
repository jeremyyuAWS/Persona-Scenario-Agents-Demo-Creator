"""Pydantic models for the Demo Blueprint and API contracts.

These mirror section 6 of the PRD. Validation rules also encode the
PRD's quality bars (e.g., no generic personas like "Manager",
no "AI Assistant" agents, 2-3 personas, 2-3 scenarios, 4-6 agents).
"""
from __future__ import annotations

import re
import uuid
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BlueprintStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class UpdateAction(str, Enum):
    REGENERATE_PERSONAS = "regenerate_personas"
    REGENERATE_SCENARIOS = "regenerate_scenarios"
    REGENERATE_AGENTS = "regenerate_agents"
    MODIFY = "modify"


# ---------------------------------------------------------------------------
# Quality filters (PRD 8.3 / 8.5)
# ---------------------------------------------------------------------------

_VAGUE_ROLES = {
    "manager",
    "employee",
    "worker",
    "staff",
    "user",
    "person",
    "people",
    "customer",  # too generic on its own; "retail customer browsing mobile app" is fine
}

_GENERIC_AGENT_NAMES = {
    "ai assistant",
    "assistant",
    "general agent",
    "ai agent",
    "chatbot",
    "bot",
    "helper",
}


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^p\d+$", description="Stable id like 'p1'.")
    name: str = Field(..., min_length=2, max_length=120)
    description: str = Field(..., min_length=15)
    goals: List[str] = Field(..., min_length=1)
    pain_points: List[str] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def reject_vague_role(cls, v: str) -> str:
        if _normalize(v) in _VAGUE_ROLES:
            raise ValueError(
                f"Persona name '{v}' is too generic. "
                "Use a specific role like 'Retail Store Manager' or 'Claims Adjuster'."
            )
        return v.strip()

    @field_validator("goals", "pain_points")
    @classmethod
    def non_empty_strings(cls, items: List[str]) -> List[str]:
        cleaned = [s.strip() for s in items if s and s.strip()]
        if not cleaned:
            raise ValueError("List must contain at least one non-empty string.")
        return cleaned


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^s\d+$", description="Stable id like 's1'.")
    title: str = Field(..., min_length=3, max_length=160)
    description: str = Field(..., min_length=20)
    actors: List[str] = Field(..., min_length=1, description="Persona ids that participate.")
    steps: List[str] = Field(..., min_length=2, description="Sequence of events.")

    @field_validator("actors")
    @classmethod
    def actor_id_shape(cls, ids: List[str]) -> List[str]:
        for a in ids:
            if not re.fullmatch(r"p\d+", a):
                raise ValueError(f"Actor id '{a}' must match persona id shape (e.g., 'p1').")
        return ids

    @field_validator("steps")
    @classmethod
    def steps_are_sentences(cls, steps: List[str]) -> List[str]:
        cleaned = [s.strip() for s in steps if s and s.strip()]
        if len(cleaned) < 2:
            raise ValueError("Scenario needs at least two concrete steps.")
        return cleaned


class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^a\d+$", description="Stable id like 'a1'.")
    name: str = Field(..., min_length=2, max_length=120)
    role: str = Field(..., min_length=5)
    responsibilities: List[str] = Field(..., min_length=1)
    inputs: List[str] = Field(..., min_length=1)
    outputs: List[str] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def reject_generic_agent(cls, v: str) -> str:
        if _normalize(v) in _GENERIC_AGENT_NAMES:
            raise ValueError(
                f"Agent name '{v}' is too generic. "
                "Use a functional name like 'Fraud Triage Agent' or 'Inventory Replenishment Agent'."
            )
        return v.strip()

    @field_validator("responsibilities", "inputs", "outputs")
    @classmethod
    def non_empty_strings(cls, items: List[str]) -> List[str]:
        cleaned = [s.strip() for s in items if s and s.strip()]
        if not cleaned:
            raise ValueError("List must contain at least one non-empty string.")
        return cleaned


class DemoBlueprint(BaseModel):
    """Top-level blueprint matching PRD section 6."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(..., min_length=3, max_length=200)
    industry: str = Field(..., min_length=2, max_length=80)
    status: BlueprintStatus = BlueprintStatus.DRAFT

    personas: List[Persona] = Field(..., min_length=2, max_length=3)
    scenarios: List[Scenario] = Field(..., min_length=2, max_length=3)
    agents: List[Agent] = Field(..., min_length=4, max_length=6)

    @model_validator(mode="after")
    def cross_check_actors(self) -> "DemoBlueprint":
        persona_ids = {p.id for p in self.personas}
        for s in self.scenarios:
            unknown = [a for a in s.actors if a not in persona_ids]
            if unknown:
                raise ValueError(
                    f"Scenario '{s.id}' references unknown persona ids: {unknown}. "
                    f"Known: {sorted(persona_ids)}"
                )
        return self

    @model_validator(mode="after")
    def unique_ids(self) -> "DemoBlueprint":
        for label, items in (
            ("persona", self.personas),
            ("scenario", self.scenarios),
            ("agent", self.agents),
        ):
            ids = [i.id for i in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"Duplicate {label} ids: {ids}")
        return self


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    use_case: str = Field(..., min_length=10, description="Free-form description.")
    company: Optional[str] = Field(None, max_length=120)


class GenerateResponse(BaseModel):
    session_id: str
    blueprint: DemoBlueprint


class UpdateRequest(BaseModel):
    session_id: str
    action: UpdateAction
    instructions: str = Field(
        default="",
        description="Required for `modify`; optional hint for regenerate_* actions.",
    )

    @model_validator(mode="after")
    def require_instructions_for_modify(self) -> "UpdateRequest":
        if self.action == UpdateAction.MODIFY and not self.instructions.strip():
            raise ValueError("`instructions` is required when action is 'modify'.")
        return self


class UpdateResponse(BaseModel):
    blueprint: DemoBlueprint


class ApproveRequest(BaseModel):
    session_id: str


class ApproveResponse(BaseModel):
    status: BlueprintStatus
    demo_id: str


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
