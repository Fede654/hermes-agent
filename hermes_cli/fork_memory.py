"""Install this distribution's pinned default memory through Hermes' plugin/PM path.

Selection is committed only after a fresh interpreter verifies the provider. An
explicit choice of another provider is preserved on repeated installations.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def verify_default_memory(home: Path) -> dict:
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("hmk-memory")
    if provider is None or not provider.is_available():
        raise RuntimeError("HMK provider is not available after installation")
    try:
        provider.initialize("installation-check", hermes_home=str(home))
        result = json.loads(provider.handle_tool_call("_hmk_installation_check", {}))
        if not result.get("success"):
            raise RuntimeError(result.get("error", "HMK installation check failed"))
        return result
    finally:
        provider.shutdown()


def install_default_memory(home: Path, *, specification: dict | None = None) -> dict:
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope

    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(home)
    secrets = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        return _install(home, specification=specification)
    finally:
        reset_secret_scope(secrets)
        reset_hermes_home_override(token)


def _install(home: Path, *, specification: dict | None) -> dict:
    from hermes_cli.config import atomic_config_write, read_user_config_raw
    from hermes_cli.plugins_cmd_install import _install_plugin_core
    from hermes_cli.plugins_cmd_git import _read_install_metadata, _canonical_source
    from hermes_cli.plugins_cmd import _resolve_git_url
    from hermes_cli.memory_setup import prepare_memory_provider_dependencies

    spec = specification or json.loads((_ROOT / "fork-memory.json").read_text(encoding="utf-8-sig"))
    if not re.fullmatch(r"[0-9a-f]{40}", spec["revision"]):
        raise ValueError("Default memory must be pinned to an exact commit")
    config_path = home / "config.yaml"
    config = read_user_config_raw(config_path)
    memory = config.get("memory", {})
    if "provider" in memory and memory["provider"] != "hmk-memory":
        return {"status": "preserved", "provider": memory["provider"]}

    installed = _read_install_metadata().get("hmk-memory", {})
    target = home / "plugins" / "hmk-memory"
    expected_source = _canonical_source(*_resolve_git_url(spec["source"]))
    if target.exists() and installed.get("source") != expected_source:
        raise RuntimeError("Existing HMK installation has a different source; preserve it and migrate explicitly")
    try:
        if not target.is_dir() or installed.get("revision") != spec["revision"]:
            # Standard publication guards preserve edited trees and enforce dependency admission.
            _install_plugin_core(spec["source"], force=target.exists(), accept_caution=False,
                                 ref=spec["revision"], assume_deps_consent=True)
        prepare_memory_provider_dependencies("hmk-memory")
        _verify_in_selected_environment(home)
    except Exception:
        # An active provider must not be left on an unverified upgrade. Native
        # selection has not changed yet; restore an incumbent through the same PM path.
        current = _read_install_metadata().get("hmk-memory", {})
        if installed.get("revision") and current.get("revision") != installed["revision"]:
            _install_plugin_core(spec["source"], force=True, accept_caution=False,
                                 ref=installed["revision"], assume_deps_consent=True)
            prepare_memory_provider_dependencies("hmk-memory")
        raise

    # Re-read after PM: its selection transaction can add unrelated config keys.
    config = read_user_config_raw(config_path)
    memory = config.get("memory", {})
    if "provider" in memory and memory["provider"] != "hmk-memory":
        return {"status": "preserved", "provider": memory["provider"]}
    config.setdefault("memory", {}).update({
        "provider": "hmk-memory", "memory_enabled": False, "user_profile_enabled": False,
    })
    atomic_config_write(config_path, config)
    return {"status": "ready", "provider": "hmk-memory", "revision": spec["revision"]}


def _verify_in_selected_environment(home: Path) -> None:
    from pm.environments import project_python

    # PM may select a new environment; verify there, not in this caller's old imports.
    command = [str(project_python(_ROOT)), "-m", "hermes_cli.fork_memory", "--verify-only", "--home", str(home)]
    subprocess.run(command, cwd=_ROOT, check=True, timeout=600)


def main():
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    home = (args.home or get_hermes_home()).resolve()
    if not args.verify_only:
        print(json.dumps(install_default_memory(home)))
        return
    token = set_hermes_home_override(home)
    secrets = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        print(json.dumps(verify_default_memory(home)))
    finally:
        reset_secret_scope(secrets)
        reset_hermes_home_override(token)


if __name__ == "__main__":
    main()
