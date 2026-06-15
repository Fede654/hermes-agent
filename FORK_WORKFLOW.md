# Fork Workflow — Fede654/hermes-agent

This repository is Fede's Hermes Agent fork. It sits **downstream of Nicolás'
fork** (`nicoechaniz`), which itself tracks NousResearch upstream. Fede's fork
adds research-specific features on top, and **replicates the result across
several agents** rather than a single runtime.

Adapted from Nicolás' original `FORK_WORKFLOW.md` (the branch-topology
discipline is his). The differences here: a third layer (Nous → Nico → Fede)
and a tag-based, multi-agent release ritual.

Keep this file current whenever the branch topology or release process changes.

## Layers

```
NousResearch/hermes-agent   (upstream — canonical Hermes)
        │  fetch, read-only
        ▼
nicoechaniz/hermes-agent    (Nico's fork — adds kimi, daemoncraft, minimax,
        │                     kanban-review, altermundi; syncs upstream)
        │  PRs flow back up (sync, fixes)
        ▼
Fede654/hermes-agent        (this fork — adds autoresearch + metric-aware /goal;
                              replicates to N agents via canonical tags)
```

## Operating principles

This is an **active collaboration with Nico and a close track of upstream Nous**,
not a long-lived divergent fork. Everything below serves that:

- **Track Nous closely, sync small and often.** Frequent small rebases beat rare
  giant ones — the god-file refactors (authz/model-flow/cli extractions) are far
  cheaper to absorb in weekly-sized chunks. Divergence is debt; pay it down on a
  cadence, not when forced.
- **Base on `nicoechaniz/main`, not Nous directly.** Nico's main already carries
  the shared canonical features (kimi, daemoncraft, …) and his own upstream sync.
  Basing there means Fede maintains only his *own* delta (autoresearch + `/goal`)
  and inherits the rest for free. Fede helps keep Nico current via sync PRs — that
  loop *is* how this fork tracks Nous.
- **Keep Fede's delta minimal and upstreamable.** Provider-neutral, no hardcoded
  models, clean over the base. Anything that could live in Nico's fork should go
  there via PR, not accrete here.
- **Push everything up promptly.** Sync PRs and autoresearch fixes flow back to
  Nico as soon as they're validated, so the shared core never drifts from what
  Fede actually runs.

## Branch / remote roles

