"""Async Body Protocol for DaemonCraft gateway adapter.

Ported from DaemonCraft agents/body/ (feat/body-abstraction branch).
Replaces synchronous urllib with async aiohttp to match the gateway runtime.

Supported body kinds:
  - hermescraft : Mineflayer bot API (default, port 3001)
  - mindcraft   : Kolbytn/Mindcraft sidecar (port 8090)

Configuration (PlatformConfig.extra):
    {
        "bot_api_url": "http://localhost:3001",
        "bot_username": "Steve",
        "body_kind": "hermescraft",   # or "mindcraft"
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from aiohttp import ClientSession, WSMsgType

logger = logging.getLogger(__name__)


def http_to_ws(url: str) -> str:
    return url.replace("http://", "ws://").replace("https://", "wss://")


class Body:
    """Abstract async body interface for the gateway adapter."""

    api_url: str
    username: str
    kind: str = "abstract"

    # Capability flags
    supports_screenshot: bool = False
    supports_impersonation: bool = False
    supports_set_goal: bool = False
    supports_act: bool = False

    def __init__(self, api_url: str, username: str):
        self.api_url = api_url.rstrip("/")
        self.username = username

    # -- HTTP helpers (async) --

    async def _post(
        self, session: ClientSession, path: str, body: dict, timeout: float = 5.0
    ) -> Optional[dict]:
        url = f"{self.api_url}{path}"
        try:
            async with session.post(
                url, json=body, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.debug("[body] POST %s -> %s: %s", path, resp.status, text)
                    return {"ok": False, "error": f"HTTP {resp.status}: {text}"}
                return await resp.json()
        except Exception as exc:
            logger.debug("[body] POST %s exception: %s", path, exc)
            return {"ok": False, "error": str(exc)}

    async def _get(
        self, session: ClientSession, path: str, timeout: float = 5.0
    ) -> Optional[dict]:
        url = f"{self.api_url}{path}"
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.debug("[body] GET %s -> %s: %s", path, resp.status, text)
                    return {"ok": False, "error": f"HTTP {resp.status}: {text}"}
                return await resp.json()
        except Exception as exc:
            logger.debug("[body] GET %s exception: %s", path, exc)
            return {"ok": False, "error": str(exc)}

    # -- Contract methods --

    async def chat(
        self, session: ClientSession, text: str, *, as_: Optional[str] = None
    ) -> dict:
        raise NotImplementedError

    async def perceive(self, session: ClientSession) -> dict:
        raise NotImplementedError

    async def get_plan(self, session: ClientSession) -> dict:
        raise NotImplementedError

    async def log_turn(self, session: ClientSession, turn: dict) -> None:
        raise NotImplementedError

    async def heartbeat(
        self,
        session: ClientSession,
        *,
        next_turn_in: Optional[float] = None,
        turn_in_progress: bool = False,
    ) -> None:
        raise NotImplementedError

    async def set_goal(self, session: ClientSession, text: str, priority: int = 5) -> dict:
        return {"ok": False, "error": "not supported"}

    async def act(self, session: ClientSession, command: str) -> dict:
        return {"ok": False, "error": "not supported"}

    async def interrupt(self, session: ClientSession, reason: str) -> dict:
        return {"ok": False, "error": "not supported"}

    # -- WebSocket --

    async def ws_connect(
        self,
        session: ClientSession,
        on_message: Callable[[dict], Any],
        on_open: Optional[Callable[[], Any]] = None,
        on_close: Optional[Callable[[Optional[int], Optional[str]], Any]] = None,
    ) -> Any:
        """Connect to the body's WebSocket and return the ws client object.

        The caller is responsible for reading messages in a loop.
        """
        raise NotImplementedError


class HermescraftBody(Body):
    """Async adapter for the Hermescraft Mineflayer bot (port 3001)."""

    supports_screenshot: bool = True
    supports_impersonation: bool = True
    kind: str = "hermescraft"

    async def chat(
        self, session: ClientSession, text: str, *, as_: Optional[str] = None
    ) -> dict:
        body: Dict[str, Any] = {"text": text}
        if as_:
            body["as"] = as_
        return await self._post(session, "/chat/send", body) or {"ok": False}

    async def perceive(self, session: ClientSession) -> dict:
        return await self._get(session, "/perceive") or {}

    async def get_plan(self, session: ClientSession) -> dict:
        return await self._get(session, "/plan") or {}

    async def log_turn(self, session: ClientSession, turn: dict) -> None:
        await self._post(session, "/agent/log", turn, timeout=5.0)

    async def heartbeat(
        self,
        session: ClientSession,
        *,
        next_turn_in: Optional[float] = None,
        turn_in_progress: bool = False,
    ) -> None:
        await self._post(
            session,
            "/agent/heartbeat",
            {"nextTurnIn": next_turn_in, "turnInProgress": turn_in_progress},
            timeout=2.0,
        )

    async def interrupt(self, session: ClientSession, reason: str) -> dict:
        return await self._post(session, "/agent/interrupt", {"reason": reason}) or {"ok": False}

    async def ws_connect(
        self,
        session: ClientSession,
        on_message: Callable[[dict], Any],
        on_open: Optional[Callable[[], Any]] = None,
        on_close: Optional[Callable[[Optional[int], Optional[str]], Any]] = None,
    ) -> Any:
        ws_url = http_to_ws(self.api_url) + "/ws"
        return await session.ws_connect(ws_url)


class MindcraftBody(Body):
    """Async adapter for the Mindcraft sidecar (port 8090 by default)."""

    supports_set_goal: bool = True
    supports_act: bool = True
    kind: str = "mindcraft"

    async def chat(
        self, session: ClientSession, text: str, *, as_: Optional[str] = None
    ) -> dict:
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "message required"}
        body = {"message": text}
        return await self._post(session, "/chat/send", body) or {"ok": False}

    async def perceive(self, session: ClientSession) -> dict:
        return await self._get(session, "/perceive") or {}

    async def get_plan(self, session: ClientSession) -> dict:
        result = await self._get(session, "/plan") or {}
        if not isinstance(result, dict):
            return {}
        goal = result.get("goal")
        if goal:
            return {
                "data": {"goal": goal, "tasks": []},
                "plan": result.get("plan"),
                "goal": goal,
            }
        return result

    async def log_turn(self, session: ClientSession, turn: dict) -> None:
        await self._post(session, "/agent/log", turn, timeout=5.0)

    async def heartbeat(
        self,
        session: ClientSession,
        *,
        next_turn_in: Optional[float] = None,
        turn_in_progress: bool = False,
    ) -> None:
        await self._post(
            session,
            "/agent/heartbeat",
            {"nextTurnIn": next_turn_in, "turnInProgress": turn_in_progress},
            timeout=2.0,
        )

    async def set_goal(self, session: ClientSession, text: str, priority: int = 5) -> dict:
        return (
            await self._post(session, "/set_goal", {"text": text, "priority": priority})
            or {"ok": False}
        )

    async def act(self, session: ClientSession, command: str) -> dict:
        return await self._post(session, "/act", {"command": command}) or {"ok": False}

    async def ws_connect(
        self,
        session: ClientSession,
        on_message: Callable[[dict], Any],
        on_open: Optional[Callable[[], Any]] = None,
        on_close: Optional[Callable[[Optional[int], Optional[str]], Any]] = None,
    ) -> Any:
        ws_url = http_to_ws(self.api_url) + "/ws"
        return await session.ws_connect(ws_url)


class UnsupportedBody(Body):
    """Fallback for unknown body kinds."""

    kind: str = "unsupported"

    async def chat(
        self, session: ClientSession, text: str, *, as_: Optional[str] = None
    ) -> dict:
        return {"ok": False, "error": f"unsupported body kind: {self.kind}"}

    async def perceive(self, session: ClientSession) -> dict:
        return {}

    async def get_plan(self, session: ClientSession) -> dict:
        return {}

    async def log_turn(self, session: ClientSession, turn: dict) -> None:
        pass

    async def heartbeat(
        self,
        session: ClientSession,
        *,
        next_turn_in: Optional[float] = None,
        turn_in_progress: bool = False,
    ) -> None:
        pass

    async def ws_connect(
        self,
        session: ClientSession,
        on_message: Callable[[dict], Any],
        on_open: Optional[Callable[[], Any]] = None,
        on_close: Optional[Callable[[Optional[int], Optional[str]], Any]] = None,
    ) -> Any:
        raise ValueError(f"unsupported body kind: {self.kind}")


def make_body(kind: Optional[str], api_url: str, username: str) -> Body:
    if kind == "mindcraft":
        return MindcraftBody(api_url, username)
    if kind in (None, "", "hermescraft"):
        return HermescraftBody(api_url, username)
    return UnsupportedBody(api_url, username)
