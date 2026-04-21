# Demo Designer Agent

A backend service that turns a free-form enterprise AI use case into a structured **Demo Blueprint** — personas, scenarios, and agents — and supports interactive refinement until a reviewer approves the artifact. The approved blueprint is the contract between this service and the frontend demo renderer (e.g. Bolt) or a refinement UI (e.g. Open WebUI).

This is v1 of the design engine described in the PRD. It is intentionally small: in-memory session store, one LLM (Claude), optional research enrichment (Tavily), strict schema validation, and no orchestration engine.

## What you get

- **FastAPI server** exposing `/generate`, `/update`, `/approve`, `/blueprint/{session_id}`, and `/health`.
- **Claude integration** with a JSON-retry loop (up to 3 attempts per call).
- **Deterministic stub mode** — runs fully offline when no API key is set, so you can try the flow or run CI without credentials.
- **Optional Tavily enrichment** triggered when a `company` is provided or the use case is short/vague.
- **Pydantic schema** that rejects vague personas ("Manager") and generic agents ("AI Assistant"), enforces 2–3 personas, 2–3 scenarios, 4–6 agents, and cross-checks scenario actors against persona ids.
- **Pytest suite** covering schema rules, session management, the JSON retry path, and the end-to-end flow through a `TestClient`.
- **Curl demo scripts** for retail, finance, healthcare, and logistics.

## Project layout

```
demo-designer-agent/
├── app/
│   ├── main.py               # FastAPI app + lifespan + error handlers
│   ├── config.py             # Settings loaded from .env
│   ├── models/schema.py      # Pydantic models (Persona, Scenario, Agent, DemoBlueprint, ...)
│   ├── services/
│   │   ├── blueprint.py      # Orchestrator: LLM + validation + retries
│   │   ├── llm.py            # Anthropic client + deterministic stub
│   │   ├── tavily.py         # Optional research enrichment
│   │   ├── prompts.py        # Generation + update prompts
│   │   └── session.py        # In-memory session store
│   └── routes/
│       ├── generate.py       # POST /generate
│       ├── update.py         # POST /update
│       ├── approve.py        # POST /approve
│       └── retrieve.py       # GET  /blueprint/{session_id}
├── tests/                    # Pytest suite
├── scripts/                  # End-to-end curl demos
├── requirements.txt
├── pytest.ini
├── .env.example
└── README.md
```

## Quickstart

```bash
# 1. Create a virtualenv
python3.11 -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) configure API keys
cp .env.example .env
# edit .env — leave ANTHROPIC_API_KEY blank to run in stub mode

# 4. Run the server
uvicorn app.main:app --reload

# 5. Smoke-test it
curl -s http://localhost:8000/health | jq .
```

Interactive API docs are available at `http://localhost:8000/docs` once the server is up.

## Running the example flows

With the server running (stub mode is fine), try:

```bash
bash scripts/demo_retail.sh
bash scripts/demo_finance.sh
bash scripts/demo_healthcare.sh
bash scripts/demo_logistics.sh
```

Each script walks through `generate → update → approve → retrieve` against a single vertical. They require `jq` for pretty output and session-id extraction.

## Running the tests

```bash
pytest
```

The suite runs in stub-LLM mode (no network), so it is fast and deterministic. Coverage:

- **`test_schema.py`** — PRD quality bars (rejects "Manager" personas, "AI Assistant" agents, enforces count bounds, cross-checks scenario actors).
- **`test_session.py`** — in-memory store create/get/update/approve.
- **`test_llm_retry.py`** — fakes the Anthropic client to verify the JSON-retry path (bad JSON on attempt 1–2, valid on attempt 3), fence stripping, and give-up-after-max-retries.
- **`test_routes.py`** — full `/generate → /update → /approve → /blueprint/...` flow via FastAPI TestClient across four industries, plus error paths (unknown session, modify-without-instructions, update-after-approve).

## API reference

### `POST /generate`

Request:
```json
{
  "use_case": "A 40-store apparel retailer wants AI support for replenishment and shrink detection.",
  "company": "Acme Apparel"
}
```