| Ref | Role | Rules |
|-----|------|-------|
| `upstream/main` | NousResearch upstream | Read-only. `https://github.com/NousResearch/hermes-agent.git`. |
| `nicoechaniz/main` | Nico's fork main | Read-only reference. Carries Nico's canonical features. PRs target his branches, not main directly. |
| `nousmain` | Local mirror of the chosen base | Must match `upstream/main` (or a specific upstream tag) exactly after each sync. Never commit directly. |
| `integration/autoresearch-on-sync-<date>` | Build / staging branch | Where the integration is assembled + validated per sync (`nousmain` + Nico's features + Fede's features + carried fixes). Once green it is **promoted to `feat/integration`**. Rebuilt per sync, never the merge target of feature branches. |
| `feat/*` / `fix/*` | Fede's canonical features/fixes | Each applies cleanly over the chosen base. Examples below. |
| `feat/integration` | **THE canonical integration — single deploy target** | What every deployed agent tracks (Chiwa's `/opt/chiwa/agent`, `~/.hermes/hermes-agent`, …). Force-updated each sync to the validated build. Provider-neutral; only per-agent config differs. The pre-2026-06 hand-built history under this name is **retired** — `canonical-<date>` tags hold immutable snapshots if you need the old states. |
| `archive/*` / `backup/*` / `*-legacy` | Safety archives (often remote-only) | Do not merge. A branch fully present on `fede654` may be deleted locally — the remote is the archive. |
| `canonical-<date>` (tag) | Immutable release pin | Annotated tag on a validated `feat/integration` state. **Optional**, for rollback / reproducibility. Agents track the `feat/integration` **branch**; pin to a tag only to freeze a specific build. |

## Current canonical features

Fede-originated (also flow upstream to Nico via PR):

| Feature | Where it lives | Notes |
|---------|----------------|-------|
| autoresearch | `agent/research/`, `tools/research_tool.py`, `tools/research_job_tool.py` | Provider-neutral. Canonical source is Nico's `feat/autoresearch-core-v014`; Fede's `fix/autoresearch-core-flaws` carries the detached-job / fan-out / `inherit_profile` fixes (PR to Nico). |
| metric-aware `/goal` | `hermes_cli/goals.py`, `hermes_cli/cli_commands_mixin.py`, `gateway/slash_commands.py` | Extends Nous's `/goal` (Ralph-style loop) with an optional `metric:` tail + deterministic verdict bypass. Wired into the relocated mixin handlers. |

Consumed from Nico (do not re-implement — they ride in via the base): `feat/kimi`,
`feat/daemoncraft`, `feat/minimax-defaults`, `feat/kanban-review`,
`feat/altermundi-cli`, `feat/altermundi-tui`, `feat/kimi-webbridge`.

## Sync workflow (advancing to a new upstream)

1. Confirm remotes: `git remote -v` (expect `upstream`, `nicoechaniz`, `fede654`).
2. Refresh the base mirror. Default base is `nicoechaniz/main` (inherits Nico's
   features + his upstream sync). Use `upstream/main` directly only when racing
   ahead of Nico — and then open the sync PR so he catches up:
   ```bash
   git fetch upstream nicoechaniz
   git checkout nousmain && git reset --hard nicoechaniz/main   # or upstream/main
   ```
3. Rebuild the integration in a worktree (keeps the live install untouched):
   ```bash
   git worktree add -b integration/autoresearch-on-sync-<date> .worktrees/sync <base>
   cd .worktrees/sync
   git rebase --onto nousmain <old-base> <fede-feature-branch>   # e.g. autoresearch-core
   # git auto-drops patch-duplicates; resolve conflicts where upstream
   # extracted code (god-file refactors — authz_mixin, model_setup_flows,
   # cli_commands_mixin). Take upstream's structure, graft the fork delta in.
   ```
4. Cherry-pick the standalone feature commits (e.g. metric-aware `/goal`) onto the
   integration; resolve against the moved handlers.
5. **Validate before tagging:**
   ```bash
   scripts/run_tests.sh   # or: pytest tests/agent/research tests/hermes_cli/test_goals.py ...
   ```
   Plus one **live** smoke — mocks hide signature drift (this is how the
   `delegate_task(inherit_profile=)` break was found):
   ```python
   research_job(action='start', topic='Return 42', metric_key='correctness', max_iterations=1)
   ```

## Release & multi-agent replication

Every deployed agent tracks **one stable branch: `feat/integration`** — the
canonical integration. Code is identical across agents; only config differs. The
dated `integration/autoresearch-on-sync-<date>` branch is just the staging area
where the integration is assembled and validated; once green it is **promoted to
`feat/integration`** and (optionally) tagged `canonical-<date>` as an immutable
rollback pin.

1. Promote the validated build to the canonical branch (+ optional tag):
   ```bash
   git push fede654 +<validated-build>:feat/integration   # force-update the canonical
   git tag -a canonical-<date> -m "Canonical stack — base, features, validation"  # optional pin
   git push fede654 canonical-<date>
   ```
2. On each agent's checkout (`~/.hermes/hermes-agent`, Chiwa's `/opt/chiwa/agent`):
   ```bash
   git fetch fede654
   git checkout -B feat/integration fede654/feat/integration
   .venv/bin/python -m pip install -e .          # sync deps to the new base
   # restart the agent's gateway (per-agent service, e.g. hermes-gateway-chiwa)
   ```
3. **Per-agent, never in git:** `~/.hermes/config.yaml` (`model.default` / `provider`
   / `base_url`) and provider credentials (e.g. `~/.kimi/credentials/kimi-code.json`).
   autoresearch is provider-neutral — each agent runs research with whatever
   runtime/provider it has configured.
4. Keep the previous integration branch as the rollback ref until the new tag is
   confirmed healthy on every agent.

## Upstream collaboration (PRs to Nico)

Fede's work flows back up so the fork doesn't diverge silently:

- **Sync** (`sync/upstream-<date>` → `nicoechaniz/main`): advances Nico's fork to a
  newer upstream. Merge, not rebuild, matching his "Merge branch 'nousmain'" style.
- **Fixes** (`fix/autoresearch-core-flaws` → `nicoechaniz/feat/autoresearch-core-v014`):
  bugfixes to the shared autoresearch core go to his canonical branch, not into the
  upstream-sync PR (different scope, different base).

## Verification checklist

Before declaring a release done:

1. `git remote -v` shows `upstream`, `nicoechaniz`, `fede654`.
2. Working tree clean; no stray conflict markers (`git grep -nE '^(<{7}|>{7}) '`).
3. Every Fede feature is rebased onto the new base or explicitly noted as pending.
4. Focused tests green **and** one live `run_research` smoke on the target provider.
5. `canonical-<date>` tag pushed to `fede654`.
6. Every agent's `~/.hermes/hermes-agent` checked out at the tag; gateway restarted.
7. Superseded local branches deleted (archive on `fede654` first if not already there).
