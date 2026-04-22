---
name: a-evolve
description: >
  Apply A-Evolve's agentic evolution methodology to improve AI agent performance
  across runs. Use when the user wants to diagnose agent failures, generate
  targeted skills from error patterns, evolve system prompts, or accumulate
  episodic knowledge. Works standalone or inside AutoResearchClaw pipelines.
  Triggers on: "evolve", "self-improve", "diagnose failures", "generate skills
  from errors", "what went wrong and how to fix it", or any mention of A-Evolve.
---

# A-Evolve: Agentic Evolution Skill

Apply the **Solve → Observe → Evolve → Gate → Reload** methodology from
[A-Evolve](https://github.com/A-EVO-Lab/a-evolve) to iteratively improve
agent performance. This skill is prompt-based — no external dependencies,
no harness changes. You analyze failures, propose workspace mutations, and
generate durable artifacts (skills, prompt patches, knowledge entries) that
the agent can load in future runs.

## Core Loop

When asked to evolve or improve agent performance, follow this 5-step loop:

### 1. Solve (Collect Evidence)

Gather the agent's execution artifacts. Ask the user for or locate:
- Run logs, error traces, or experiment outputs
- Pass/fail results per task
- Metric values (accuracy, reward, success rate)
- Any existing session files from previous runs

If inside Hermes AutoResearch, look at:
- `artifacts/hermes-research-*/` — experiment outputs per round
- Lattice task event history (`lattice show <task_id> --events`)
- Lattice comments — each round posts KEPT/IMPROVED/DISCARDED + metric
- `ExperimentRunner.history.to_dict()` — full round history in memory

### 2. Observe (Diagnose)

Analyze the collected evidence to produce structured observations:

For each failed or underperforming task, identify:
- **Error category**: code bug, timeout, wrong approach, missing knowledge,
  API misuse, hallucinated reference, prompt ambiguity, etc.
- **Root cause**: What specifically went wrong and why
- **Frequency**: Is this a one-off or a recurring pattern across tasks?
- **Severity**: blocking (pipeline crash) / degrading (wrong result) /
  cosmetic (formatting issue)

Write observations as a structured list:

```
## Observations (Batch N)

### OBS-1: [Category] Short description
- Tasks affected: task_001, task_005, task_012
- Root cause: ...
- Frequency: 3/50 tasks (6%)
- Severity: degrading

### OBS-2: ...
```

### 3. Evolve (Propose Mutations)

Based on observations, propose one or more of these mutation types:

**A. Generate a Skill** (for recurring patterns, frequency ≥ 3)

Write a new `SKILL.md` file that teaches the agent how to handle this
pattern. A good evolved skill:
- Targets a specific failure category, not generic advice
- Contains concrete steps the agent should follow
- Includes a "when to apply" trigger condition
- Is short (under 100 lines) and self-contained

Example — if the agent keeps failing at API pagination:

```markdown
---
name: api-pagination-handler
description: >
  Handle paginated API responses correctly. Use when making API calls
  that may return partial results, or when results seem truncated.
---

When calling any API that supports pagination:

1. Check response for pagination indicators: `next_page`, `offset`,
   `has_more`, `cursor`, or truncated result counts.
2. If paginated, loop until all pages are collected.
3. Concatenate results before processing.
4. Set a max-page safety limit (default: 20) to prevent infinite loops.
5. Log total items collected vs expected count if available.
```

**B. Patch the System Prompt** (for prompt ambiguity or missing guidance)

Write a short addendum to the system prompt that addresses the gap.
Keep patches minimal — one paragraph per issue. Format:

```
## Prompt Patch: [Issue]
Append to system prompt:
> When [specific situation], always [specific action] because [reason].
```

**C. Add a Knowledge Entry** (for factual gaps or learned heuristics)

Record a reusable insight as a knowledge entry:

```json
{
  "id": "know-001",
  "category": "experiment_design",
  "insight": "Synthetic benchmarks with <100 samples produce high-variance results. Always use ≥500 samples or report confidence intervals.",
  "source": "observation OBS-3 from batch 2",
  "confidence": 0.85
}
```

**D. Do Nothing** (if observation is a one-off, severity is cosmetic,
or the fix would be too broad / risky)

### 4. Gate (Validate)

Before accepting any mutation, check:

- **Specificity**: Does it target the observed failure without being so
  broad it could cause regressions elsewhere?
- **Testability**: Could you verify this mutation helps by re-running the
  failed tasks?
- **Blast radius**: How much of the agent's behavior does this change?
  Prefer small, targeted mutations over large rewrites.
- **Consistency**: Does it contradict existing skills or prompt guidance?

If a mutation fails the gate, either refine it or discard it.
Explain your reasoning to the user.

### 5. Reload (Apply and Record)

Present the accepted mutations to the user. For each:
- State what changed and why
- Show the artifact (skill file, prompt patch, knowledge entry)
- Suggest where to place it in the project

For Hermes AutoResearch projects, recommended locations:

| Artifact | Location |
|----------|----------|
| Evolved skill | `skills/autoresearch/evolved/<skill-name>/SKILL.md` |
| Prompt patch | Append to `prompts/autoresearch.yaml` |
| Knowledge entry | `agent/evolution_store.jsonl` via `EvolutionStore.append_many()` |
| Observation log | `artifacts/hermes-research-<run_id>/observations/<batch>.md` |

Keep a running version log so the user can track what evolved and when:

```
## Evolution Log
- evo-1 (2026-03-30): Generated `api-pagination-handler` skill from OBS-1
- evo-2 (2026-03-30): Prompt patch for citation format from OBS-4
```

## Usage with Hermes AutoResearch

This skill maps to Hermes Karpathy loop steps:

| Loop Step | Evolution Role |
|-----------|---------------|
| Step 3: DELEGATE | Source of Solve artifacts — delegate_task outputs |
| Step 4: METRIC | Main Observe trigger — parse what went wrong in metric extraction |
| Step 5: KEEP/DISCARD | Natural Gate — KEPT = accept, DISCARDED = evolve |
| EvolutionStore | Lessons persisted via `EvolutionStore.append_many()` |

When the user says "evolve my research pipeline" or similar:

1. Ask which run to analyze (or find the latest `artifacts/hermes-research-*/`)
2. Run the Observe step on Lattice round comments + experiment outputs
3. Propose mutations targeting the weakest loop steps
4. Generate skill files in `skills/autoresearch/evolved/`

## Anti-Patterns

Do NOT:
- Generate vague, generic skills ("always be careful", "check your work")
- Propose mutations for one-off errors that won't recur
- Rewrite the entire system prompt — patch it surgically
- Generate more than 3 skills per evolution cycle (quality over quantity)
- Mutate tool code unless the user explicitly asks for it

## Relationship to EvolutionStore

Hermes uses `EvolutionStore` (`agent/research_evolution.py`) as the lesson persistence layer.
Evolved skills from this process can be placed in `skills/autoresearch/evolved/`
so they are available in future research sessions. The two systems are complementary:

- **A-Evolve skill**: Deep, targeted mutation from structured observation
- **EvolutionStore lesson**: Broad pattern captured with time-decay weighting (`LessonEntry`)

Both can coexist. Skills generated here are higher-precision; EvolutionStore
lessons are higher-recall and decay naturally over time (30-day half-life).
