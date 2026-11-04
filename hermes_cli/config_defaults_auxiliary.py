"""Pure-data factory for auxiliary model configuration defaults."""


def _aux(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see DEFAULT_CONFIG["auxiliary"]).

    reasoning_effort=False omits that key (MoA blocks configure depth per slot);
    ``extra`` keys are appended after the standard ones.
    """
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d


def _audit():
    """Opt-in knowledge-audit defaults; deployment owns provider pins and doctrine."""
    return {
        "enabled": False, "mode": "dry_run", "idle_hours": 6, "window_days": 2,
        "max_messages": 120, "overlap": 10, "max_writes": 8, "max_input_tokens": 64000,
        "timeout": 600, "provider": "", "model": "", "reasoning_effort": "high",
        "workspace": "", "platforms": ["telegram"],
        "doctrine_files": ["AGENTS.md", "doctrina/memoria.md", "doctrina/conocimiento.md", "SOUL.md"],
        "wiki_path": "wiki", "library_path": "biblioteca", "journal_path": "bitacora.md",
    }
