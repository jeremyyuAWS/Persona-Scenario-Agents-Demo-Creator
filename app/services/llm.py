"""LLM client abstraction with JSON-retry semantics and a deterministic stub.

The service can run in two modes:
  - Live: if ANTHROPIC_API_KEY is set, calls Claude via the official SDK.
  - Stub: otherwise, synthesizes a realistic-ish blueprint from keyword rules.
    This lets the whole stack boot and pass tests without any credentials.

Either way, the returned value is a parsed JSON dict that conforms to the
DemoBlueprint contract (sans `id` and `status`, which the orchestrator sets).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Protocol

from app.config import get_settings
from app.services import tracing
from app.services.prompts import SYSTEM_GENERATE, SYSTEM_UPDATE

logger = logging.getLogger(__name__)


class InvalidLLMJSON(Exception):
    """Raised when the LLM keeps returning non-JSON after all retries."""


class LLMClient(Protocol):
    async def generate(self, user_prompt: str) -> Dict[str, Any]: ...
    async def update(self, user_prompt: str) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _first_balanced_object(text: str) -> Optional[str]:
    """Return the first top-level balanced {...} object found in `text`.

    Uses brace counting (string-aware) rather than a greedy regex so it
    can pick out an embedded blueprint from a prompt that contains
    multiple JSON-looking blocks (e.g. a schema example).
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON parse. Strips common LLM noise (fences, prose)."""
    if not text or not text.strip():
        raise ValueError("Empty LLM response.")
    cleaned = text.strip()
    # Strip markdown fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the first top-level object in the text
        match = _JSON_BLOCK.search(cleaned)
        if not match:
            raise ValueError(f"No JSON object found in LLM response: {cleaned[:200]}...")
        return json.loads(match.group(0))


# ---------------------------------------------------------------------------
# Anthropic-backed client
# ---------------------------------------------------------------------------


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, temperature: float, max_tokens: int,
                 max_json_retries: int) -> None:
        # Lazy import so the package isn't required for stub-mode use.
        from anthropic import AsyncAnthropic  # type: ignore

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_json_retries = max_json_retries

    @tracing.observe(as_type="generation", name="anthropic.messages.create")
    async def _call(self, system: str, user: str) -> str:
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate text blocks
        parts = []
        for block in message.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        output = "".join(parts)

        # Attach Langfuse-native generation metadata. `usage` in the
        # Langfuse schema uses input/output/total keys; the Anthropic SDK
        # returns `input_tokens` / `output_tokens`.
        usage = getattr(message, "usage", None)
        usage_dict: Dict[str, Any] = {}
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            usage_dict = {
                "input": input_tokens,
                "output": output_tokens,
                "total": (
                    (input_tokens or 0) + (output_tokens or 0)
                    if input_tokens is not None or output_tokens is not None
                    else None
                ),
                "unit": "TOKENS",
            }
        tracing.update_observation(
            model=self._model,
            input={"system": system, "user": user},
            output=output,
            usage=usage_dict or None,
            model_parameters={
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
            },
        )
        return output

    @tracing.observe(name="anthropic.call_with_retry")
    async def _call_with_retry(self, system: str, user: str) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        prompt = user
        for attempt in range(1, self._max_json_retries + 1):
            try:
                raw = await self._call(system, prompt)
                logger.info(
                    "llm.response", extra={"attempt": attempt, "chars": len(raw)}
                )
                parsed = _extract_json(raw)
                tracing.update_observation(
                    metadata={
                        "attempts": attempt,
                        "max_attempts": self._max_json_retries,
                        "outcome": "ok",
                    }
                )
                return parsed
            except (ValueError, json.JSONDecodeError) as e:
                last_err = e
                logger.warning(
                    "llm.invalid_json", extra={"attempt": attempt, "error": str(e)}
                )
                # Ask the model to correct itself
                prompt = (
                    f"{user}\n\n"
                    "Your previous response was not valid JSON matching the schema. "
                    f"Error: {e}. Return ONLY raw JSON this time."
                )
        tracing.update_observation(
            level="ERROR",
            status_message=f"invalid JSON after {self._max_json_retries} attempts",
            metadata={
                "attempts": self._max_json_retries,
                "max_attempts": self._max_json_retries,
                "outcome": "gave_up",
                "last_error": str(last_err),
            },
        )
        raise InvalidLLMJSON(
            f"LLM returned invalid JSON after {self._max_json_retries} attempts: {last_err}"
        )

    async def generate(self, user_prompt: str) -> Dict[str, Any]:
        return await self._call_with_retry(SYSTEM_GENERATE, user_prompt)

    async def update(self, user_prompt: str) -> Dict[str, Any]:
        return await self._call_with_retry(SYSTEM_UPDATE, user_prompt)


# ---------------------------------------------------------------------------
# Deterministic stub client
# ---------------------------------------------------------------------------


_INDUSTRY_KEYWORDS = [
    ("healthcare", ["hospital", "clinic", "patient", "ehr", "medical", "nurse", "physician",
                    "healthcare", "insurance claim", "pharmacy"]),
    ("financial services", ["bank", "finance", "fraud", "loan", "credit", "underwriting",
                            "kyc", "aml", "trading", "wealth", "brokerage"]),
    ("retail", ["retail", "store", "ecommerce", "checkout", "shopper", "merchandising",
                "sku", "inventory", "point of sale", "pos"]),
    ("logistics", ["logistics", "supply chain", "warehouse", "shipment", "fleet",
                   "delivery", "carrier", "freight", "route"]),
    ("manufacturing", ["manufacturing", "factory", "plant", "assembly", "defect"]),
    ("technology", ["saas", "developer", "devops", "incident", "cloud"]),
]


def _guess_industry(text: str) -> str:
    lowered = text.lower()
    for industry, kws in _INDUSTRY_KEYWORDS:
        if any(kw in lowered for kw in kws):
            return industry.title()
    return "Enterprise Operations"


_TEMPLATES = {
    "Healthcare": {
        "title": "Clinical Intake & Claims Triage Demo",
        "personas": [
            {
                "id": "p1",
                "name": "Claims Adjuster at a Regional Health Insurer",
                "description": "Reviews 60+ inbound claims per day for medical necessity and coding accuracy.",
                "goals": ["Close clean claims in under 48 hours", "Flag high-risk claims for clinical review"],
                "pain_points": ["Manual EHR lookups", "Inconsistent CPT coding across providers"],
            },
            {
                "id": "p2",
                "name": "Care Coordinator at a Multi-Specialty Clinic",
                "description": "Owns patient handoffs between primary care, specialists, and billing.",
                "goals": ["Reduce no-show rate", "Shorten time from referral to scheduled visit"],
                "pain_points": ["Insurance pre-auth delays", "Fragmented notes across systems"],
            },
        ],
        "scenarios": [
            {
                "id": "s1",
                "title": "High-dollar claim arrives with missing documentation",
                "description": "A $47k inpatient claim is submitted without the full operative report, blocking payment.",
                "actors": ["p1"],
                "steps": [
                    "Claim is ingested from the provider portal.",
                    "System detects missing operative report and flags the claim.",
                    "Provider is auto-notified with a specific document request.",
                    "Adjuster receives a summary once documentation arrives.",
                ],
            },
            {
                "id": "s2",
                "title": "Specialist referral with pre-auth complication",
                "description": "A patient is referred to cardiology but the insurer requires prior authorization for the requested echocardiogram.",
                "actors": ["p2"],
                "steps": [
                    "Referral is created in the EHR.",
                    "System checks insurer pre-auth rules and flags the procedure.",
                    "Pre-auth packet is auto-assembled from clinical notes.",
                    "Care coordinator reviews and submits; patient is scheduled on approval.",
                ],
            },
        ],
        "agents": [
            {"id": "a1", "name": "Claims Completeness Agent", "role": "Validates that each incoming claim has all required clinical and billing documents.",
             "responsibilities": ["Parse claim payloads", "Detect missing documentation", "Generate provider requests"],
             "inputs": ["Claim JSON", "Provider document registry"], "outputs": ["Completeness report", "Provider notification"]},
            {"id": "a2", "name": "CPT Coding Audit Agent", "role": "Cross-checks procedure codes against clinical notes and payer rules.",
             "responsibilities": ["Match codes to notes", "Flag unbundling risks"], "inputs": ["Clinical notes", "Claim codes"], "outputs": ["Coding audit findings"]},
            {"id": "a3", "name": "Pre-Authorization Assembly Agent", "role": "Builds pre-auth packets from EHR data for specialist referrals.",
             "responsibilities": ["Assemble clinical justification", "Fill payer-specific forms"], "inputs": ["EHR notes", "Payer rulebook"], "outputs": ["Pre-auth packet"]},
            {"id": "a4", "name": "Referral Scheduling Agent", "role": "Coordinates appointment scheduling once pre-auth is resolved.",
             "responsibilities": ["Match patient availability to specialist slots", "Send reminders"], "inputs": ["Patient preferences", "Specialist calendar"], "outputs": ["Confirmed appointment"]},
        ],
    },
    "Financial Services": {
        "title": "Fraud & Loan Operations Demo",
        "personas": [
            {"id": "p1", "name": "Fraud Operations Analyst at a Mid-Market Bank",
             "description": "Triages 200+ fraud alerts per shift across debit, credit, and ACH channels.",
             "goals": ["Cut false-positive rate", "Resolve confirmed fraud in under 15 minutes"],
             "pain_points": ["Alerts spread across three dashboards", "Noisy rules-based flags"]},
            {"id": "p2", "name": "Commercial Loan Underwriter",
             "description": "Reviews SMB loan applications between $100k and $2M.",
             "goals": ["Shorten time-to-decision", "Stay within credit policy"],
             "pain_points": ["Manual tax return parsing", "Covenant tracking in spreadsheets"]},
        ],
        "scenarios": [
            {"id": "s1", "title": "Suspicious card-not-present surge overnight",
             "description": "A burst of CNP transactions triggers 38 alerts tied to the same BIN range.",
             "actors": ["p1"],
             "steps": [
                 "Overnight alerts are clustered by device fingerprint.",
                 "System proposes a single investigation with the top 5 representative alerts.",
                 "Analyst confirms fraud pattern; remaining alerts are auto-dispositioned.",
                 "Card reissue workflow is triggered for affected customers.",
             ]},
            {"id": "s2", "title": "SMB loan with ambiguous cash flow",
             "description": "A $650k working-capital request shows inconsistent revenue across bank statements and tax returns.",
             "actors": ["p2"],
             "steps": [
                 "Application and documents land in the underwriting queue.",
                 "System normalizes statements and highlights reconciliation gaps.",
                 "Underwriter reviews the flagged variance with source citations.",
                 "Decision memo is drafted with policy-compliant rationale.",
             ]},
        ],
        "agents": [
            {"id": "a1", "name": "Fraud Alert Clustering Agent", "role": "Groups related alerts by device, merchant, and behavioral signals.",
             "responsibilities": ["Deduplicate alerts", "Rank clusters by loss exposure"], "inputs": ["Alert stream"], "outputs": ["Investigation bundles"]},
            {"id": "a2", "name": "Card Reissue Orchestration Agent", "role": "Executes downstream reissue workflows when fraud is confirmed.",
             "responsibilities": ["Queue reissue requests", "Notify affected customers"], "inputs": ["Confirmed fraud events"], "outputs": ["Reissue tickets"]},
            {"id": "a3", "name": "Statement Normalization Agent", "role": "Parses bank statements and tax returns into a unified cash-flow view.",
             "responsibilities": ["Extract line items", "Reconcile across sources"], "inputs": ["PDF statements", "Tax returns"], "outputs": ["Normalized cash-flow summary"]},
            {"id": "a4", "name": "Underwriting Memo Agent", "role": "Drafts credit memos grounded in policy and application data.",
             "responsibilities": ["Cite source documents", "Apply policy checklists"], "inputs": ["Normalized financials", "Credit policy"], "outputs": ["Draft decision memo"]},
        ],
    },
    "Retail": {
        "title": "Store Operations & Replenishment Demo",
        "personas": [
            {"id": "p1", "name": "Retail Store Manager at a 40-Location Apparel Chain",
             "description": "Owns P&L, staffing, and inventory for a single store location.",
             "goals": ["Hit weekly sales target", "Keep stock-outs on top-30 SKUs under 2%"],
             "pain_points": ["Manual replenishment guesswork", "Late visibility into shrink"]},
            {"id": "p2", "name": "Regional Merchandising Planner",
             "description": "Allocates seasonal inventory across 40 stores.",
             "goals": ["Reduce end-of-season markdowns", "Match allocation to local demand"],
             "pain_points": ["Planning based on last-year data only", "Slow reaction to weather shifts"]},
        ],
        "scenarios": [
            {"id": "s1", "title": "Hot-selling SKU stocks out at three stores",
             "description": "A viral denim SKU sells through at three stores within 48 hours of launch.",
             "actors": ["p1", "p2"],
             "steps": [
                 "Sell-through signal crosses a dynamic threshold.",
                 "System drafts a transfer plan from under-performing stores.",
                 "Store manager confirms receipt capacity; transfer is booked.",
                 "Planner is notified for next allocation wave.",
             ]},
            {"id": "s2", "title": "Weather shift forces allocation rethink",
             "description": "An unseasonal cold front arrives, shifting demand toward outerwear.",
             "actors": ["p2"],
             "steps": [
                 "Weather feed updates regional temperature forecast.",
                 "System reprojects demand for outerwear categories.",
                 "Planner reviews suggested reallocation and approves transfers.",
             ]},
        ],
        "agents": [
            {"id": "a1", "name": "Inventory Replenishment Agent", "role": "Computes per-store replenishment recommendations based on sell-through.",
             "responsibilities": ["Forecast short-horizon demand", "Propose transfers"], "inputs": ["POS stream", "On-hand by store"], "outputs": ["Transfer recommendations"]},
            {"id": "a2", "name": "Demand Sensing Agent", "role": "Incorporates weather and local events into category demand.",
             "responsibilities": ["Blend external signals into forecasts"], "inputs": ["Weather feed", "Local events"], "outputs": ["Adjusted demand projections"]},
            {"id": "a3", "name": "Store Task Dispatch Agent", "role": "Assigns receiving, markdown, and visual tasks to store staff.",
             "responsibilities": ["Prioritize tasks by impact"], "inputs": ["Task queue", "Staff schedule"], "outputs": ["Staff task list"]},
            {"id": "a4", "name": "Shrink Anomaly Agent", "role": "Detects shrink patterns from POS and inventory counts.",
             "responsibilities": ["Flag suspect transactions", "Surface cycle-count gaps"], "inputs": ["POS data", "Cycle counts"], "outputs": ["Shrink alerts"]},
        ],
    },
    "Logistics": {
        "title": "Fleet Operations & Exception Handling Demo",
        "personas": [
            {"id": "p1", "name": "Dispatcher at a Regional LTL Carrier",
             "description": "Owns day-of routing and exception handling for 80 trucks across three hubs.",
             "goals": ["Keep on-time delivery above 96%", "Avoid empty miles"],
             "pain_points": ["Manual rerouting during weather events", "Late driver check-ins"]},
            {"id": "p2", "name": "Warehouse Shift Supervisor",
             "description": "Runs the dock during the 2am-10am shift.",
             "goals": ["Hit loading SLAs", "Minimize dock congestion"],
             "pain_points": ["Unexpected trailer arrivals", "Labor shortages"]},
        ],
        "scenarios": [
            {"id": "s1", "title": "Weather event closes I-80 mid-route",
             "description": "A snowstorm closes a key interstate with 12 active loads on or near the route.",
             "actors": ["p1"],
             "steps": [
                 "Traffic feed triggers a route-impact alert.",
                 "System proposes alternate routes with ETA deltas.",
                 "Dispatcher approves reroutes; customers are notified automatically.",
             ]},
            {"id": "s2", "title": "Overbooked dock at 5am",
             "description": "Three inbound trailers arrive within 20 minutes of each other without prior notice.",
             "actors": ["p2"],
             "steps": [
                 "Yard camera feed detects unscheduled arrivals.",
                 "System reassigns dock doors based on priority freight.",
                 "Supervisor is handed a reshuffled dock plan.",
             ]},
        ],
        "agents": [
            {"id": "a1", "name": "Route Reoptimization Agent", "role": "Proposes alternate routes when an exception invalidates the current plan.",
             "responsibilities": ["Score alternate routes", "Estimate ETA deltas"], "inputs": ["Current routes", "Traffic/weather feeds"], "outputs": ["Route proposals"]},
            {"id": "a2", "name": "Customer Notification Agent", "role": "Keeps shippers and consignees informed of changes in transit.",
             "responsibilities": ["Select appropriate channel", "Personalize message"], "inputs": ["Delivery events"], "outputs": ["Customer messages"]},
            {"id": "a3", "name": "Dock Orchestration Agent", "role": "Re-sequences dock door assignments when arrivals deviate from plan.",
             "responsibilities": ["Reassign doors by priority"], "inputs": ["Appointment schedule", "Yard arrivals"], "outputs": ["Updated dock plan"]},
            {"id": "a4", "name": "Driver Check-In Agent", "role": "Monitors driver status and flags missed check-ins.",
             "responsibilities": ["Escalate overdue check-ins"], "inputs": ["Telematics feed"], "outputs": ["Check-in alerts"]},
        ],
    },
}


def _default_template() -> Dict[str, Any]:
    # Reasonable fallback - used when the use case doesn't match a known vertical.
    return {
        "title": "Enterprise Operations Demo",
        "personas": [
            {"id": "p1", "name": "Operations Lead at a Mid-Market Enterprise",
             "description": "Owns day-to-day operational KPIs across a team of 25.",
             "goals": ["Reduce manual handoffs", "Improve SLA adherence"],
             "pain_points": ["Fragmented tooling", "Reactive triage"]},
            {"id": "p2", "name": "Business Analyst Embedded with Operations",
             "description": "Turns operational data into weekly improvement proposals.",
             "goals": ["Ship one process change per sprint"],
             "pain_points": ["Slow data access", "Inconsistent metric definitions"]},
        ],
        "scenarios": [
            {"id": "s1", "title": "Incoming work spikes past capacity",
             "description": "A 40% volume spike over a 24-hour window strains the intake team.",
             "actors": ["p1"],
             "steps": ["Intake volume breaches threshold.", "System proposes load-balancing moves.",
                       "Operations lead approves the adjusted assignments."]},
            {"id": "s2", "title": "Recurring process gap identified",
             "description": "A pattern of rework shows up across three weekly reports.",
             "actors": ["p2"],
             "steps": ["System clusters rework causes.", "Analyst reviews top cluster.",
                       "Proposal is drafted for the next sprint."]},
        ],
        "agents": [
            {"id": "a1", "name": "Workload Balancing Agent", "role": "Reassigns queued work based on team capacity and SLA risk.",
             "responsibilities": ["Score SLA risk", "Propose reassignments"], "inputs": ["Queue state", "Team capacity"], "outputs": ["Reassignment plan"]},
            {"id": "a2", "name": "Rework Pattern Agent", "role": "Detects and clusters recurring failure modes in operational data.",
             "responsibilities": ["Cluster similar rework cases"], "inputs": ["Case history"], "outputs": ["Rework clusters"]},
            {"id": "a3", "name": "KPI Drift Agent", "role": "Monitors weekly KPI trends and flags regressions.",
             "responsibilities": ["Detect drift", "Surface probable causes"], "inputs": ["KPI time series"], "outputs": ["Drift alerts"]},
            {"id": "a4", "name": "Process Proposal Agent", "role": "Drafts process-change proposals grounded in evidence.",
             "responsibilities": ["Synthesize data into proposals"], "inputs": ["Clusters", "KPI drifts"], "outputs": ["Change proposal drafts"]},
        ],
    }


def _extract_use_case_section(user_prompt: str) -> str:
    """Pull out just the USE CASE: block so the stub doesn't trip on keywords
    that appear in the schema contract (e.g. "Retail" used as an example)."""
    marker = "USE CASE:"
    idx = user_prompt.find(marker)
    if idx == -1:
        return user_prompt
    tail = user_prompt[idx + len(marker):]
    # Stop at the next section header (e.g. "COMPANY CONTEXT:", "EXTERNAL RESEARCH",
    # or the schema block). These are separated by a blank line.
    end = tail.find("\n\n")
    return tail[:end].strip() if end != -1 else tail.strip()


class StubLLM:
    """Deterministic fallback used when no Anthropic key is configured."""

    async def generate(self, user_prompt: str) -> Dict[str, Any]:
        use_case_text = _extract_use_case_section(user_prompt)
        industry = _guess_industry(use_case_text)
        template = _TEMPLATES.get(industry, _default_template())
        return {
            "title": template["title"],
            "industry": industry,
            "personas": template["personas"],
            "scenarios": template["scenarios"],
            "agents": template["agents"],
        }

    async def update(self, user_prompt: str) -> Dict[str, Any]:
        """For stub mode, extract the current blueprint from the prompt and
        tweak the title to signal the update actually ran."""
        from app.services.prompts import SCHEMA_CONTRACT  # avoid import cycle

        # The prompt embeds the current blueprint as JSON. Pull it out.
        # We look for the first balanced JSON object after "CURRENT BLUEPRINT:".
        marker = "CURRENT BLUEPRINT:"
        idx = user_prompt.find(marker)
        if idx == -1:
            return await self.generate(user_prompt)
        tail = user_prompt[idx + len(marker) :]
        block = _first_balanced_object(tail)
        if not block:
            return await self.generate(user_prompt)
        try:
            current = json.loads(block)
        except json.JSONDecodeError:
            return await self.generate(user_prompt)

        updated = dict(current)
        # Drop top-level fields that the orchestrator assigns.
        updated.pop("id", None)
        updated.pop("status", None)

        # Apply a small visible transformation per action so the API contract
        # can be exercised end-to-end in stub mode.
        if "ACTION: regenerate_personas" in user_prompt:
            template = _TEMPLATES.get(updated.get("industry", ""), _default_template())
            updated["personas"] = template["personas"]
        elif "ACTION: regenerate_scenarios" in user_prompt:
            template = _TEMPLATES.get(updated.get("industry", ""), _default_template())
            updated["scenarios"] = template["scenarios"]
        elif "ACTION: regenerate_agents" in user_prompt:
            template = _TEMPLATES.get(updated.get("industry", ""), _default_template())
            updated["agents"] = template["agents"]
        else:
            # MODIFY: surface the instruction in the title to prove it ran.
            instruction = ""
            inst_idx = user_prompt.find("USER INSTRUCTION:")
            if inst_idx != -1:
                instruction = user_prompt[inst_idx:].splitlines()[1:2]
                instruction = instruction[0].strip() if instruction else ""
            if instruction:
                updated["title"] = f"{updated.get('title', 'Demo')} — {instruction[:60]}"
        return updated


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.use_stub_llm:
        logger.info("llm.mode", extra={"mode": "stub"})
        return StubLLM()
    logger.info("llm.mode", extra={"mode": "anthropic", "model": settings.anthropic_model})
    return AnthropicLLM(
        api_key=settings.anthropic_api_key or "",
        model=settings.anthropic_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        max_json_retries=settings.llm_max_json_retries,
    )
