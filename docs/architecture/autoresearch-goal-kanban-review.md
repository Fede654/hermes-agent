# Architecture Review: AutoResearcher + /goal + Kanban (HRM-110 follow-up)

**Context:** HRM-110 (A/B testing) is complete. This doc explores how the new `/goal` and `kanban` primitives could reshape the autoresearch architecture.

---

## Current AutoResearch Architecture

```
User → run_research tool → ResearchSupervisor.run()
                                ├── ExperimentRunner (Karpathy loop)
                                │       ├── delegate_task → Worker subagent
                                │       └── Metric parsing + keep/discard
                                ├── _improve_attempt() → LLM reflection
                                └── Lattice comments (progress tracking)
```

**Strengths:** Self-contained, single-turn tool call, deterministic loop.

**Weaknesses:**
- Research is a "fire-and-forget" tool call. If it exceeds the turn budget, the user must wait or the agent loops blindly.
- No visibility into running experiments except Lattice comments.
- Fan-out branches are ephemeral — no durable task representation.
- No integration with the dispatch/worker system that Hermes uses for other multi-agent workloads.

---

## New Primitives Overview

### `/goal` (GoalManager)

- **What:** A standing objective that persists across turns. After each assistant response, a judge model asks "is the goal done?"
- **State machine:** active → paused → done/cleared
- **Budget:** Max turns (default 20). Auto-pauses on budget exhaustion.
- **Persistence:** Stored in SessionDB state_meta, survives `/resume`.

### Kanban

- **What:** SQLite-backed task board with statuses (triage → todo → ready → running → blocked → done → archived).
- **Claim/CAS:** Workers claim tasks via compare-and-swap on `claim_lock`.
- **Workspaces:** Each task gets a scratch/workspace directory.
- **Events/Comments:** Full audit trail per task.
- **Multi-board:** Separate boards per project.

---

## Strategic Opportunities

### 1. Research as a Persistent Goal

**Idea:** A research task becomes a `/goal` instead of a single `run_research` tool call.

**Flow:**
```
User: /goal "Find and evaluate 5 papers on diffusion models for video generation"

Turn 1: Agent runs baseline search, produces initial list, metric = 0.3
Judge: "NOT done — only 2 papers found, need 5"

Turn 2: Agent refines search strategy, finds 3 more papers, metric = 0.6
Judge: "NOT done — papers found but no evaluation yet"

Turn 3: Agent evaluates each paper, metric = 0.9
Judge: "DONE — 5 papers found and evaluated"
```

**Benefits:**
- Research can span multiple turns without blocking the user.
- The judge provides an external "done" signal independent of the worker's self-report.
- Natural pause/resume semantics (`/goal pause`, `/goal resume`).

**Challenges:**
- The judge currently evaluates a single response, not a cumulative research state. Need a research-aware judge that can inspect the workspace/checkpoints.
- Turn budget may be too coarse for research (20 turns × N iterations each = potentially 100+ worker calls).

### 2. Kanban as Experiment Orchestrator

**Idea:** Each experiment run becomes a kanban task. Fan-out branches become linked subtasks.

**Mapping:**

| Kanban Concept | Research Mapping |
|----------------|------------------|
| Board | Research project / topic |
| Task | Single experiment run |
| Task status | Experiment lifecycle |
| Task workspace | `research-workspace/<run_id>/` |
| Task assignee | Worker profile or agent ID |
| Task links | Parent/child for fan-out branches |
| Task comments | Round-by-round progress |
| Task events | CHECKPOINT_SAVED, SNAPSHOT_CREATED |

**Flow:**
```
User: /kanban create "Research: diffusion models for video"
Agent: Creates task, moves to "running"

Each iteration:
  → Update task comment with metric
  → If blocked: move to "blocked", add block reason
  → If done: move to "done", attach results.json

Fan-out (3 branches):
  → Create 3 linked subtasks
  → Each worker claims one subtask
  → MOA aggregation: parent task collects subtask results
```

