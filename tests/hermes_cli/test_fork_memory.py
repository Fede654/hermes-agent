"""A failed provider installation must leave the working native memory selected."""
import subprocess

import pytest

from hermes_cli import fork_memory
from hermes_cli.config import read_user_config_raw
from hermes_cli.plugins_cmd_git import _write_install_metadata

SPEC = {"source": "https://github.com/example/memory.git", "revision": "a" * 40}


def test_selection_waits_for_verification_and_repeat_preserves_data(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("# Keep my settings\nmemory:\n  memory_enabled: true\n  user_profile_enabled: true\nterminal:\n  timeout: 73\n")
    installs = []

    def install(source, **kwargs):
        installs.append(kwargs["ref"])
        (home / "plugins/hmk-memory").mkdir(parents=True, exist_ok=True)
        _write_install_metadata({"hmk-memory": {"source": source, "revision": kwargs["ref"]}})

    def verify(actual_home):
        assert actual_home == home
        assert read_user_config_raw(config)["memory"]["memory_enabled"]
        raise subprocess.CalledProcessError(1, "verification")

    monkeypatch.setattr("hermes_cli.plugins_cmd_install._install_plugin_core", install)
    monkeypatch.setattr("hermes_cli.memory_setup.prepare_memory_provider_dependencies", lambda name: ({}, None))
    monkeypatch.setattr(fork_memory, "_verify_in_selected_environment", verify)
    before = config.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        fork_memory.install_default_memory(home, specification=SPEC)
    assert config.read_bytes() == before

    monkeypatch.setattr(fork_memory, "_verify_in_selected_environment", lambda home: None)
    result = fork_memory.install_default_memory(home, specification=SPEC)
    marker = home / "hmk-memory/library.db"
    marker.parent.mkdir()
    marker.write_bytes(b"existing durable memory")
    fork_memory.install_default_memory(home, specification=SPEC)
    assert result["status"] == "ready"
    assert installs == [SPEC["revision"]]
    assert marker.read_bytes() == b"existing durable memory"
    after = read_user_config_raw(config)
    assert after["memory"]["provider"] == "hmk-memory"
    assert not after["memory"]["memory_enabled"] and not after["memory"]["user_profile_enabled"]
    assert after["terminal"]["timeout"] == 73
    assert config.read_text().startswith("# Keep my settings")
    def fail_upgrade(home):
        raise subprocess.CalledProcessError(1, "verification")

    monkeypatch.setattr(fork_memory, "_verify_in_selected_environment", fail_upgrade)
    with pytest.raises(subprocess.CalledProcessError):
        fork_memory.install_default_memory(home, specification={**SPEC, "revision": "b" * 40})
    assert installs[-2:] == ["b" * 40, SPEC["revision"]]
    assert marker.read_bytes() == b"existing durable memory"
    assert read_user_config_raw(config) == after



def test_an_explicit_provider_choice_is_preserved(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: builtin\n")
    before = config.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("An explicit provider choice must not trigger installation")

    monkeypatch.setattr("hermes_cli.plugins_cmd_install._install_plugin_core", forbidden)
    assert fork_memory.install_default_memory(tmp_path, specification=SPEC)["status"] == "preserved"
    assert config.read_bytes() == before
