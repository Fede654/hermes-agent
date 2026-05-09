#!/usr/bin/env python3
"""embodied_plan — single-tool body orchestration delegate.

The Hermes-side counterpart to the DaemonCraft embodied service v1.

Hermes' cloud LLM (Kimi/MiniMax/etc.) calls this **one tool** when it
needs the body to do something. The embodied service handles:

  1. Reading world_state from bot/server.js
  2. Filtering allowed_tools by executor_supported
  3. Composing a canonical Gemma-Andy v2 payload
  4. Calling Ollama (gemma-andy:e4b-v2-2-3-q8_0)
  5. Parsing the response (with <think> strip + bracket fallback)
  6. Dispatching each tool_call to bot/server.js
  7. Returning the assembled {plan, execution_results}

Hermes never has to know about the granular Mineflayer mc_* tools — that
is Gemma-Andy's job. Path B canonical per team architectural decision
2026-05-08 (see vault/concepts/gemma-andy-embodied-service.md and
vault/epics/E002-body-protocol-wireup.md).

Environment:
    EMBODIED_SERVICE_URL  Base URL of the embodied service
                          (default: http://localhost:7790)
    EMBODIED_PLAN_TIMEOUT Per-request timeout in seconds
                          (default: 60 — Ollama + dispatch can be slow)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from tools.registry import registry

logger = logging.getLogger(__name__)


def _service_url() -> str:
    return os.environ.get("EMBODIED_SERVICE_URL", "http://localhost:7790").rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get("EMBODIED_PLAN_TIMEOUT", "60"))
    except ValueError:
        return 60.0


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

EMBODIED_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "embodied_plan",
        "description": (
            "Delegate a body task in Minecraft to the embodied service "
            "(Gemma-Andy via Ollama). Use this when the user wants the "
            "agent's Minecraft character to DO something — gather, build, "
            "fight, navigate, craft, etc. The service handles world-state "
            "perception, tool selection, and execution against bot/server.js. "
            "You only describe the high-level intent in natural language. "
            "DO NOT use granular mc_* tools when this tool is available — "
            "this one collapses what would be 5-15 LLM rounds into a single "
            "delegation backed by a fine-tuned local model.\n\n"
            "USE WHEN:\n"
            "- The user asks the bot to do something physical in Minecraft\n"
            "- A multi-step body task (gather → craft → place)\n"
            "- A movement / navigation request\n"
            "- A combat / defensive action\n\n"
            "NOT FOR:\n"
            "- Conversation, narrative, education (handle yourself)\n"
            "- Reading/explaining game state to the user (handle yourself)\n"
            "- Tasks outside body orchestration (writing code, web research, etc.)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "Natural-language description of what the bot should do. "
                        "Be CONCRETE. Include 'what', 'where', and 'why' when "
                        "relevant. Examples: 'Help the player gather 12 oak logs "
                        "before night.' / 'Go to coordinates [120, 64, -33] but "
                        "avoid the ravine.' / 'Build a small shelter using planks "
                        "from the inventory.' Ambiguous intents are okay — the "
                        "embodied service will respond with an ask_clarification "
                        "tool_call which surfaces a question to ask the user."
                    ),
                },
                "autonomy_level": {
                    "type": "integer",
                    "description": (
                        "Guardian autonomy. 0=observer / 1=assistant / "
                        "2=supervised builder (DEFAULT, safe for kids+adults) / "
                        "3=autonomous companion / 4=advanced operator (risky)."
                    ),
                    "default": 2,
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional override of the tool subset Gemma-Andy may use. "
                        "Names must be canonical v2 tool names. When omitted, the "
                        "service uses its default safe set. The service further "
                        "filters by executor_supported, so passing tools the bot "
                        "server doesn't implement is harmless — they're dropped."
                    ),
                },
                "guardian_constraints": {
                    "type": "object",
                    "description": (
                        "Optional override of the safety constraints. Recognized "
                        "fields include no_tnt, no_protected_zone_edit, "
                        "protected_zone_owner, plus any no_<verb> bool flags. "
                        "Defaults are sane (no_tnt=true, no_protected_zone_edit=true)."
                    ),
                },
                "previous_error": {
                    "type": "object",
                    "description": (
                        "Optional. Pass when the previous embodied_plan call's "
                        "execution_results contained a failure and you want "
                        "Gemma-Andy to compose a recovery plan. Shape: "
                        "{tool: <name>, error_type: 'stuck'|'no_path'|'tool_timeout'|"
                        "'hazard_detected'|'missing_material'|'other', "
                        "details: <string>}."
                    ),
                },
                "deadline_seconds": {
                    "type": "integer",
                    "description": (
                        "Wall-clock budget for the WHOLE call (compose + Ollama + "
                        "dispatch). Default 30. Set higher for long execution "
                        "sequences."
                    ),
                    "default": 30,
                },
            },
            "required": ["intent"],
        },
    },
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _handler(args: dict[str, Any], **_kw: Any) -> str:
    intent = (args or {}).get("intent", "")
    if not intent or not isinstance(intent, str):
        return json.dumps({
            "ok": False,
            "error": {"error_type": "missing_intent",
                      "details": "embodied_plan requires a non-empty 'intent' string"},
        })

    body: dict[str, Any] = {"intent": intent}
    for k in (
        "autonomy_level",
        "allowed_tools",
        "guardian_constraints",
        "previous_error",
        "deadline_seconds",
    ):
        if k in args and args[k] is not None:
            body[k] = args[k]

    url = f"{_service_url()}/intent"
    timeout = _timeout()

    try:
        resp = httpx.post(url, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        logger.warning("embodied_plan timed out after %.1fs: %s", timeout, exc)
        return json.dumps({
            "ok": False,
            "error": {
                "error_type": "embodied_service_timeout",
                "details": f"timed out after {timeout}s waiting for {url}",
            },
        })
    except httpx.RequestError as exc:
        logger.warning("embodied_plan request failed: %s", exc)
        return json.dumps({
            "ok": False,
            "error": {
                "error_type": "embodied_service_unreachable",
                "details": f"{type(exc).__name__}: {exc}",
            },
        })

    try:
        result = resp.json()
    except json.JSONDecodeError as exc:
        return json.dumps({
            "ok": False,
            "error": {
                "error_type": "embodied_service_bad_response",
                "details": f"non-JSON body (status {resp.status_code}): {resp.text[:200]}",
            },
        })

    # Pass the service response through verbatim. Hermes' AIAgent gets
    # the full {ok, plan, execution_results, ...} envelope so the LLM
    # can decide whether to retry with previous_error, reword the
    # request, or surface ask_clarification questions to the user.
    return json.dumps(result)


def _check_service_available() -> bool:
    """Light availability check — does NOT call /health (would block tool
    discovery on a slow service). The check_fn is invoked at toolset
    enumeration time; an unreachable service still lets the tool register
    and produce a clean error at call time. We just verify the URL parses."""
    try:
        url = _service_url()
        return url.startswith("http://") or url.startswith("https://")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# AST check in tools/registry.py only recognizes `registry.register(...)`
# at module scope, not inside loops or conditionals.
registry.register(
    name="embodied_plan",
    toolset="embodiment",
    schema=EMBODIED_PLAN_SCHEMA,
    handler=_handler,
    check_fn=_check_service_available,
    emoji="🤖",
    description=EMBODIED_PLAN_SCHEMA["function"]["description"],
)
