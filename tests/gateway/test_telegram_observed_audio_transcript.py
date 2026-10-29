"""Observed (unmentioned) group audio is transcribed so passive ingestion keeps what was said.

Regression cover for a fork delta: without it the observed context only carried the cached
file path, and the agent could not answer "what did X say in that voice note?".
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.gateway.test_telegram_group_gating import _make_adapter


def _install_transcriber(monkeypatch, result):
    mod = ModuleType("tools.transcription_tools")
    mod.transcribe_audio = lambda path, *a, **k: result
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", mod)


@pytest.mark.asyncio
async def test_transcript_is_appended_to_observed_text(monkeypatch):
    _install_transcriber(monkeypatch, {"success": True, "transcript": " hola equipo "})
    adapter = _make_adapter()
    event = SimpleNamespace(text="[Bob|222]\n")
    await adapter._transcribe_observed_audio(event, "/tmp/a.ogg")
    assert '[Audio transcript: "hola equipo"]' in event.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [{"success": False, "error": "stt down"}, {"success": True, "transcript": "  "}],
)
async def test_failed_or_empty_transcript_leaves_text_untouched(monkeypatch, result):
    _install_transcriber(monkeypatch, result)
    adapter = _make_adapter()
    event = SimpleNamespace(text="[Bob|222]\n")
    await adapter._transcribe_observed_audio(event, "/tmp/a.ogg")
    assert event.text == "[Bob|222]\n"


@pytest.mark.asyncio
async def test_transcriber_exception_is_not_fatal(monkeypatch):
    mod = ModuleType("tools.transcription_tools")

    def boom(*a, **k):
        raise RuntimeError("boom")

    mod.transcribe_audio = boom
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", mod)
    adapter = _make_adapter()
    event = SimpleNamespace(text="x")
    await adapter._transcribe_observed_audio(event, "/tmp/a.ogg")
    assert event.text == "x"
