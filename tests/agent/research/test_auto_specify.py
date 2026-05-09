"""Tests for agent.research.auto_specify."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.research.auto_specify import auto_specify_topic, _extract_json_blob


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


class TestExtractJsonBlob:
    def test_clean_json(self):
        out = _extract_json_blob('{"a": 1}')
        assert out == {"a": 1}

    def test_fenced_json(self):
        out = _extract_json_blob('```json\n{"a": 1}\n```')
        assert out == {"a": 1}

    def test_prose_around(self):
        out = _extract_json_blob('Sure, here is: {"a": 1} done.')
        assert out == {"a": 1}

    def test_empty_returns_none(self):
        assert _extract_json_blob("") is None

    def test_unparseable_returns_none(self):
        assert _extract_json_blob("not json at all") is None

    def test_array_returns_none(self):
        # We only accept top-level objects.
        assert _extract_json_blob("[1, 2, 3]") is None


class TestAutoSpecifyTopic:
    def test_returns_structured_spec(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = _fake_aux_response(
            '```json\n{"deliverable": "Python function classify(payload)",'
            '"metric_key": "pass_rate", "metric_direction": "maximize",'
            '"task_type": "code", "evaluation_mode": "self_report"}\n```'
        )
        with patch(
            "agent.research.auto_specify.get_text_auxiliary_client",
            return_value=(fake_client, "test-model"),
        ):
            out = auto_specify_topic("classify daemoncraft heartbeat events")
        assert out is not None
        assert out["deliverable"] == "Python function classify(payload)"
        assert out["metric_key"] == "pass_rate"
        assert out["task_type"] == "code"

    def test_returns_none_on_aux_error(self):
        with patch(
            "agent.research.auto_specify.get_text_auxiliary_client",
            side_effect=RuntimeError("no aux configured"),
        ):
            assert auto_specify_topic("vague topic") is None

    def test_returns_none_when_aux_returns_none_client(self):
        with patch(
            "agent.research.auto_specify.get_text_auxiliary_client",
            return_value=(None, None),
        ):
            assert auto_specify_topic("vague topic") is None

    def test_returns_none_on_unparseable_output(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = _fake_aux_response(
            "I think you should make a thing that scores high"
        )
        with patch(
            "agent.research.auto_specify.get_text_auxiliary_client",
            return_value=(fake_client, "test-model"),
        ):
            assert auto_specify_topic("vague") is None

    def test_returns_none_for_empty_topic(self):
        # Even with a working aux, empty input is rejected before the call.
        fake_client = MagicMock()
        with patch(
            "agent.research.auto_specify.get_text_auxiliary_client",
            return_value=(fake_client, "test-model"),
        ):
            assert auto_specify_topic("") is None
            assert auto_specify_topic("   ") is None
            fake_client.chat.completions.create.assert_not_called()

    def test_swallows_api_exception(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = RuntimeError("rate limit")
        with patch(
            "agent.research.auto_specify.get_text_auxiliary_client",
            return_value=(fake_client, "test-model"),
        ):
            assert auto_specify_topic("topic") is None
