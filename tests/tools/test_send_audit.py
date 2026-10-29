"""Outbound audit follows the active Hermes profile and the resolved destination."""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.send_message_tool import send_message_tool


def test_send_audit_stays_with_profile_across_switches(tmp_path):
    config = SimpleNamespace(
        platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True, token="test", extra={})},
        get_home_channel=lambda _platform: None,
    )
    homes = [tmp_path / "first", tmp_path / "second", tmp_path / "first"]
    sender = AsyncMock(side_effect=[
        {"success": True, "message_id": "one"},
        {"success": False, "error": "rejected"},
        {"success": True, "message_id": "three"},
    ])
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=asyncio.run), \
         patch("tools.send_message_tool._send_to_platform", new=sender), \
         patch("tools.send_message_tool._mirror_sent_message", return_value=False):
        for home, message in zip(homes, ("first", "second", "third")):
            token = set_hermes_home_override(home)
            try:
                result = json.loads(send_message_tool({
                    "action": "send", "target": "telegram:12345", "message": message,
                }))
                assert "success" in result
            finally:
                reset_hermes_home_override(token)

    def rows(home):
        with sqlite3.connect(home / "audit.db") as conn:
            return conn.execute(
                "SELECT target_chat, content_preview, success, message_id "
                "FROM send_events ORDER BY id"
            ).fetchall()

    assert rows(homes[0]) == [
        ("12345", "first", 1, "one"), ("12345", "third", 1, "three"),
    ]
    assert rows(homes[1]) == [("12345", "second", 0, None)]
    if os.name == "posix":
        assert (homes[0] / "audit.db").stat().st_mode & 0o077 == 0
