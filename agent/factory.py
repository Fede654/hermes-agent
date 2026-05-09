"""Centralized AIAgent construction for non-CLI entrypoints.

Most Hermes entrypoints (CLI, gateway, ACP, TUI gateway, batch_runner) build
``AIAgent`` directly with their own kwargs. The detached research-job runner
historically did the same plus a manual post-init patching block to satisfy
the runtime invariants ``delegate_task`` expects.

This module consolidates that patching into one well-named factory so:
1. The fragile patch list lives in *one* place — easier to keep in sync as
   ``AIAgent`` evolves upstream.
2. Other detached entrypoints (cron, batch jobs, future schedulers) can
   reuse the same factory rather than copy-pasting the patch block.

The longer-term plan (HRM-57 follow-up, requires upstream coordination)
is to absorb the five fragile internal attributes — ``_delegate_depth``,
``terminal_cwd``, ``cwd``, ``_subdirectory_hints``, ``_delegate_spinner``
— into ``AIAgent.__init__`` itself so this factory becomes a thin profile
mapper. Until then it is the single point of fragility.
"""
from __future__ import annotations

import os
from typing import Any


def build_agent_for_research_job(spec: dict[str, Any]) -> Any:
    """Build an AIAgent suitable for running a detached research job.

    Reads model/provider/toolset config from ``spec`` (typically loaded from
    ``<job_dir>/job.json``). The parent agent inherits the active profile's
    SOUL.md, AGENTS.md, and MEMORY.md unless ``spec`` explicitly opts out
    via ``skip_context_files`` or ``skip_memory``.

    Returns:
        Live ``AIAgent`` ready to be passed as ``parent_agent`` to
        ``run_research`` / ``ResearchSupervisor``.
    """
    from run_agent import AIAgent

    agent = AIAgent(
        model=spec["model"],
        provider=spec.get("provider"),
        base_url=spec.get("base_url"),
        api_key=spec.get("api_key"),
        api_mode=spec.get("api_mode"),
        enabled_toolsets=spec.get("toolsets", ["research", "terminal", "file"]),
        quiet_mode=True,
        platform="cli",
        session_id=f"research-job:{spec['job_id']}",
        skip_context_files=spec.get("skip_context_files", False),
        skip_memory=spec.get("skip_memory", False),
    )

    # The "HRM-57 full" idea was to fold these five runtime invariants into
    # AIAgent.__init__ upstream so the factory wouldn't need to patch them.
    # That kwargs change never landed in the upstream AIAgent — the
    # constructor still hardcodes _delegate_depth=0 internally and does not
    # accept delegate_depth / terminal_cwd / cwd / subdirectory_hints kwargs
    # at all. Until that reaches main, we patch the post-init invariants the
    # delegate_task code path depends on.
    if not hasattr(agent, "_delegate_depth"):
        agent._delegate_depth = 0
    if not hasattr(agent, "terminal_cwd") or not getattr(agent, "terminal_cwd", None):
        agent.terminal_cwd = os.getcwd()
    if not hasattr(agent, "cwd") or not getattr(agent, "cwd", None):
        agent.cwd = os.getcwd()
    if not hasattr(agent, "_subdirectory_hints"):
        agent._subdirectory_hints = None

    # tool_progress_callback IS in __init__, but defaults to None. Set a
    # no-op so callers that read it can dispatch without a None-check.
    if agent.tool_progress_callback is None:
        agent.tool_progress_callback = lambda *a, **k: None

    return agent
