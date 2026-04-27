"""Tests for tools/altercraft_tool.py — focused on _req retry behaviour (HRM-64)."""

import pytest
import httpx
from unittest import mock

from tools.altercraft_tool import _req


# ==============================================================================
# HRM-65 — Safe coercion helpers
# ==============================================================================

from tools.altercraft_tool import _safe_int, _safe_float, _safe_bool


class TestSafeInt:
    def test_scalar_happy_path(self):
        assert _safe_int("42", 0) == 42
        assert _safe_int(7, 0) == 7

    def test_dict_input_returns_default(self):
        assert _safe_int({"foo": "bar"}, 99) == 99

    def test_list_input_returns_default(self):
        assert _safe_int([1, 2, 3], 99) == 99

    def test_none_input_returns_default(self):
        assert _safe_int(None, 99) == 99

    def test_unparseable_string_returns_error(self):
        result = _safe_int("foo", 99)
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "foo" in result

    def test_max_clamping(self):
        assert _safe_int("100", 0, max=10) == 10
        assert _safe_int("-5", 0, max=10) == 0
        assert _safe_int("5", 0, max=10) == 5


class TestSafeFloat:
    def test_scalar_happy_path(self):
        assert _safe_float("3.14", 0.0) == 3.14
        assert _safe_float(2, 0.0) == 2.0

    def test_dict_input_returns_default(self):
        assert _safe_float({"foo": "bar"}, 99.0) == 99.0

    def test_list_input_returns_default(self):
        assert _safe_float([1, 2, 3], 99.0) == 99.0

    def test_none_input_returns_default(self):
        assert _safe_float(None, 99.0) == 99.0

    def test_unparseable_string_returns_error(self):
        result = _safe_float("foo", 99.0)
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "foo" in result


class TestSafeBool:
    def test_scalar_happy_path(self):
        assert _safe_bool(True, False) is True
        assert _safe_bool(False, True) is False

    def test_dict_input_returns_default(self):
        assert _safe_bool({"foo": "bar"}, True) is True
        assert _safe_bool({"foo": "bar"}, False) is False

    def test_list_input_returns_default(self):
        assert _safe_bool([1, 2, 3], True) is True
        assert _safe_bool([1, 2, 3], False) is False

    def test_none_input_returns_default(self):
        assert _safe_bool(None, True) is True
        assert _safe_bool(None, False) is False

    def test_string_truthy_values(self):
        assert _safe_bool("true", False) is True
        assert _safe_bool("1", False) is True
        assert _safe_bool("yes", False) is True
        assert _safe_bool("TRUE", False) is True

    def test_string_falsy_values(self):
        assert _safe_bool("false", True) is False
        assert _safe_bool("0", True) is False
        assert _safe_bool("no", True) is False
        assert _safe_bool("FALSE", True) is False

    def test_unparseable_string_returns_default(self):
        assert _safe_bool("foo", True) is True
        assert _safe_bool("foo", False) is False

    def test_numeric_values(self):
        assert _safe_bool(1, False) is True
        assert _safe_bool(0, True) is False
        assert _safe_bool(0.0, True) is False


class TestFuzzDictArgs:
    """Feed dict-shaped numeric params to handlers and assert no TypeError."""

    def test_dict_args_do_not_crash_handlers(self):
        from tools.altercraft_tool import (
            _h_look, _h_goto, _h_collect, _h_smelt_raw, _h_flee,
        )

        with mock.patch(
            "tools.altercraft_tool._req",
            return_value=(False, "Error: mock server unavailable"),
        ):
            # _h_look: radius is a dict -> should use default, then _req returns error
            result = _h_look({"radius": {"foo": "bar"}})
            assert isinstance(result, str)
            assert result.startswith("Error:")

            # _h_goto: near is a dict -> default False; x,y,z missing -> error
            result = _h_goto({"near": {"foo": "bar"}})
            assert isinstance(result, str)
            assert result.startswith("Error:")

            # _h_collect: count is a dict -> default 1; block provided
            result = _h_collect({"block": "stone", "count": {"foo": "bar"}})
            assert isinstance(result, str)
            assert result.startswith("Error:")

            # _h_smelt_raw: count is a dict -> default 1; item provided
            result = _h_smelt_raw({"item": "raw_iron", "count": {"foo": "bar"}})
            assert isinstance(result, str)
            assert result.startswith("Error:")

            # _h_flee: distance is a dict -> default 0.0
            result = _h_flee({"distance": {"foo": "bar"}})
            assert isinstance(result, str)
            assert result.startswith("Error:")


