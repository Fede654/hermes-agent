# Fork Workflow — nicoechaniz/hermes-agent

This repository is Nicolás' Hermes Agent fork. It intentionally carries local features that may or may not be upstreamed. Keep this file current whenever the branch topology changes.

## Branch roles

| Branch | Role | Rules |
|--------|------|-------|
| `upstream/main` | NousResearch upstream | Read-only reference. Fetch from `https://github.com/NousResearch/hermes-agent.git`. |
| `nousmain` | Local mirror of upstream main | Must match `upstream/main` exactly after each upstream sync. Do not commit directly. |
| `main` | Integration branch for our runtime | Merge `nousmain` plus all active canonical feature branches. This is what gets deployed to `~/.hermes/hermes-agent`. |
| `feat/*` / `fix/*` | Canonical fork features/fixes | Branch from `nousmain`, not from `main`, unless explicitly creating a short-lived integration/docs cleanup branch. Each branch must apply cleanly over `nousmain`. |
| `backup/*` / `*-legacy` | Safety archives | Do not merge. Keep only while useful for recovery. |

## Current canonical feature branches

These are the active branches we intentionally preserve as separable patches over `nousmain`:

| Branch | Purpose | Notes |
|--------|---------|-------|
| `feat/kimi` | Kimi support and corrections | Kimi CLI OAuth from `~/.kimi/credentials/kimi-code.json`; `X-Msh-*` headers; runtime credential resolution; Kimi OAuth refresh on auxiliary 401; Kimi K2.x context-length fixes. This replaces the old `feat/kimi-oauth-clean*` and `fix/kimi-context-length-resolution` branches. |
| `feat/daemoncraft` | DaemonCraft gateway / embodied-agent integration | Canonical DaemonCraft patch set. Keep clean over `nousmain`; old messy history lives only in `feat/daemoncraft-legacy` / backups. |
| `feat/minimax-defaults` | MiniMax defaults | Provider defaults for MiniMax Anthropic Messages transport and base URL behavior. |
| `feat/kanban-review` | Kanban review orchestration | Review graph/templates/CLI wiring for `hermes kanban review`. |
| `feat/altermundi-cli` | CLI input / interrupt behavior | Ctrl+C priority, interrupt transcript safety, multimodal requeue, TUI config support. |
| `feat/altermundi-tui` | TUI history behavior | History navigation behavior in the Ink TUI. |

If a branch is superseded, delete the remote branch after the replacement is merged and verified. Leave a local `backup/*` branch only when recent recovery is useful.

## Sync workflow

Before any push or pull, verify remotes:

```bash
git remote -v
```

Update the upstream mirror:

```bash
git fetch upstream main
git checkout nousmain
git reset --hard upstream/main
```

Rebuild each canonical feature branch on top of the new `nousmain`:

```bash
git checkout feat/kimi
git rebase nousmain
# resolve conflicts, run focused tests, then push:
git push origin feat/kimi --force-with-lease
```

Repeat for each active canonical branch. Do not rebase `main` onto feature branches; `main` is rebuilt by merging branches.

Rebuild integration `main`:

```bash
git checkout main
git reset --hard nousmain
git merge --no-ff feat/kimi
git merge --no-ff feat/daemoncraft
git merge --no-ff feat/minimax-defaults
git merge --no-ff feat/kanban-review
git merge --no-ff feat/altermundi-cli
git merge --no-ff feat/altermundi-tui
```

Run tests before pushing. Use the wrapper, never raw `pytest`:

```bash
scripts/run_tests.sh
```

For provider-specific fixes, add an end-to-end smoke test when credentials are available. Example for Kimi OAuth:

```bash
hermes chat --provider kimi-coding -m kimi-k2.6 -q 'Say OK only.' -Q --yolo
```

Push and deploy:

```bash
git push origin main
git -C ~/.hermes/hermes-agent fetch origin main
git -C ~/.hermes/hermes-agent reset --hard origin/main
```

If the gateway is running and the change affects gateway/runtime behavior, restart the relevant service after deployment.

## Rebirth sync coupling

Fork sync and CompAII rebirth sync go together. After a meaningful Hermes fork sync or deploy, also run the rebirth sync from the CompAII state repository:

```bash
python ~/Projects/compaii-state/sync.py
```

## Verification checklist

Before declaring a branch/workflow update complete:

1. `git remote -v` confirms `origin` and `upstream`.
2. `git status --short --branch` is clean.
3. Every canonical feature branch is either rebased onto `nousmain` or explicitly documented as pending rebase.
4. `main` contains every canonical active branch.
5. Superseded remote branches are deleted or clearly documented as archives.
6. Tests relevant to the changed areas pass via `scripts/run_tests.sh`.
7. Deployed checkout `~/.hermes/hermes-agent` matches `origin/main` when runtime behavior changed.
