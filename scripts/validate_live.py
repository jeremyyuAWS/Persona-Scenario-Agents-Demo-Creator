"""Live validation harness for the Demo Designer Agent.

Runs the four PRD §14 use cases (retail, finance, healthcare, logistics)
against a running server, saves each blueprint to outputs/, and prints a
scorecard of quality checks that approximate the PRD quality bars:

- Personas: specific role (not "Manager"/"User"/etc.), goals and pain points
  tied to workflows, >= 15 char description.
- Scenarios: >= 2 concrete steps, description mentions operational context.
- Agents: functional (not "AI Assistant"/"General Agent"/etc.),
  responsibilities tied to scenarios.

Usage:
    # 1. In one terminal, start the server with a real key in .env:
    uv run uvicorn app.main:app --reload

    # 2. In another terminal:
    uv run python scripts/validate_live.py
    # or target a different host:
    BASE_URL=http://localhost:8000 uv run python scripts/validate_live.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx


BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"

# Claude can take 30-90s on a long prompt with Tavily enrichment, especially
# on the first request per process while the httpx connection pool is cold.
# Give each /generate call a generous ceiling.
REQUEST_TIMEOUT_SECONDS = 180.0

# PRD §14 use cases. Keep them realistic but open-ended — we want to see how
# Claude chooses the specific personas, scenarios, and agents.
USE_CASES = [
    {
        "slug": "retail",
        "expected_industry_hints": ["retail"],
        "payload": {
            "use_case": (
                "A 40-store apparel retailer wants to pilot AI at the store level. "
                "Top priorities are inventory replenishment between stores when a "
                "SKU sells through, and early detection of shrink patterns from POS "
                "and cycle counts."
            ),
            "company": "Acme Apparel",
        },
    },
    {
        "slug": "finance",
        "expected_industry_hints": ["financial", "bank", "finance"],
        "payload": {
            "use_case": (
                "A mid-market commercial bank wants AI support for two workflows: "
                "(1) fraud triage across debit, credit, and ACH channels — today "
                "analysts drown in rules-based alerts with a high false-positive "
                "rate; (2) SMB loan underwriting — normalizing bank statements "
                "and tax returns to surface cash-flow discrepancies."
            ),
            "company": "Meridian Bank",
        },
    },
    {
        "slug": "healthcare",
        "expected_industry_hints": ["health", "clinic", "medical"],
        "payload": {
            "use_case": (
                "A multi-specialty clinic wants to reduce friction around insurance "
                "pre-authorization and specialist referral scheduling. Today "
                "pre-auth packets are assembled manually from EHR notes and "
                "referrals sit in limbo waiting on payer approval."
            ),
        },
    },
    {
        "slug": "logistics",
        "expected_industry_hints": ["logistic", "transport", "freight"],
        "payload": {
            "use_case": (
                "A regional LTL carrier needs AI for day-of routing exceptions "
                "(weather, road closures), dock orchestration when trailers arrive "
                "off-schedule, and monitoring driver check-ins for missed stops."
            ),
            "company": "Northern Freight",
        },
    },
]


# PRD-style quality patterns
VAGUE_PERSONA_NAMES = re.compile(
    r"^(manager|employee|worker|staff|user|person|people|customer)$",
    re.IGNORECASE,
)
GENERIC_AGENT_NAMES = re.compile(
    r"^(ai assistant|assistant|general agent|ai agent|chatbot|bot|helper)$",
    re.IGNORECASE,
)


def _check_persona(p: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    name = p.get("name", "")
    if VAGUE_PERSONA_NAMES.match(name.strip()):
        issues.append(f"persona name '{name}' is too generic")
    if len(p.get("description", "")) < 30:
        issues.append(f"persona '{name}' description is thin (< 30 chars)")
    if not p.get("goals"):
        issues.append(f"persona '{name}' has no goals")
    if not p.get("pain_points"):
        issues.append(f"persona '{name}' has no pain points")
    return issues


def _check_scenario(s: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if len(s.get("steps", [])) < 2:
        issues.append(f"scenario '{s.get('title', '?')}' has fewer than 2 steps")
    desc = s.get("description", "")
    if len(desc) < 40:
        issues.append(f"scenario '{s.get('title', '?')}' description is thin (< 40 chars)")
    return issues


def _check_agent(a: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    name = a.get("name", "")
    if GENERIC_AGENT_NAMES.match(name.strip()):
        issues.append(f"agent name '{name}' is generic")
    if not a.get("responsibilities"):
        issues.append(f"agent '{name}' has no responsibilities")
    if not a.get("inputs") or not a.get("outputs"):
        issues.append(f"agent '{name}' is missing inputs or outputs")
    return issues


def _industry_matches(industry: str, hints: list[str]) -> bool:
    lowered = industry.lower()
    return any(h in lowered for h in hints)


def _score_blueprint(bp: dict[str, Any], expected_hints: list[str]) -> dict[str, Any]:
    issues: list[str] = []

    if not _industry_matches(bp.get("industry", ""), expected_hints):
        issues.append(
            f"industry '{bp.get('industry')}' doesn't match any of {expected_hints}"
        )

    n_personas = len(bp.get("personas", []))
    if not 2 <= n_personas <= 3:
        issues.append(f"expected 2-3 personas, got {n_personas}")

    n_scenarios = len(bp.get("scenarios", []))
    if not 2 <= n_scenarios <= 3:
        issues.append(f"expected 2-3 scenarios, got {n_scenarios}")

    n_agents = len(bp.get("agents", []))
    if not 4 <= n_agents <= 6:
        issues.append(f"expected 4-6 agents, got {n_agents}")

    for p in bp.get("personas", []):
        issues.extend(_check_persona(p))
    for s in bp.get("scenarios", []):
        issues.extend(_check_scenario(s))
    for a in bp.get("agents", []):
        issues.extend(_check_agent(a))

    return {
        "issues": issues,
        "counts": {
            "personas": n_personas,
            "scenarios": n_scenarios,
            "agents": n_agents,
        },
    }


def _run_case(client: httpx.Client, case: dict[str, Any], *, drop_company: bool) -> dict[str, Any]:
    slug = case["slug"]
    print(f"\n=== {slug} ===")
    payload = dict(case["payload"])
    if drop_company:
        # Dropping `company` (and using a longer use case) keeps the server from
        # triggering Tavily enrichment — see app/services/tavily.py::should_enrich.
        payload.pop("company", None)
    t0 = time.perf_counter()
    resp = client.post(f"{BASE_URL}/generate", json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    dt = time.perf_counter() - t0
    resp.raise_for_status()
    body = resp.json()
    bp = body["blueprint"]

    path = OUTPUT_DIR / f"{slug}_blueprint.json"
    path.write_text(json.dumps(body, indent=2))

    score = _score_blueprint(bp, case["expected_industry_hints"])
    print(f"  industry: {bp['industry']}")
    print(f"  title:    {bp['title']}")
    print(
        f"  counts:   {score['counts']['personas']} personas, "
        f"{score['counts']['scenarios']} scenarios, "
        f"{score['counts']['agents']} agents"
    )
    print(f"  latency:  {dt:.2f}s")
    if score["issues"]:
        print(f"  issues ({len(score['issues'])}):")
        for issue in score["issues"]:
            print(f"    - {issue}")
    else:
        print("  issues:   none \u2713")
    print(f"  saved to: {path.relative_to(path.parent.parent)}")
    return {"slug": slug, "latency": dt, **score}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-tavily",
        action="store_true",
        help="Drop the `company` field from each payload so the server skips "
        "Tavily enrichment. Useful when Tavily is slow or you want to isolate "
        "Claude's output.",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=[c["slug"] for c in USE_CASES],
        help="Run only the named verticals (e.g. --only retail finance).",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client() as client:
            health = client.get(f"{BASE_URL}/health", timeout=5.0).json()
    except httpx.HTTPError as e:
        print(f"ERROR: could not reach {BASE_URL}/health — is the server running? ({e})")
        return 2

    if health.get("stub_llm"):
        print(
            "WARNING: server is in stub mode (no ANTHROPIC_API_KEY set).\n"
            "  This script will still run, but you're validating stub output,\n"
            "  not live Claude. Set ANTHROPIC_API_KEY in .env and restart the server.\n"
        )

    cases = [c for c in USE_CASES if (not args.only or c["slug"] in args.only)]
    tavily_note = "disabled via --no-tavily" if args.no_tavily else f"{health.get('tavily_enabled')}"
    print(
        f"Validating against {BASE_URL} — tavily_enabled={tavily_note} — "
        f"request timeout {REQUEST_TIMEOUT_SECONDS:.0f}s"
    )

    results: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for case in cases:
            try:
                results.append(_run_case(client, case, drop_company=args.no_tavily))
            except httpx.HTTPError as e:
                print(f"  ERROR: {e}")
                results.append({"slug": case["slug"], "error": str(e)})

    # Summary table
    total_issues = sum(len(r.get("issues", [])) for r in results)
    print("\n=== Summary ===")
    for r in results:
        if "error" in r:
            print(f"  {r['slug']:12s}  ERROR — {r['error']}")
        else:
            marker = "\u2713" if not r["issues"] else f"\u2717 ({len(r['issues'])} issues)"
            print(f"  {r['slug']:12s}  {marker:20s}  {r['latency']:.2f}s")
    print(f"\nTotal issues across all verticals: {total_issues}")
    print(f"Raw blueprints saved to: {OUTPUT_DIR}")

    # Reminder of the qualitative checks that can only be done by eye
    print(
        "\nManual review checklist (PRD §14):\n"
        "  - Do the persona names read as real job titles at real companies?\n"
        "  - Do the scenarios describe specific situations a practitioner would recognize?\n"
        "  - Do the agents map cleanly to steps in the scenarios?\n"
        "  - Is any output recycled wording you'd expect from a generic AI demo?\n"
    )
    return 1 if total_issues else 0


if __name__ == "__main__":
    sys.exit(main())