Response:
```json
{
  "session_id": "9f8b...",
  "blueprint": {
    "id": "d8a1...",
    "title": "Store Operations & Replenishment Demo",
    "industry": "Retail",
    "status": "draft",
    "personas": [ ... ],
    "scenarios": [ ... ],
    "agents": [ ... ]
  }
}
```

If `company` is provided or the use case is short/vague, Tavily is queried for up to 5 results and the summarized findings are injected into the generation prompt (skipped silently if `TAVILY_API_KEY` is not set or the request fails).

### `POST /update`

Request:
```json
{
  "session_id": "9f8b...",
  "action": "regenerate_personas | regenerate_scenarios | regenerate_agents | modify",
  "instructions": "Free-form instruction. Required for action=modify, optional for regenerate_*."
}
```

The top-level blueprint `id` is preserved across updates — the session always points at a single logical blueprint. Approved blueprints are locked (`409 Conflict`).

### `POST /approve`

Request:
```json
{ "session_id": "9f8b..." }
```

Response:
```json
{ "status": "approved", "demo_id": "d8a1..." }
```

`demo_id` is the blueprint's `id` — use it as the hand-off identifier to the frontend renderer.

### `GET /blueprint/{session_id}`

Returns the current blueprint for the session, whether draft or approved.

## Configuration

All knobs live in `.env` (see `.env.example`):

| Var | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | _(unset)_ | Leave blank for stub mode. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Any Anthropic messages-API model. |
| `LLM_TEMPERATURE` | `0.3` | PRD requires ≤ 0.4 for determinism. |
| `LLM_MAX_TOKENS` | `4000` | Per call. |
| `LLM_MAX_JSON_RETRIES` | `3` | JSON + schema retries per LLM call. |
| `TAVILY_API_KEY` | _(unset)_ | Leave blank to skip enrichment. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | _(unset)_ | Both required to enable tracing. |
| `LANGFUSE_HOST` (or `LANGFUSE_BASE_URL`) | cloud default | For a self-hosted Langfuse, e.g. `http://localhost:3000`. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Server bind. |

## Notes on error handling (PRD §12)

- **Invalid JSON from the LLM** — `app/services/llm.py` strips markdown fences, extracts the first top-level object, and retries up to `LLM_MAX_JSON_RETRIES` with a corrective nudge ("your previous response was not valid JSON, error: …").
- **Schema-valid JSON that still fails Pydantic validation** — `app/services/blueprint.py` catches `ValidationError`, injects the first few errors back into the prompt, and retries.
- **Upstream API failure / give-up** — returned as `502 Bad Gateway` with a structured `{"error", "detail"}` envelope.
- **Unknown `session_id`** — `404 Not Found`.
- **Update on an already-approved blueprint** — `409 Conflict`.

## Observability (Langfuse)

Tracing is opt-in. When both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set, `/generate` and `/update` emit a hierarchical trace per request:

```
generate (trace)
├── blueprint.generate
│   ├── tavily.enrich                 (span, with query + num_results)
│   └── anthropic.call_with_retry     (span, with attempts + outcome)
│       └── anthropic.messages.create (generation, with model, input, output, token usage)
```

`update` traces have the same shape rooted at `blueprint.update`. Traces on `/update` carry `session_id` so a refinement session is filterable in the Langfuse UI.

`GET /health` reports `tracing_enabled` so you can confirm the wiring came up. If the keys are missing, tracing is a hard no-op — no background threads, no network attempts, and the `langfuse` package is not imported. Tests force tracing off via `tests/conftest.py` regardless of `.env` contents.

See `app/services/tracing.py` for the integration seam; the rest of the codebase imports `observe`, `update_observation`, and `update_trace` from that module and never touches Langfuse directly.

## Swapping the LLM / storage layer

`BlueprintService` accepts an injected `LLMClient` (see `app/services/llm.py`), so you can plug in a different provider by implementing the `LLMClient` Protocol (`.generate(user_prompt)` and `.update(user_prompt)` returning a JSON dict). The session store exposes a similarly narrow interface in `app/services/session.py` — a SQLite/Supabase implementation only needs `create`, `get`, `update_blueprint`, and `approve`.

## What's explicitly out of scope (v1)

Per the PRD: no real-time agent execution, no multi-agent orchestration engine, no frontend UI, no persistent memory beyond the current session. The blueprint is a *simulation design artifact*, not a runnable system.