**Benefits:**
- Durable, queryable experiment history (not just Lattice comments).
- Workers can claim experiment tasks via the standard kanban dispatcher.
- Dashboard visibility into all running research.
- Research tasks coexist with coding tasks on the same board.

**Challenges:**
- Kanban tasks are designed for dispatcher/worker workloads, not iterative self-improvement loops. Need a "loop driver" that updates the task after each iteration.
- The kanban DB schema doesn't have native support for "iteration N of M" or "best metric so far."

### 3. Unified Dispatch: Kanban Workers for Research

**Idea:** Replace `delegate_task` with kanban's dispatch system for research workers.

**Current:** `ResearchSupervisor` spawns workers directly via `delegate_task`.
**Proposed:** `ResearchSupervisor` creates kanban tasks; the kanban dispatcher spawns workers.

```
ResearchSupervisor → kanban_db.create_task() → dispatcher tick
                                           → worker claims task
                                           → worker runs experiment
                                           → worker updates task result
                                           → ResearchSupervisor reads result
```

**Benefits:**
- Workers can run detached (like `research_job_tool`) but with full kanban lifecycle.
- Fault tolerance: if a worker crashes, the task becomes reclaimable.
- Multi-profile: different worker profiles for different task types (code vs search vs research).

**Challenges:**
- Research workers need tight feedback loops (seconds, not minutes). Kanban dispatcher ticks are typically 30s.
- The supervisor needs synchronous results to decide keep/discard. Async kanban would require a polling loop.

### 4. Research-Aware Goal Judge

**Idea:** Extend the goal judge to understand research-specific completion criteria.

**Current judge prompt:** "Is the goal satisfied based on the last response?"
**Research judge prompt:** "Is the research goal satisfied? Check: (1) metric >= threshold, (2) iterations converged, (3) no regressions in last 3 rounds."

**Integration point:** `agent/research/ab_testing.py` could expose a `ResearchGoalEvaluator` that the GoalManager calls instead of the generic judge.

---

## Recommended Path Forward

### Phase 1: Research-Aware Goal (Immediate)

Implement a `ResearchGoalManager` subclass or adapter that:
- Wraps `ResearchSupervisor.run()` as a goal lifecycle.
- Uses the research metric as the "done" signal (instead of a generic judge).
- Persists experiment state in the goal's session metadata.

**Deliverable:** A new tool `run_research_goal` or a parameter `as_goal=True` in `run_research`.

### Phase 2: Kanban Integration for Fan-Out (Medium-term)

When `fan_out > 1`, create linked kanban subtasks instead of inline `delegate_task` batch calls.

**Deliverable:** `ResearchSupervisor._run_fan_out_iteration()` optionally uses kanban tasks. Falls back to `delegate_task` when kanban is unavailable.

### Phase 3: Research Board Template (Long-term)

A `hermes kanban board create --template research` that sets up:
- Predefined columns: hypothesis → experiment → evaluate → aggregate → done
- Auto-linking of fan-out subtasks
- Dashboard widgets for metric-over-time graphs

---

## Open Questions

1. **Should research tasks live on the main kanban board or a separate board?**
   - Same board: visibility alongside coding tasks.
   - Separate board: cleaner schema, research-specific columns.

2. **How does the goal turn budget interact with research iterations?**
   - One turn = one iteration? Too coarse.
   - One turn = one full run (baseline + N iterations)? May exceed budget.
   - Sub-turn budget within the goal loop?

3. **Should the A/B tester (HRM-110) create kanban tasks for each strategy?**
   - Yes: each strategy run becomes a task, results are comments.
   - No: keep A/B tester lightweight, use Lattice for now.

---

## Related Tasks

- HRM-105: TUI integration for research progress → could render kanban tasks.
- HRM-106: Dashboard metrics graph → could read kanban task events.
- HRM-107: Lattice API native → kanban already has a native API (SQLite).
- HRM-111: Skill generation from lessons → kanban tasks could track skill generation experiments.
