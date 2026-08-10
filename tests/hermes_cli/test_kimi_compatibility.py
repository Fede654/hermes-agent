import json


def _write_kimi_state(tmp_path, *, device_id: str | None = "device-123", credentials=None):
    credential_path = tmp_path / "credentials" / "kimi-code.json"
    credential_path.parent.mkdir(parents=True)
    credential_path.write_text(
        json.dumps(credentials or {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": 4_000_000_000,
        })
    )
    device_path = tmp_path / "device_id"
    if device_id is not None:
        device_path.write_text(device_id)
    return credential_path, device_path


def test_kimi_compatibility_report_contains_metadata_only(monkeypatch, tmp_path):
    from hermes_cli import auth

    _write_kimi_state(tmp_path)
    monkeypatch.setattr(auth, "_kimi_cli_credentials_path", lambda: tmp_path / "credentials" / "kimi-code.json")
    monkeypatch.setattr(auth, "_kimi_cli_device_id_path", lambda: tmp_path / "device_id")
    monkeypatch.setattr(auth, "_kimi_cli_version", lambda: "0.34.0")
    monkeypatch.setattr(auth, "kimi_coding_default_headers", lambda: {
        "User-Agent": "kimi-code-cli/0.34.0",
        "X-Msh-Platform": "kimi_cli",
        "X-Msh-Version": "0.34.0",
        "X-Msh-Device-Name": "test",
        "X-Msh-Device-Model": "test",
        "X-Msh-Os-Version": "test",
        "X-Msh-Device-Id": "device-123",
    })

    report = auth.kimi_coding_compatibility_report()

    assert report["ok"] is True
    assert report["observed"]["has_access_token"] is True
    assert report["observed"]["has_refresh_token"] is True
    assert "access-token" not in json.dumps(report)
    assert "refresh-token" not in json.dumps(report)
    assert "device-123" not in json.dumps(report)


def test_kimi_compatibility_report_rejects_missing_device_id(monkeypatch, tmp_path):
    from hermes_cli import auth

    _write_kimi_state(tmp_path, device_id=None)
    monkeypatch.setattr(auth, "_kimi_cli_credentials_path", lambda: tmp_path / "credentials" / "kimi-code.json")
    monkeypatch.setattr(auth, "_kimi_cli_device_id_path", lambda: tmp_path / "device_id")
    monkeypatch.setattr(auth, "_kimi_cli_version", lambda: "0.34.0")
    monkeypatch.setattr(auth, "kimi_coding_default_headers", lambda: {
        "User-Agent": "kimi-code-cli/0.34.0",
        "X-Msh-Platform": "kimi_cli",
        "X-Msh-Version": "0.34.0",
        "X-Msh-Device-Name": "test",
        "X-Msh-Device-Model": "test",
        "X-Msh-Os-Version": "test",
    })

    report = auth.kimi_coding_compatibility_report()

    assert report["ok"] is False
    assert any("device_id" in error for error in report["errors"])


def test_kimi_compatibility_report_rejects_invalid_credentials(monkeypatch, tmp_path):
    from hermes_cli import auth

    _write_kimi_state(tmp_path, credentials={"expires_at": 4_000_000_000})
    monkeypatch.setattr(auth, "_kimi_cli_credentials_path", lambda: tmp_path / "credentials" / "kimi-code.json")
    monkeypatch.setattr(auth, "_kimi_cli_device_id_path", lambda: tmp_path / "device_id")
    monkeypatch.setattr(auth, "_kimi_cli_version", lambda: "0.34.0")
    monkeypatch.setattr(auth, "kimi_coding_default_headers", lambda: {
        "User-Agent": "kimi-code-cli/0.34.0",
        "X-Msh-Platform": "kimi_cli",
        "X-Msh-Version": "0.34.0",
        "X-Msh-Device-Name": "test",
        "X-Msh-Device-Model": "test",
        "X-Msh-Os-Version": "test",
        "X-Msh-Device-Id": "device-123",
    })

    report = auth.kimi_coding_compatibility_report()

    assert report["ok"] is False
    assert any("access_token" in error for error in report["errors"])