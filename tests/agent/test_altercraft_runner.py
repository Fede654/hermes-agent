from unittest.mock import MagicMock, patch

import pytest

from agent.altercraft_runner import ReactiveLoop


def test_respond_emits_in_world_chat_when_agent_chat_raises(tmp_path):
    """If agent.chat() raises, _respond must POST a crash message to /action/chat."""
    agent = MagicMock()
    agent.chat.side_effect = RuntimeError("model timeout")

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    loop = ReactiveLoop(
        agent=agent,
        api_url="http://127.0.0.1:9999",
        mc_username="TestBot",
        session_dir=session_dir,
    )

    post_calls = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            post_calls.append({"url": url, "kwargs": kwargs})
            return FakeResponse()

    event = {"text": "hello TestBot", "from": "Alice"}

    with patch("agent.altercraft_runner.httpx.Client", FakeClient):
        loop._respond(event)

    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "http://127.0.0.1:9999/action/chat"
    body = post_calls[0]["kwargs"]["json"]
    assert "crashed" in body["message"]
    assert "RuntimeError" in body["message"]

    # Profile-level error marker should have been written
    profile_dir = session_dir.parents[1]
    events_file = profile_dir / "events.jsonl"
    assert events_file.exists()
    lines = [ln for ln in events_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "RuntimeError" in lines[0]
    assert "model timeout" in lines[0]


def test_respond_swallows_secondary_http_failure(tmp_path):
    """If both agent.chat() and the recovery POST fail, _respond must not re-raise."""
    agent = MagicMock()
    agent.chat.side_effect = RuntimeError("model timeout")

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    loop = ReactiveLoop(
        agent=agent,
        api_url="http://127.0.0.1:9999",
        mc_username="TestBot",
        session_dir=session_dir,
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            raise ConnectionError("network down")

    event = {"text": "hello TestBot", "from": "Alice"}

    with patch("agent.altercraft_runner.httpx.Client", FakeClient):
        # Must not raise even though the fallback HTTP call also fails
        loop._respond(event)


# ==============================================================================
# HRM-71 — addressed-chat detection
# ==============================================================================

def test_is_addressed_detects_comma_prefix(tmp_path):
    """Hermes-Clio, ... must be recognised as addressed."""
    loop = ReactiveLoop(
        agent=MagicMock(),
        api_url="http://127.0.0.1:9999",
        mc_username="Hermes-Clio",
        session_dir=tmp_path,
    )
    ev = {"message": "Hermes-Clio, please look around", "from": "Alice"}
    assert loop._is_addressed(ev) is True


def test_is_addressed_detects_colon_prefix(tmp_path):
    """Hermes-Clio: ... must be recognised as addressed."""
    loop = ReactiveLoop(
        agent=MagicMock(),
        api_url="http://127.0.0.1:9999",
        mc_username="Hermes-Clio",
        session_dir=tmp_path,
    )
    ev = {"message": "Hermes-Clio: look around", "from": "Alice"}
    assert loop._is_addressed(ev) is True


def test_is_addressed_detects_targets_list(tmp_path):
    """Explicit routing via targets list must be recognised."""
    loop = ReactiveLoop(
        agent=MagicMock(),
        api_url="http://127.0.0.1:9999",
        mc_username="Hermes-Clio",
        session_dir=tmp_path,
    )
    ev = {"message": "hello", "from": "Alice", "targets": ["hermes-clio"]}
    assert loop._is_addressed(ev) is True


def test_is_addressed_detects_whisper_flag(tmp_path):
    """Whisper events (whisper: true) must be recognised as addressed."""
    loop = ReactiveLoop(
        agent=MagicMock(),
        api_url="http://127.0.0.1:9999",
        mc_username="Hermes-Clio",
        session_dir=tmp_path,
    )
    ev = {"message": "psst", "from": "Alice", "whisper": True}
    assert loop._is_addressed(ev) is True


def test_reactive_loop_fetches_and_detects_addressed_chat(tmp_path):
    """Mock /chat response containing 'Hermes-Clio, ...' must pass the addressed check."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    loop = ReactiveLoop(
        agent=MagicMock(),
        api_url="http://127.0.0.1:9999",
        mc_username="Hermes-Clio",
        session_dir=session_dir,
    )

    fake_body = {
        "ok": True,
        "data": {
            "messages": [
                {
                    "time": 9999999999999,
                    "from": "Alice",
                    "message": "Hermes-Clio, please look around",
                }
            ]
        }
    }

    class FakeResponse:
        status_code = 200
        def json(self):
            return fake_body

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, **kwargs):
            return FakeResponse()

    with patch("agent.altercraft_runner.httpx.Client", FakeClient):
        loop.last_seen_ms = 0
        events = loop._fetch_events()
        assert len(events) == 1
        assert loop._is_addressed(events[0]) is True
