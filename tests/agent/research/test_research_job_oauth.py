"""Verify provider-neutral config handling for detached research jobs.

The spec generator inherits the user's configured delegation runtime first,
then the main runtime. It must not pin provider-specific API keys from env:
credential resolution belongs to the normal Hermes provider/auth machinery in
AIAgent, not to the research-job tool.
"""
from __future__ import annotations

import json


def test_spec_omits_api_key_when_env_unset(monkeypatch, tmp_path):
    """_load_config_for_job must not pin provider-specific API keys."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

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
        f"Spec must not pin provider-specific API keys (got {spec!r}). "
        "Credential resolution belongs to the standard Hermes provider/auth path."
    )
    assert spec["provider"] == "kimi-coding"
    assert spec["model"] == "kimi-k2.6"


def test_spec_prefers_delegation_runtime(monkeypatch, tmp_path):
    """Detached research jobs inherit delegation runtime when configured."""
    config_dir = tmp_path / "hermes-home"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "model:\n"
        "  default: main-model\n"
        "  provider: main-provider\n"
        "delegation:\n"
        "  model: delegate-model\n"
        "  provider: delegate-provider\n"
        "  base_url: https://example.invalid/v1\n"
        "  api_mode: chat_completions\n"
    )
    monkeypatch.setattr(
        "tools.research_job_tool.get_hermes_home",
        lambda: config_dir,
    )

    from tools.research_job_tool import _load_config_for_job
    spec = _load_config_for_job()
    assert spec == {
        "model": "delegate-model",
        "provider": "delegate-provider",
        "base_url": "https://example.invalid/v1",
        "api_mode": "chat_completions",
    }


def test_action_start_does_not_write_null_api_key(monkeypatch, tmp_path):
    """When resolved cfg has no api_key, persisted spec.json has no null api_key."""
    monkeypatch.setattr(
        "tools.research_job_tool._load_config_for_job",
        lambda: {
            "model": "test-model",
            "provider": "test-provider",
            "base_url": "https://example.invalid/v1",
        },
    )
    monkeypatch.setattr(
        "tools.research_job_tool._job_dir",
        lambda job_id: tmp_path / "jobs" / job_id,
    )

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
    assert spec["provider"] == "test-provider"
