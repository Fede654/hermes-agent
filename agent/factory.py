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
        # HRM-57 full: the five formerly-patched runtime invariants are now
        # constructor kwargs on AIAgent. terminal_cwd / cwd default to
        # env-derived values inside __init__; the rest are zero/None defaults
        # appropriate for a top-level detached parent.
        delegate_depth=0,
        terminal_cwd=os.getcwd(),
        cwd=os.getcwd(),
        subdirectory_hints=None,
    )

    # Only the progress callback still needs post-init wiring because it is
    # not a constructor kwarg today. tool_progress_callback IS in __init__,
    # but defaults to None — set a no-op so callers that read it can dispatch
    # without checking. provider_* are honored by __init__ from the spec, so
    # the previous defensive getattr block is no longer necessary.
    if agent.tool_progress_callback is None:
        agent.tool_progress_callback = lambda *a, **k: None

    return agent
