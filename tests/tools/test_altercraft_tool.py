"""Tests for tools/altercraft_tool.py — focused on _req retry behaviour (HRM-64)."""

import pytest
import httpx
from unittest import mock

from tools.altercraft_tool import _req


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
