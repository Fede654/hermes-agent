# UPSTREAM pin — hermes-continuity-plugin

This directory is a **vendored copy** of the upstream plugin code from:

- **Repository:** https://github.com/Mar-IA-no/hermes-continuity-plugin
- **Vendored commit:** `8515c98d7bde080e1698a19402889993f05e9c21` (v1.1.2)
- **Vendored on:** 2026-04-30
- **License:** see upstream `LICENSE` (MIT-equivalent permissive)

## What was copied

Verbatim from upstream `plugin/`:
- `__init__.py` — the plugin module (`register(ctx)` entry point + the
  `pre_llm_call` / `post_llm_call` hook implementations).
- `plugin.yaml` — name (`dialogue-handoff`), version (`1.1.2`), and the
  `provides_hooks` declaration.

## Why vendored, not pip

The upstream repo doesn't (yet) publish a pip package with semver
guarantees. The plugin is small, self-contained, and stable. Vendoring
gives us:

- A reproducible build of `hermes-agent` independent of upstream
  availability.
- The ability to patch in hot fixes locally if a Minecraft-specific
  edge case appears (e.g. the platform-resolver corner cases we noted
  in §13 of the scene-graph spec).
- A clear `UPSTREAM.md` ↔ commit pin for future re-syncs.

When the upstream project ships a stable pip release, this directory can
be replaced with a `pip install hermes-continuity-plugin>=1.1` plus a
thin import shim under the same path.

## Re-sync procedure

To update to a newer upstream commit:

```bash
cd /tmp && git clone https://github.com/Mar-IA-no/hermes-continuity-plugin
cp /tmp/hermes-continuity-plugin/plugin/__init__.py \
   /home/fede/REPOS/hermes-agent/plugins/dialogue-handoff/__init__.py
cp /tmp/hermes-continuity-plugin/plugin/plugin.yaml \
   /home/fede/REPOS/hermes-agent/plugins/dialogue-handoff/plugin.yaml
# Update the commit pin and date in this UPSTREAM.md.
```

Run the test suite (`tests/agent/test_dialogue_handoff_plugin.py`) before
committing the bump.

## What this plugin gives us

See `Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md`
§13.3 for the full pattern catalogue this brings in. Short version:

- **Working-memory continuity across sessions/restarts/crashes.**
  `post_llm_call` writes a Recent-Exchanges tail to a per-platform
  `DIALOGUE-HANDOFF.<platform>.md`. `pre_llm_call` injects it into the
  first turn of a new session as `<previous_session_context>...`.
- **Per-platform separation.** A Hermes serving CLI + Minecraft +
  Telegram simultaneously gets one handoff file per platform (so the
  Minecraft thread doesn't bleed into Telegram and vice versa).
- **Substantive-turn gate.** Trivial chat (<300 chars combined turn)
  doesn't overwrite a real tail.
- **`ALWAYS-CONTEXT.md`** layer for stable persona/operator rules.
- **Working-set extraction** (file paths touched by recent tool calls)
  surfaces in the handoff for engineering personas; harmless for
  Minecraft personas.

## How to activate (per-profile)

Set in the profile's environment when launching agent_loop or hermes:

```bash
export HERMES_AGENT_MEMORY_BASE="$HERMES_HOME/profiles/altercraft-clio/agent-memory"
export HERMES_PLATFORM="minecraft"
```

The plugin will resolve:
- Handoff: `$HERMES_AGENT_MEMORY_BASE/state/DIALOGUE-HANDOFF.minecraft.md`
- Always-context: `$HERMES_AGENT_MEMORY_BASE/state/ALWAYS-CONTEXT.md`
- Sessions index: `$HERMES_HOME/sessions/`

If neither `HERMES_HANDOFF_PATH` nor `HERMES_AGENT_MEMORY_BASE` is set,
the plugin **disables itself** and logs a single error explaining what
to set. It does not crash the agent.