def _ok_response(json_data=None, text=""):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.text = text
    return resp


def _err_response(status_code, text=""):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.json.side_effect = ValueError("no json")
    resp.text = text
    return resp


def test_req_retries_once_on_timeout_then_succeeds():
    """Timeout on first call -> retry once -> succeed."""
    ok_resp = _ok_response(json_data={"ok": True})
    with mock.patch(
        "tools.altercraft_tool.httpx.Client.request",
        side_effect=[httpx.TimeoutException("timed out"), ok_resp],
    ) as mock_request:
        ok, body = _req("GET", "/status")

    assert ok is True
    assert body == {"ok": True}
    assert mock_request.call_count == 2


def test_req_does_not_retry_on_4xx():
    """4xx responses are not transient — no retry."""
    not_found = _err_response(404, text="not found")
    with mock.patch(
        "tools.altercraft_tool.httpx.Client.request",
        return_value=not_found,
    ) as mock_request:
        ok, body = _req("GET", "/missing")

    assert ok is False
    assert "404" in body
    assert mock_request.call_count == 1


def test_req_gives_up_after_one_retry():
    """Two consecutive timeouts -> failure after single retry."""
    with mock.patch(
        "tools.altercraft_tool.httpx.Client.request",
        side_effect=[
            httpx.TimeoutException("timed out"),
            httpx.TimeoutException("timed out again"),
        ],
    ) as mock_request:
        ok, body = _req("POST", "/action")

    assert ok is False
    assert "timeout" in body
    assert mock_request.call_count == 2


# ==============================================================================
# HRM-67 — per-call api_url override
# ==============================================================================

from tools.altercraft_tool import _resolve_api_url, _req, _h_status


class TestResolveApiUrl:
    def test_resolve_api_url_prefers_args_over_env(self, monkeypatch):
        monkeypatch.setenv("MC_API_URL", "http://env-bot:3001")
        result = _resolve_api_url({"api_url": "http://args-bot:3002"})
        assert result == "http://args-bot:3002"

    def test_resolve_api_url_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("MC_API_URL", "http://env-bot:3001")
        result = _resolve_api_url({})
        assert result == "http://env-bot:3001"

    def test_resolve_api_url_ignores_non_http(self, monkeypatch):
        monkeypatch.setenv("MC_API_URL", "http://env-bot:3001")
        result = _resolve_api_url({"api_url": "not-a-url"})
        assert result == "http://env-bot:3001"

    def test_resolve_api_url_strips_trailing_slash(self):
        result = _resolve_api_url({"api_url": "http://bot:3001/"})
        assert result == "http://bot:3001"


def test_req_uses_api_url_kwarg():
    """_req should hit the api_url passed instead of _api_base()."""
    ok_resp = _ok_response(json_data={"ok": True})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return ok_resp

    with mock.patch("tools.altercraft_tool.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.request = fake_request
        ok, body = _req("GET", "/status", api_url="http://custom:3002")

    assert ok is True
    assert body == {"ok": True}
    assert len(calls) == 1
    assert calls[0] == ("GET", "http://custom:3002/status")


def test_handler_routes_to_per_call_url():
    """_h_status with api_url in args should route request to that URL."""
    calls = []

    def fake_req(method, path, *, api_url=None, **kwargs):
        calls.append((method, path, api_url))
        return True, {"health": 100}

    with mock.patch("tools.altercraft_tool._req", fake_req):
        result = _h_status({"api_url": "http://special-bot:3003"})

    assert len(calls) == 1
    assert calls[0] == ("GET", "/status", "http://special-bot:3003")
    assert "health" in result
