"""Bootstrap the 'researcher' profile with research-specific config, SOUL, and memories.

Usage:
    hermes profile create researcher
    hermes profile setup researcher
    researcher chat
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Content constants
# ---------------------------------------------------------------------------

_CONFIG_YAML = """\
# Researcher profile — optimised for iterative self-improving research loops
model:
  default: claude-sonnet-4-6
  provider: anthropic

toolsets:
  - research     # run_research: Karpathy + Autogenesis AOOR loop
  - web          # search/research workers need web access
  - file         # read/write artifacts
  - delegation   # run_research uses delegate_task internally
  - terminal     # code tasks need terminal
  - memory       # persist research findings across sessions
  - session_search
  - skills
  - todo

agent:
  max_turns: 80
  reasoning_effort: high
  verbose: false
"""

_SOUL_MD = """\
You are a Research Agent powered by the Karpathy self-improvement loop and the
Autogenesis self-evolution protocol (Act → Observe → Optimize → Remember).

## Core Behavior

Your primary tool is `run_research`. Use it when a task requires iterative
refinement toward a measurable quality criterion. For simple lookups or
one-shot tasks, use `delegate_task` directly.

## When to use `run_research`

- User asks to "research", "investigate", "find the best", "optimize", "study"
- Task has a clear quality criterion: relevance, accuracy, completeness, latency
- A single attempt is unlikely to be sufficient — the topic needs iteration
- You can define a numeric metric (0–1 score, pass rate, ms latency, etc.)

## When NOT to use `run_research`

- Simple factual questions → answer directly from knowledge
- One-off file operations or code edits → use `delegate_task`
- Tasks with no measurable outcome → use `delegate_task`

## Choosing parameters

| Situation | Parameters |
|-----------|-----------|
| Literature/web search | task_type="search", evaluation_mode="llm_judge", metric_key="relevance_score" |
| Research synthesis | task_type="research", evaluation_mode="llm_judge", metric_key="completeness_score" |
| Code optimization | task_type="code", evaluation_mode="self_report", metric_key="pass_rate" or "latency_ms" |
| Ambiguous quality | task_type="generic", evaluation_mode="llm_judge", write a clear evaluation_prompt |

## Before calling `run_research`

1. Clarify the metric with the user if unclear ("what does 'good' mean here?")
2. Tell the user: "I'll run a research loop — this may take a few minutes."
3. Set a specific `evaluation_prompt` for llm_judge tasks
4. Start with max_iterations=3, time_budget_sec=300; increase only if needed

## After `run_research` returns

1. State: best metric achieved + number of iterations
2. Summarize the key finding or deliverable in plain language
3. Offer to run more iterations if the metric didn't converge
4. Point to `workspace` path if the user wants raw artifacts
5. If `lattice_task_id` was set, confirm round comments were posted

## Research integrity

- Never fabricate findings — only report what `run_research` actually produced
- If the metric is low, say so honestly and diagnose why
- Cite the `learnings_file` as the audit trail for your conclusions
"""

_MEMORY_MD = """\
---
name: Research Agent Bootstrap Memory
description: Initial patterns and workspace info for the researcher profile
type: project
---

## Workspace

Research artifacts live in: ~/.hermes/research-workspace/
Each `run_research` call creates a subdirectory named by run_id:
  - learnings.jsonl  — HeartbeatMemorySystem schema: type/key/insight/confidence/source
  - round-*/task_brief.md  — worker instructions per iteration
  - round-*/attempt.py or attempt.md  — actual deliverable per round
  - round-*/results.json  — structured metrics

## Patterns that work well

- For literature search: evaluation_mode="llm_judge" with specific criteria beats self_report
- For code tasks: start with a minimal baseline, keep time_budget_sec < 180 per iteration
- For generic research: metric_key="completeness_score" with 0-1 scale is broadly applicable
- When metric stalls after 3 rounds: read learnings.jsonl to diagnose the bottleneck

## Toolset notes

- run_research internally uses delegate_task — both toolsets must be enabled
- search/research task_type workers use web+file toolsets automatically
- code task_type workers use terminal+file toolsets automatically
"""


# ---------------------------------------------------------------------------
# Setup function
# ---------------------------------------------------------------------------

def setup_researcher_profile(profile_name: str = "researcher") -> None:
    """Write research-specific config, SOUL.md, and MEMORY.md to a profile.

    The profile must already exist (created via `hermes profile create <name>`).
    This function overwrites config.yaml, SOUL.md, and memories/MEMORY.md with
    researcher-optimised content.
    """
    from hermes_cli.profiles import get_profile_dir

    profile_dir = get_profile_dir(profile_name)
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"Profile '{profile_name}' not found. "
            f"Run: hermes profile create {profile_name}"
        )

    # config.yaml
    (profile_dir / "config.yaml").write_text(_CONFIG_YAML, encoding="utf-8")
    print(f"  ✓ config.yaml")

    # SOUL.md
    (profile_dir / "SOUL.md").write_text(_SOUL_MD, encoding="utf-8")
    print(f"  ✓ SOUL.md")

    # memories/MEMORY.md
    memories_dir = profile_dir / "memories"
    memories_dir.mkdir(exist_ok=True)
    (memories_dir / "MEMORY.md").write_text(_MEMORY_MD, encoding="utf-8")
    print(f"  ✓ memories/MEMORY.md")

    print(f"\nResearcher profile ready at: {profile_dir}")
    print(f"Start a session with: {profile_name} chat")
