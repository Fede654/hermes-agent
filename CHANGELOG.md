# Changelog — nicoechaniz/hermes-agent fork

> **Provider note:** Profile configs reference DeepSeek, Kimi, and MiniMax providers because that's our stack. Team members using different providers (OpenRouter, Anthropic, Nous, etc.) should adapt `model.provider`, `model.default`, and `model.base_url` in each profile's `config.yaml`. API keys go in each profile's `.env` (or symlink to shared `.env`). The `max_turns` and `reasoning_effort` values are provider-agnostic and should work across backends.

## 2026-06-14 — v0.16.0 / v2026.6.5+ sync (851 upstream commits, big release)

### TL;DR for team members on older agents

If your agent hasn't been updated since before 2026-06-14, here's what changed and how to get the new capabilities:

1. **Run `hermes update` in `~/.hermes/hermes-agent`** — this pulls the latest from `origin/main` (currently at `2665e44ef`).
2. **If TUI changed: `cd ~/.hermes/hermes-agent/ui-tui && npm run build`**.
3. **New `video_generate` is available** — `video_gen.provider: xai` (default) or `video_gen.provider: minimax` (PR #41241 open upstream). Just call `video_generate` in chat.
4. **New model `kimi-k2.7-code` is in the Coding Plan picker** — first option in the Kimi/Moonshot provider list.
5. **AutoResearch is functional again** — `run_research` + `research_job` with 136/136 tests passing. Docs in `~/wiki/projects/hermes-agent/notes/autoresearch-guide.md`.

If you can't `hermes update` for some reason (locked deploy, network down, etc.), see the manual fallback in `~/wiki/projects/hermes-agent/notes/workflow.md` section "Option B — Manual fallback".

### What merged in (chronological)

#### Kimi WebBridge toolset (commit `72098a906`, cherry-picked from `feat/kimi-webbridge`)

Real-browser automation via the Kimi WebBridge daemon on `127.0.0.1:10086`. Unlike Playwright-based browser tools, this controls the user's REAL browser with their actual login sessions. Tools: `kimi_webbridge_navigate`, `kimi_webbridge_find_tab`, `kimi_webbridge_snapshot`, `kimi_webbridge_click`, `kimi_webbridge_fill`, `kimi_webbridge_evaluate`, `kimi_webbridge_screenshot`, `kimi_webbridge_save_screenshot`, `kimi_webbridge_save_pdf`, `kimi_webbridge_list_tabs`, `kimi_webbridge_close_tab`, `kimi_webbridge_close_session`. Off by default (`_DEFAULT_OFF_TOOLSETS`); enable via `hermes tools` once the WebBridge extension is installed. 26/26 tests passing.

#### AutoResearch core (commit `0f6120146`, cherry-picked from `feat/autoresearch-core-v014`)

The distilled AutoResearch core (1 commit by nicoechaniz 2026-05-18, distilled from the 162-commit heavy `feat/autoresearch` branch). Provides `run_research` (interactive) and `research_job` (detached long-running) with full parameter set: `topic`, `deliverable`, `metric_key`, `metric_direction`, `task_type`, `max_iterations`, `evaluation_mode` (`self_report`/`llm_judge`), `evaluation_prompt`, `acceptance_criterion`, `initial_attempt`, `time_budget_sec`, `kanban_task_id`, `strategies`, `auto_specify`. 136/136 tests in `tests/agent/research/` + `tests/agent/test_research_supervisor.py` + `tests/agent/test_factory.py`. Full parameter spec in `~/wiki/projects/hermes-agent/notes/autoresearch-guide.md`.

#### Kimi k2.7-code picker (commit `2665e44ef`)

`kimi-k2.7-code` (Moonshot's new coding model, released 2026-06-12) is now the first option in the Kimi Coding Plan picker. Three-file change: `hermes_cli/models.py:282` (curated list), `hermes_cli/model_setup_flows.py:1800` (the picker the user sees), `hermes_cli/main.py:4038` (deprecated copy). Trigger: run `hermes model`, choose Kimi / Moonshot → Coding Plan.

#### TUI TERMINAL_TIMEOUT display fix (commit `607f0c0e9`, cherry-picked from `feat/altermundi`)

`hermes info` used to print `TERMINAL_TIMEOUT: 60` but the actual default in `tools/terminal_tool.py:1152` is `180`. This was confusing — now it reads the real default. One-line change, 28/28 tests passing.

### How to verify you're on the new version

```bash
# Check the version Hermes reports
hermes --version
# Should show: Hermes Agent v0.16.0 (2026.6.5) · upstream 2665e44e
# Or later commits (k2.7 picker = 2665e44ef, TUI fix = 607f0c0e9)

# Check video_generate is available
hermes tools | grep -i video
# Should show video_generate tool

# Check kimi-k2.7-code is in the picker
hermes model  # interactive, see the model list

# Check autoresearch is functional
python -c "from tools.autoresearch import run_research" 2>&1 | head
# (Import path may vary; this is just a smoke test)
```

### Files changed (high level)

- 1237 files changed in the upstream sync (mostly noise: desktop, dashboard, i18n, docs)
- 18 files changed in our fork: 4 conflict resolutions + 3 cherry-picks + 1 fix + 1 picker update
- DaemonCraft tools (`mc_navigate_tool`, `mc_bit_tool`, `embodied_plan_tool`) all preserved and verified in deploy
- Kimi OAuth from `~/.kimi/credentials/kimi-code.json` still works (auto-detected)

### Conflicts resolved

- `agent/conversation_loop.py` — kept ours (17 retry tracking vars)
- `cli.py` — took theirs (refactored `_estimate_tui_input_height`)
- `gateway/run.py` — kept ours (DaemonCraft lab-mode fail-safe)
- `hermes_cli/main.py` — kept ours (`_model_flow_kimi`, 113 lines)

All preserved: session_id propagation, X-Msh-* headers, DaemonCraft lab-mode, kanban review.

### Source of truth

- `~/Projects/hermes-agent/MEMORY.md` — current operational state
- `~/wiki/projects/hermes-agent/notes/branch-stewardship-2026-06-14.md` — full branch state
- HMK chapter 61 — canonical branch list for future sessions
- `~/wiki/projects/hermes-agent/notes/autoresearch-guide.md` — AutoResearch parameter spec

## 2026-05-16 — mc_bit Tool Fix

### Synchronous mc_bit Handler

The `mc_bit` Hermes tool was broken since deploy: `async def _handler(...)` returned
a coroutine object, which surfaced as `object of type 'coroutine' has no len()` in
live tool calls. Replaced with a synchronous `httpx.get` wrapper.

**Branch:** `feat/daemoncraft`
**Commit:** `a16bc0c5b fix(daemoncraft): make mc_bit tool synchronous`

Tests: `scripts/run_tests.sh tests/tools/test_mc_bit_tool.py -q --tb=short` → 3 passed.

### mBit Context in Embodied Service (DaemonCraft side)

See DaemonCraft CHANGELOG for the full mBit context integration. The hermes-agent
side only needed the mc_bit tool fix above — the world_state injection lives in
the embodied service composer on the DaemonCraft repo.

## 2026-05-09 — Multi-Agent Coding Roster + Kanban Hardening

### New Profiles
- **riqui** (deepseek-v4-flash, max_turns=30, reasoning=minimal): Surgical coding Kanban worker. Fixed protocol violation (was max_turns=15 + reasoning=none → iteration exhaustion before kanban_complete).
- **miki** (kimi-k2.6, kimi-coding OAuth via ~/.kimi/, max_turns=30, reasoning=high): Coding agent. Tested working.
- **maxi** (MiniMax-M2.7, minimax provider, Anthropic endpoint, max_turns=30, reasoning=high): Coding agent. Config created but blocked by CLI api_mode detection bug (404 — hardcoded chat_completions vs anthropic_messages).
- **claudio** (planned): Proxy profile → Claude Code CLI
- **gepeto** (planned): Proxy profile → Codex CLI

### Kanban System
- **Protocol violation root cause:** max_turns too low + reasoning=none on weak models → iteration exhaustion → model writes kanban_complete as text (not function call) → clean exit without transition → effective_limit=1 → auto-blocked
- **Fix:** max_turns ≥ 25 + reasoning ≥ minimal for all Kanban coding workers
- **Self-spawn guard:** Dispatcher DOES spawn tasks assigned to gateway's own profile (compaii). Tasks must stay in `todo`/`triage` until manually claimed.
- **Smoke test pattern:** t_4631001e (17s, riqui) validated the fix

### RTK Plugin
- **FIXED** by Riqui (t_ad89b059): Replaced corrupted `rtk_hermes/__init__.py` (circular self-import) with 332-line source from GitHub
- Binary symlinked for gateway PATH
- Plugin loads cleanly on gateway restart (no WARNING)

### Memory Infrastructure
- HMK chapters 9-11 seeded: dispatcher guard, profile roster, maxi api_mode debug
- Project MEMORY.md updated with full profile roster and dispatcher critical rule

### Known Issues
- **maxi:** `hermes -p maxi chat` returns 404. CLI hardcodes api_mode=chat_completions. Provider transport=anthropic_messages is ignored. curl confirms endpoint works.
- **Upstream:** ~90 commits behind (v2026.5.7+), needs sync

## 2026-05-08 — Upstream Sync v2026.5.7

- Full rebase onto upstream/main (993 commits, 7 conflicts resolved)
- All 10 custom features preserved
- Gateway split: hermes-gateway.service (CompAII) + hermes-gateway@steve.service
- RTK plugin installed (but init.py was corrupted — fixed May 9)
- Kanban migration from Lattice (64+ tasks)
- CompAII hardening: max_turns=40, reasoning=high, compression=0.50
- HMK memory kit: library.db seeded, engram_pack prefetch

## Custom Features (all branches merged into main)

1. feat/kimi-oauth-clean — Kimi OAuth refresh, header fixes
2. feat/altermundi-tui — TUI scrollbar, max lines config
3. feat/altermundi-cli — Ctrl+C priority config
4. feat/minimax-defaults — MiniMax provider defaults
5. feat/compression-config-reboot — Configurable compression protect_first_n
6. feat/dc-112-daemoncraft-gateway — Gateway adapter wiring, tool_choice propagation
7. DC-99 — Profile system prompt override per platform
8. DC-123 — TTS fixes + wake-up logging, CycleDetector
9. DC-132 — Contextvars-based endpoint resolution, turn metrics
10. DC-134 — Configurable turn wall-clock timeout + per-profile max_iterations
