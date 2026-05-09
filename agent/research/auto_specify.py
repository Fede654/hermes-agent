"""Auto-specify: flesh out a vague research topic via the kanban triage
auxiliary LLM. Used by ``run_research(..., auto_specify=True)`` so callers
can pass a one-line topic and get back a structured TaskSpec scaffold.

Reuses the ``triage_specifier`` aux-client role that the kanban
``/specify`` button already uses (same model + same prompt-shape
discipline). Output is constrained to a tight JSON schema so the caller
can drop the keys straight into ``TaskSpec(...)``.

Failures are silent: aux-client unavailable, API error, or unparseable
JSON returns ``None``. The caller decides whether to fall back to its
original args or surface a warning.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


_SPECIFY_SYSTEM = (
    "You are a research-task specifier. Given a short topic that may be "
    "vague, produce a JSON object with these keys:\n"
    '  - "deliverable" (string): what the worker must produce, concretely\n'
    '  - "metric_key" (string): the name of a measurable success metric\n'
    '  - "metric_direction" (string): "maximize" or "minimize"\n'
    '  - "task_type" (string): "code" | "search" | "research" | "generic"\n'
    '  - "evaluation_mode" (string): "self_report" or "llm_judge"\n'
    '  - "evaluation_prompt" (string, optional): only when evaluation_mode is '
    '"llm_judge" — a 0-to-1 scoring rubric\n\n'
    "Output ONLY the JSON object. No commentary. No fences. No prose."
)

_SPECIFY_USER_TEMPLATE = (
    "Topic: {topic}\n\n"
    "Produce the JSON spec. Pick task_type to match the deliverable:\n"
    "  code → measurable test outcomes (pass_rate, accuracy)\n"
    "  search → ranked retrieval (relevance_score)\n"
    "  research → synthesis quality (completeness_score, coverage)\n"
    "  generic → anything else with a numeric metric"
)


def get_text_auxiliary_client(role: str):
    """Indirection seam — patched in tests so we don't need a real aux client."""
    from agent.auxiliary_client import get_text_auxiliary_client as _impl
    return _impl(role)


def _extract_json_blob(raw: str) -> Optional[dict]:
    """Lenient JSON extraction tolerating fenced code blocks and prose
    around the JSON. Returns None when nothing parses or the result is
    not a dict."""
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        val = json.loads(stripped[first : last + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


def auto_specify_topic(topic: str) -> Optional[dict]:
    """Return a dict with TaskSpec scaffold fields, or None on any failure.

    Caller-facing schema (string keys; all optional in the response):
      deliverable, metric_key, metric_direction, task_type,
      evaluation_mode, evaluation_prompt
    """
    if not topic or not topic.strip():
        return None

    try:
        client, model = get_text_auxiliary_client("triage_specifier")
    except Exception as exc:
        logger.debug("auto_specify: aux client unavailable: %s", exc)
        return None
    if client is None or not model:
        return None

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SPECIFY_SYSTEM},
                {"role": "user", "content": _SPECIFY_USER_TEMPLATE.format(topic=topic)},
            ],
            temperature=0,
        )
    except Exception as exc:
        logger.debug("auto_specify: aux call failed: %s", exc)
        return None

    try:
        content = resp.choices[0].message.content or ""
    except Exception:
        return None

    return _extract_json_blob(content)
