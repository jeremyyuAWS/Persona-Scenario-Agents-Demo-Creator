"""Prompt templates for blueprint generation and refinement.

Prompts enforce strict JSON output with no explanations, matching the
schema in app/models/schema.py. Updates always return the FULL blueprint
so the server has an atomic state to validate and store.
"""
from __future__ import annotations

import json
from typing import Optional

from app.models.schema import DemoBlueprint, UpdateAction


SYSTEM_GENERATE = """You are a senior enterprise solutions architect who designs
realistic AI demo blueprints for B2B software companies.

Your output is consumed directly by a frontend rendering engine, so it MUST be
valid JSON that strictly matches the schema provided. Do not wrap the JSON in
markdown fences, do not add comments, do not add any prose before or after.

Quality bar:
- Personas must reflect specific, real-world job titles (e.g. "Claims Adjuster
  at a mid-size P&C insurer"). Never use vague titles like "Manager", "User",
  or "Employee".
- Scenarios must describe concrete operational situations with a clear sequence
  of events (not abstract capabilities).
- Agents must have clearly defined functional roles that map to scenario steps.
  Never output generic names like "AI Assistant", "Chatbot", or "General Agent".
- The blueprint should feel like a real customer project, not a marketing page.
"""

SCHEMA_CONTRACT = """Return a single JSON object with EXACTLY these fields:

{
  "title": string,                        // demo title, <= 200 chars
  "industry": string,                     // e.g., "Retail", "Financial Services"
  "personas": [                           // 2 or 3 items
    {
      "id": "p1",                         // stable ids p1, p2, ...
      "name": string,                     // specific job title
      "description": string,              // >= 15 chars
      "goals": [string, ...],             // >= 1
      "pain_points": [string, ...]        // >= 1
    }
  ],
  "scenarios": [                          // 2 or 3 items
    {
      "id": "s1",
      "title": string,
      "description": string,              // >= 20 chars
      "actors": ["p1", ...],              // persona ids only
      "steps": [string, ...]              // >= 2 concrete steps
    }
  ],
  "agents": [                             // 4 to 6 items
    {
      "id": "a1",
      "name": string,                     // functional, not generic
      "role": string,                     // >= 5 chars
      "responsibilities": [string, ...],
      "inputs": [string, ...],
      "outputs": [string, ...]
    }
  ]
}

Do not include "id" or "status" at the top level - the server assigns those.
Do not include any other fields. Output raw JSON only.
"""


def build_generate_prompt(
    use_case: str,
    company: Optional[str] = None,
    research_summary: Optional[str] = None,
) -> str:
    blocks = [f"USE CASE:\n{use_case.strip()}"]
    if company:
        blocks.append(f"COMPANY CONTEXT:\n{company.strip()}")
    if research_summary:
        blocks.append(
            "EXTERNAL RESEARCH (summarized, may inform personas, workflows,"
            f" and pain points):\n{research_summary.strip()}"
        )
    blocks.append(SCHEMA_CONTRACT)
    blocks.append(
        "Generate the Demo Blueprint now. Remember: raw JSON only, no prose."
    )
    return "\n\n".join(blocks)


SYSTEM_UPDATE = """You are refining an existing Demo Blueprint based on a user
instruction from a reviewer. Preserve parts of the blueprint that are not
affected by the instruction. Maintain id stability where possible: keep existing
persona/scenario/agent ids when the item is unchanged, and mint new sequential
ids for anything added (e.g., if p1, p2 exist and the user asks for a new
persona, the new one is p3).

Output the COMPLETE updated blueprint as raw JSON that matches the schema.
No prose, no markdown fences."""


def build_update_prompt(
    blueprint: DemoBlueprint,
    action: UpdateAction,
    instructions: str,
) -> str:
    action_hints = {
        UpdateAction.REGENERATE_PERSONAS: (
            "Replace the `personas` list with 2-3 fresh, specific personas. "
            "You may refine scenarios so the new persona ids are referenced, "
            "but leave scenario ids and agents unchanged unless necessary."
        ),
        UpdateAction.REGENERATE_SCENARIOS: (
            "Replace the `scenarios` list with 2-3 fresh scenarios that use the "
            "existing persona ids. Leave personas and agents alone."
        ),
        UpdateAction.REGENERATE_AGENTS: (
            "Replace the `agents` list with 4-6 agents that map to steps in the "
            "existing scenarios. Leave personas and scenarios alone."
        ),
        UpdateAction.MODIFY: (
            "Apply the user's free-form instruction surgically. Only change what "
            "the instruction requires."
        ),
    }
    hint = action_hints[action]
    current = json.dumps(blueprint.model_dump(mode="json"), indent=2)
    parts = [
        f"ACTION: {action.value}",
        f"GUIDANCE: {hint}",
    ]
    if instructions.strip():
        parts.append(f"USER INSTRUCTION:\n{instructions.strip()}")
    parts.append(f"CURRENT BLUEPRINT:\n{current}")
    parts.append(SCHEMA_CONTRACT)
    parts.append("Return the full updated blueprint as raw JSON only.")
    return "\n\n".join(parts)
