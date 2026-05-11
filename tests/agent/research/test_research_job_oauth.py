"""Verify Kimi OAuth path for detached research jobs.

Per nicoechaniz's PR #1 review (2026-05-10): the default Kimi auth path
on this fork is OAuth via ~/.kimi/credentials/kimi-code.json, not
KIMI_API_KEY. The spec generator must not pin an empty api_key when env
is unset — otherwise the auxiliary_client treats the empty string as
an explicit override and bypasses the OAuth resolver.

The unit tests in this file run in CI. The E2E test is marked as
'integration' (skipped by default) and runs a minimal detached job
end-to-end with KIMI_API_KEY unset — opt in with
    pytest -m integration tests/agent/research/test_research_job_oauth.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit tests — pin spec generator behavior
# ---------------------------------------------------------------------------


def test_spec_omits_api_key_when_env_unset(monkeypatch, tmp_path):
    """When KIMI_API_KEY is unset, _load_config_for_job must NOT pin
    api_key in the returned dict — let auxiliary_client resolve OAuth."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    # Provide a minimal config.yaml so the function doesn't return {}.
    config_dir = tmp_path / "hermes-home"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "model:\n  default: kimi-k2.6\n  provider: kimi-coding\n"
    )
    monkeypatch.setattr(
        "tools.research_job_tool.get_hermes_home",
        lambda: config_dir,
    )

    from tools.research_job_tool import _load_config_for_job
    spec = _load_config_for_job()
    assert "api_key" not in spec, (
        f"Spec must omit api_key when KIMI_API_KEY is unset (got {spec!r}). "
        "Pinning an empty api_key would force auxiliary_client to treat it "
        "as an explicit override and skip OAuth resolution."
    )
    # Sanity: other fields still present
    assert spec["provider"] == "kimi-coding"
    assert spec["model"] == "kimi-k2.6"


def test_spec_pins_api_key_when_env_set(monkeypatch, tmp_path):
    """When KIMI_API_KEY is set, it must flow into the spec as an
    explicit override (callers who set the env var want it honored)."""
    monkeypatch.setenv("KIMI_API_KEY", "test-api-key-value")

    config_dir = tmp_path / "hermes-home"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "model:\n  default: kimi-k2.6\n  provider: kimi-coding\n"
    )
    monkeypatch.setattr(
        "tools.research_job_tool.get_hermes_home",
        lambda: config_dir,
    )

    from tools.research_job_tool import _load_config_for_job
    spec = _load_config_for_job()
    assert spec.get("api_key") == "test-api-key-value"


def test_spec_strips_whitespace_api_key(monkeypatch, tmp_path):
    """Whitespace-only KIMI_API_KEY must be treated as unset, not as a
    valid (broken) override."""
    monkeypatch.setenv("KIMI_API_KEY", "   ")

    config_dir = tmp_path / "hermes-home"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "model:\n  default: kimi-k2.6\n  provider: kimi-coding\n"
    )
    monkeypatch.setattr(
        "tools.research_job_tool.get_hermes_home",
        lambda: config_dir,
    )

    from tools.research_job_tool import _load_config_for_job
    spec = _load_config_for_job()
    assert "api_key" not in spec


def test_action_start_does_not_write_null_api_key(monkeypatch, tmp_path):
    """When the resolved cfg has no api_key, the persisted spec.json
    must not contain a null api_key entry."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    # Stub config loader to mimic 'env unset, OAuth path' resolution.
    monkeypatch.setattr(
        "tools.research_job_tool._load_config_for_job",
        lambda: {
            "model": "kimi-k2.6",
            "provider": "kimi-coding",
            "base_url": "https://api.kimi.com/coding/v1",
        },
    )
    monkeypatch.setattr(
        "tools.research_job_tool._job_dir",
        lambda job_id: tmp_path / "jobs" / job_id,
    )

    # Stub the terminal-spawn so the test doesn't actually fork a runner.
    import json
    monkeypatch.setattr(
        "tools.terminal_tool.terminal_tool",
        lambda **kw: json.dumps({"session_id": "stub-session", "pid": 12345}),
    )

    from tools.research_job_tool import _action_start

    out = _action_start({
        "topic": "trivial sort fn",
        "deliverable": "a Python sort function",
        "metric_key": "pass_rate",
        "acceptance_criterion": "pass_rate >= 1.0",
        "max_iterations": 1,
    })

    result = json.loads(out)
    job_id = result["job_id"]
    spec_path = tmp_path / "jobs" / job_id / "job.json"
    spec = json.loads(spec_path.read_text())

    assert "api_key" not in spec, (
        f"Spec must not write null api_key to job.json (got {spec!r})"
    )


# ---------------------------------------------------------------------------
# E2E test — opt-in via -m integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not Path("~/.kimi/credentials/kimi-code.json").expanduser().exists(),
    reason="requires Kimi OAuth credentials at ~/.kimi/credentials/kimi-code.json",
)
def test_detached_job_resolves_kimi_oauth_without_api_key(monkeypatch):
    """A detached research_job must succeed with KIMI_API_KEY unset,
    relying on Kimi OAuth credentials at ~/.kimi/credentials/kimi-code.json.

    Opt in: pytest -m integration tests/agent/research/test_research_job_oauth.py
    """
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    from tools.research_job_tool import research_job
    import json

    start_out = research_job(
        action="start",
        topic="trivial sort fn for ints",
        deliverable="a Python function sort_ints(xs: list[int]) -> list[int]",
        metric_key="pass_rate",
        acceptance_criterion="pass_rate >= 1.0",
        max_iterations=1,
        task_type="code",
    )
    start = json.loads(start_out)
    job_id = start["job_id"]

    deadline = time.time() + 300  # 5 min
    final = None
    while time.time() < deadline:
        status_out = research_job(action="status", job_id=job_id)
        status = json.loads(status_out)
        if status.get("state") in ("completed", "failed", "done"):
            final = status
            break
        time.sleep(5)

    assert final is not None, (
        f"Job {job_id} did not finish within 5 minutes — likely Kimi OAuth "
        "resolution is broken for spawned jobs. Check ~/.hermes/research-jobs/"
        f"{job_id}/runner.log."
    )
    assert final.get("state") in ("completed", "done"), (
        f"Detached job failed with KIMI_API_KEY unset: {final!r}. "
        f"Inspect ~/.hermes/research-jobs/{job_id}/runner.log."
    )
