"""altercraft_memory — persistent memory layer for AlterCraft personas.

Memory files live at ``~/.hermes/profiles/altercraft-<persona>/memory/``:
  - locations.json    : known world locations
  - players.json      : known players and relationship notes
  - events.jsonl      : chronological events (capped + compressed)
  - preferences.json  : persona preferences
  - strategies.json   : learned winning strategies

All I/O is local (never synced).  The module is safe to import from any
AlterCraft loop; missing files are created lazily.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("altercraft_memory")

# ────────────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────────────

MEMORY_FILES = (
    "locations.json",
    "players.json",
    "events.jsonl",
    "preferences.json",
    "strategies.json",
)


def _profile_dir(persona: str) -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "profiles" / f"altercraft-{persona}"


def _memory_dir(persona: str) -> Path:
    d = _profile_dir(persona) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _memory_path(persona: str, key: str) -> Path:
    if key not in {p.rsplit(".", 1)[0] for p in MEMORY_FILES}:
        raise ValueError(f"unknown memory key {key!r}")
    fname = f"{key}.json" if key != "events" else "events.jsonl"
    return _memory_dir(persona) / fname


# ────────────────────────────────────────────────────────────────────────────
# Core API
# ────────────────────────────────────────────────────────────────────────────

def load_memory(persona: str) -> dict[str, Any]:
    """Load all memory files for *persona* into a dict."""
    out: dict[str, Any] = {}
    for fname in MEMORY_FILES:
        key = fname.rsplit(".", 1)[0]
        path = _memory_dir(persona) / fname
        if not path.exists():
            out[key] = [] if key == "events" else {}
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                if key == "events":
                    out[key] = [json.loads(line) for line in f if line.strip()]
                else:
                    out[key] = json.load(f)
        except Exception as exc:
            logger.warning("failed to load %s: %s", path, exc)
            out[key] = [] if key == "events" else {}
    return out


def save_memory(persona: str, key: str, value: Any) -> None:
    """Atomically write *value* to the memory file for *key*."""
    path = _memory_path(persona, key)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if key == "events":
                for ev in value:
                    f.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")
            else:
                json.dump(value, f, indent=2, ensure_ascii=False, default=str)
        tmp.replace(path)
    except Exception as exc:
        logger.warning("failed to save %s: %s", path, exc)
        raise


def append_event(persona: str, event: dict, *, max_events: Optional[int] = None) -> None:
    """Append *event* to ``events.jsonl``, compressing older events when over limit."""
    # Resolve max_events from profile if not provided
    if max_events is None:
        max_events = _profile_max_events(persona)

    path = _memory_path(persona, "events")
    event = dict(event)
    if "ts" not in event:
        event["ts"] = time.time()

    # Fast-append
    try:
        with open(path, "a", encoding="utf-8", buffering=1) as f:
            f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("append_event failed: %s", exc)
        return

    # Truncate if over limit (simple: drop oldest half)
    if max_events and max_events > 0:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > max_events:
                # Compress: keep first and last event from the dropped block as summary
                drop_count = len(lines) - max_events // 2
                kept = lines[drop_count:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                logger.info("events.jsonl compressed: dropped %d old events", drop_count)
        except Exception as exc:
            logger.warning("event compression failed: %s", exc)


def summarize_memory(persona: str, max_chars: int = 1200) -> str:
    """Return a compact paragraph summarising *persona*'s persistent memory."""
    mem = load_memory(persona)
    parts: list[str] = []

    # Players
    players = mem.get("players") or {}
    if players:
        items = []
        for name, info in players.items():
            note = info.get("note", "")
            last = info.get("last_seen")
            items.append(f"{name}" + (f" ({note})" if note else "") + (f", last seen {last}" if last else ""))
        parts.append("Known players: " + "; ".join(items))

    # Locations
    locations = mem.get("locations") or {}
    if locations:
        items = [f"{n}: {v}" for n, v in locations.items()]
        parts.append("Known locations: " + "; ".join(items))

    # Preferences
    preferences = mem.get("preferences") or {}
    if preferences:
        items = [f"{k}={v}" for k, v in preferences.items()]
        parts.append("Preferences: " + "; ".join(items))

    # Recent events (last 5)
    events = mem.get("events") or []
    if events:
        recent = events[-5:]
        ev_lines = []
        for ev in recent:
            t = ev.get("type", "event")
            desc = ev.get("description") or ev.get("summary") or str(ev)[:80]
            ev_lines.append(f"[{t}] {desc}")
        parts.append("Recent events: " + " | ".join(ev_lines))

    # Strategies
    strategies = mem.get("strategies") or {}
    if strategies:
        best = strategies.get("best") or strategies.get("current") or ""
        if best:
            parts.append(f"Current strategy: {best}")

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rsplit(" ", 1)[0] + "..."
    return text


# ────────────────────────────────────────────────────────────────────────────
# Profile helpers (backward-compatible)
# ────────────────────────────────────────────────────────────────────────────

def _profile_max_events(persona: str) -> int:
    """Read ``memory.max_events`` from the persona config, default 500."""
    try:
        import yaml
        cfg_path = _profile_dir(persona) / "config.yaml"
        if not cfg_path.exists():
            return 500
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        mem = (data.get("profile") or {}).get("memory") or {}
        return mem.get("max_events", 500)
    except Exception:
        return 500


def _profile_memory_enabled(persona: str) -> bool:
    """Return whether the persona config has ``memory.enabled == true``."""
    try:
        import yaml
        cfg_path = _profile_dir(persona) / "config.yaml"
        if not cfg_path.exists():
            return False
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        mem = (data.get("profile") or {}).get("memory") or {}
        return bool(mem.get("enabled", False))
    except Exception:
        return False
