# altercraft memory plugin

Wraps the flat-JSON store in `agent/altercraft_memory.py` (locations,
players, events, preferences, strategies) as a `MemoryProvider`.

## When to use

For Hermes profiles that drive embodied agents in AlterCraft (Minecraft).
Activate by adding to the profile's `config.yaml`:

```yaml
profile:
  memory:
    provider: altercraft
```

For altercraft-* profiles (e.g. `altercraft-clio`), the plugin scopes
writes to `~/.hermes/profiles/altercraft-<persona>/memory/`. For
non-altercraft profile names, the plugin uses the profile name verbatim
as the directory key.

## Tools exposed

| Tool | Purpose |
|---|---|
| `altercraft_recall_summary(max_chars?)` | Compact paragraph summary of all memory layers. |
| `altercraft_remember_location(name, x, y, z, notes?)` | Save a named location. |
| `altercraft_recall_locations()` | Read all known locations. |
| `altercraft_record_event(type, description, details?)` | Append an event to events.jsonl. |
| `altercraft_recall_events(n?)` | Read the N most recent events. |

The system prompt also receives a summary block automatically (via
`system_prompt_block`).

## What this plugin is NOT

* Not a replacement for the `altercraft` toolset. The toolset (in
  `tools/altercraft_tool.py`) wraps the bot's HTTP API for perception
  and action; this memory plugin wraps the persistent JSON store. Both
  can be active together — they're orthogonal.
* Not a scene graph. The flat JSON shape is preserved for backwards
  compatibility with `agent/altercraft_runner.py` and the existing
  Karpathy loop. A scene graph (R*Tree spatial index, time-versioned
  edges, etc.) is a separate phase per the consolidated architecture
  spec.
* Not embedding-backed. Recall is exact-match by name + chronological by
  event timestamp. Semantic recall over events is future work.

## Files written

| File | Shape |
|---|---|
| `locations.json` | `{name: {x, y, z, notes}}` |
| `players.json` | `{name: {note, last_seen, ...}}` |
| `events.jsonl` | line-delimited `{ts, type, description, ...}` |
| `preferences.json` | `{key: value}` |
| `strategies.json` | `{best, current, notes}` |

All atomic (write-tmp-then-rename). All under
`~/.hermes/profiles/<scope>/memory/`.

## Spec

`Alter-infra:docs/superpowers/specs/2026-04-28-consolidated-architecture.md`
(Phase 6, "Memory plugin for altercraft world state").
