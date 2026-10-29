"""An untargeted send resolves to the session's chat, not the home channel.

Regression cover for a cross-chat leak: ``send_message`` without an explicit
target used to fall straight through to the configured home channel. Inside a
gateway session that means a reply meant for whoever is talking can surface in
the home chat instead. The home channel remains the fallback when there is no
session (cron, CLI) or when the session belongs to another platform.
"""

import sys
from types import ModuleType

import pytest

from tools.send_message_tool import _session_chat_for


@pytest.fixture
def session_env(monkeypatch):
    """Install a fake ``gateway.session_context.get_session_env``.

    The real module may be absent in a bare env, and importing the gateway
    just to read two strings is more coupling than this needs — the helper
    imports it lazily, so a stub module is enough.
    """

    def _install(mapping):
        mod = ModuleType("gateway.session_context")
        mod.get_session_env = lambda name, default="": mapping.get(name, default)
        monkeypatch.setitem(sys.modules, "gateway.session_context", mod)

    return _install


def test_prefers_session_chat_on_same_platform(session_env):
    session_env({"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "12345"})
    assert _session_chat_for("telegram") == "12345"


def test_ignores_session_from_another_platform(session_env):
    """A WhatsApp session must not capture a Telegram send."""
    session_env({"HERMES_SESSION_PLATFORM": "whatsapp", "HERMES_SESSION_CHAT_ID": "99999"})
    assert _session_chat_for("telegram") is None


def test_no_session_falls_back(session_env):
    """Cron / CLI: no session context -> caller uses the home channel."""
    session_env({})
    assert _session_chat_for("telegram") is None


def test_blank_chat_id_falls_back(session_env):
    """A session with no chat id is not a target."""
    session_env({"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "   "})
    assert _session_chat_for("telegram") is None


def test_platform_match_is_case_insensitive(session_env):
    session_env({"HERMES_SESSION_PLATFORM": "Telegram", "HERMES_SESSION_CHAT_ID": "12345"})
    assert _session_chat_for("telegram") == "12345"


def test_unimportable_session_context_is_not_fatal(monkeypatch):
    """Outside a gateway the import fails; that must not break sending."""
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    assert _session_chat_for("telegram") is None
