"""
DaemonCraft platform adapter for Hermes Gateway.

Routes Minecraft chat (player whispers + world broadcasts) through the
Hermes AIAgent, while the agent_loop.py handles embodiment (movement,
quest engine, sensors).

The adapter consumes the Bot API WebSocket and HTTP endpoints:
  - WS /ws       : inbound chat events (array snapshot)
  - POST /chat/send   : outbound text
  - POST /tts/play    : outbound TTS relay to dashboards
  - GET  /agent/log   : recent loop turns for context injection
"""

import asyncio
import datetime as _dt
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Set

import aiohttp
from aiohttp import WSMsgType

from gateway.config import Platform, PlatformConfig
from gateway.platforms.daemoncraft_antiloop import StuckPivotTracker
from gateway.platforms.daemoncraft_narrategate import (
    NarrateGateTracker,
)

# ---------------------------------------------------------------------------
# CycleDetector — ported from daemoncraft agents/safety.py (stdlib-only)
# ---------------------------------------------------------------------------
import hashlib
import json as _json
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


def _cd_canonicalize(args) -> str:
    try:
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except Exception:
                return args
        return _json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return repr(args)


def _cd_signature(name: str, args) -> str:
    payload = f"{name}|{_cd_canonicalize(args)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass
class _CycleResult:
    triggered: bool
    sig: Optional[str]
    count: int
    window: int
    action: str


@dataclass
class CycleDetector:
    """Ring-buffer cycle detector for repeated tool-call patterns."""
    n: int = 4
    window: int = 6
    action: str = "warn"
    _buf: Deque[str] = field(default_factory=deque)
    _last_triggered_sig: Optional[str] = None

    def __post_init__(self) -> None:
        self._buf = deque(maxlen=max(self.window, self.n))

    def record(self, name: str, args) -> _CycleResult:
        sig = _cd_signature(name, args)
        self._buf.append(sig)
        return self._evaluate()

    def _evaluate(self) -> _CycleResult:
        if len(self._buf) < self.n:
            return _CycleResult(False, None, 0, len(self._buf), self.action)
        counts: Dict[str, int] = {}
        for s in self._buf:
            counts[s] = counts.get(s, 0) + 1
        top_sig, top_count = max(counts.items(), key=lambda kv: kv[1])
        if top_count >= self.n:
            if top_sig == self._last_triggered_sig:
                return _CycleResult(False, top_sig, top_count, len(self._buf), self.action)
            self._last_triggered_sig = top_sig
            return _CycleResult(True, top_sig, top_count, len(self._buf), self.action)
        if self._last_triggered_sig and self._last_triggered_sig != top_sig:
            self._last_triggered_sig = None
        return _CycleResult(False, top_sig, top_count, len(self._buf), self.action)

    def reset(self) -> None:
        self._buf.clear()
        self._last_triggered_sig = None
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key

logger = logging.getLogger(__name__)

META_NO_CLAMP = "_no_clamp"  # Set in metadata to bypass gateway-side char clamping (used by TTS transcripts)


class DaemonCraftAdapter(BasePlatformAdapter):
    """Gateway adapter for DaemonCraft (Minecraft bot API)."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DAEMONCRAFT)
        self._bot_api_url: str = (config.extra or {}).get("bot_api_url", "")
        self._bot_username: str = (config.extra or {}).get("bot_username", "")
        self._profile: str = (config.extra or {}).get("profile", "")
        self._allowed_users: Set[str] = set()
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._last_seen_timestamp: int = 0
        self._shutdown_event = asyncio.Event()
        self._world_names: Set[str] = set()  # Track broadcast worlds for send() routing
        self._ws_retry_count: int = 0
        self._voice_mode_default: str = "all"  # DaemonCraft defaults to TTS for all replies
        self._last_tts_time: float = 0.0
        self._tts_queue: list[dict] = []  # Dedup buffer for rapid-fire messages
        self._cycle_detector: Optional[CycleDetector] = None

        # Plan tracking for heartbeat-driven progress evaluation and GC
        self._plan_goal: Optional[str] = None
        self._plan_tasks_snapshot: list = []
        self._plan_created_at: float = 0.0
        self._plan_last_progress_at: float = 0.0
        self._plan_gc_timeout: int = (config.extra or {}).get("plan_gc_timeout_seconds", 300)
        self._turn_counter: int = 0  # Sequential turn counter for agent logs
        self._last_idle_wake_up: float = 0.0  # Throttle idle wake-ups
        self._task_signature_history: list = []  # Anti-loop watchdog: last N (action,status) tuples
        self._task_loop_threshold: int = (config.extra or {}).get("task_loop_threshold", 4)
        self._task_stale_seconds: int = (config.extra or {}).get("task_stale_seconds", 600)
        # StuckPivotTracker: detect same-objective / no-progress thrash using
        # judge verdicts + position. Threshold + bucket size are config-tunable.
        self._stuck_pivot_tracker = StuckPivotTracker(
            threshold=(config.extra or {}).get("stuck_pivot_threshold", 3),
            bucket_size=(config.extra or {}).get("spatial_bucket_blocks", 5),
            cooldown_seconds=(config.extra or {}).get("stuck_pivot_cooldown_seconds", 120.0),
        )
        # NarrateGateTracker: detect when the L4 narrates a past-tense action
        # that contradicts the most recent mc_* tool result. Records every tool
        # result so the gateway can check the LLM's next assistant text against
        # the verified tool outcome. Part of Opción B for t_0fa2c6dc.
        self._narrate_gate_tracker = NarrateGateTracker(
            reminder_cooldown_seconds=(config.extra or {}).get(
                "narrate_reminder_cooldown_seconds", 30.0,
            ),
        )
        self._session_epoch: int = int(time.time() * 1_000_000)  # microsecond resolution — practically zero collision risk between gateway restarts

        # Load allowlist by UUID (preferred) or username fallback.
        raw_allow = os.getenv("DAEMONCRAFT_ALLOWED_USERS", "").strip()
        if raw_allow:
            self._allowed_users = {u.strip().lower() for u in raw_allow.split(",") if u.strip()}

        # Force group sessions per world (broadcasts must share context)
        if config.extra is None:
            config.extra = {}
        config.extra.setdefault("group_sessions_per_user", False)

    def _group_chat_id(self, world: str = "world") -> str:
        """Return a chat_id scoped to this bot so each bot has its own session."""
        return f"{world}:{self._bot_username}"

    def _is_group_chat_id(self, chat_id: str) -> bool:
        """Check whether a chat_id is one of our group chat ids."""
        return chat_id in self._world_names or any(
            chat_id.startswith(w + ":") for w in self._world_names
        )

    def invoke_hook(self, hook_name: str, **kwargs):
        """Wrapper around the hermes_cli.plugins invoke_hook helper.

        The plugin system is hermes-cli-internal; this adapter sits
        above the LLM loop and shouldn't import from hermes_cli
        directly at module top (avoids a circular import when
        hermes-cli imports back from the gateway package during
        tests). We do a lazy import here.
        """
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            return _invoke_hook(hook_name, **kwargs)
        except Exception as _he:
            logging.debug(f"invoke_hook({hook_name}) failed: {_he}")
            return iter(())

    def _write_event_to_queue(self, event: dict) -> None:
        """Write an event to the daemoncraft-events.jsonl bridge file.
        
        The agent_loop reads this file each tick and includes events in the
        context stream, which the CLI can observe.
        """
        try:
            # Match agent_loop's path: ~/.hermes/sessions/<BOT_USERNAME>-events.jsonl
            bot_user = (os.getenv("MC_USERNAME") or self._bot_username or "CompAII")
            queue_path = Path.home() / ".hermes" / "sessions" / f"{bot_user}-events.jsonl"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            with open(queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.warning("[DaemonCraft] Failed to write event to queue: %s", e)

    async def _is_lab_mode(self) -> bool:
        """Check if the bot is in lab mode (explicit, not automagic).
        
        In lab mode, the gateway never spawns agent turns. All events
        (chat, heartbeats) only add context to the stream file.
        """
        try:
            async with self._session.get(
                f"{self._bot_api_url}/controller/mode", timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    mode_data = data.get("data") if data.get("ok") else {}
                    return mode_data.get("mode") == "lab"
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self._bot_api_url:
            logger.error("[DaemonCraft] bot_api_url missing in platform config extra")
            return False
        if not self._bot_username:
            logger.error("[DaemonCraft] bot_username missing in platform config extra")
            return False

        self._last_seen_timestamp = int(time.time() * 1000)
        self._shutdown_event.clear()
        self._session = aiohttp.ClientSession()
        # Cache the controller mode at connect() so the heartbeat
        # classifier can short-circuit without an HTTP roundtrip on
        # every heartbeat. Updated whenever the user toggles mode
        # via /controller/mode.
        # Set initial value to 0.0 so the FIRST heartbeat forces a
        # refresh (the 5s threshold check would otherwise trust the
        # stale "autonomous" default for up to 5s after a bot server
        # that's slow to start).
        self._last_mode_check = 0.0
        self._controller_mode_cache = "autonomous"  # safe default
        try:
            async with self._session.get(
                f"{self._bot_api_url}/controller/mode",
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        self._controller_mode_cache = data.get("data", {}).get("mode", "autonomous")
                        logger.info("[DaemonCraft] controller_mode at connect: %s", self._controller_mode_cache)
                else:
                    logger.warning("[DaemonCraft] controller_mode fetch returned status %d at connect; "
                                   "defaulting to '%s' (will refresh on first heartbeat)",
                                   resp.status, self._controller_mode_cache)
        except Exception as _ce:
            logger.warning("[DaemonCraft] controller_mode fetch failed at connect: %s; defaulting to '%s' "
                           "(will refresh on first heartbeat)", _ce, self._controller_mode_cache)

        n = int(os.getenv("MC_CYCLE_N", "0"))
        window = int(os.getenv("MC_CYCLE_WINDOW", "20"))
        action = os.getenv("MC_CYCLE_ACTION", "warn")
        if n > 0:
            self._cycle_detector = CycleDetector(n=n, window=window, action=action)
            logger.info("[DaemonCraft] CycleDetector enabled: n=%d window=%d action=%s", n, window, action)
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._mark_connected()
        logger.info("[DaemonCraft] Connected to %s as %s", self._bot_api_url, self._bot_username)
        return True

    async def disconnect(self) -> None:
        self._shutdown_event.set()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._mark_disconnected()
        logger.info("[DaemonCraft] Disconnected")

    async def handle_message(self, event: MessageEvent) -> None:
        """Handle a chat message, injecting heartbeat context if relevant.

        Sets the bot_api_url context variable so that any tools (today:
        embodied_plan; previously: minecraft/altercraft) dispatched for
        this message target the correct bot server.

        In lab mode, strip the re-entry "[System note: ...]" prefix that the
        gateway prepends when restoring a session after a crash. The note would
        otherwise wake the L4 with a turn containing only the previous state
        summary, which is exactly the "no-op narration" pattern we want to
        avoid in lab. Autonomous mode keeps the note (it's useful for
        continuity when the L4 is meant to keep acting).
        """
        from tools.bot_api_url_ctx import set_bot_api_url, reset_bot_api_url
        if event.text and await self._is_lab_mode():
            stripped = self._strip_reentry_note(event.text)
            if stripped != event.text:
                logger.info(
                    "[DaemonCraft] Stripped re-entry note in lab mode "
                    "(%d → %d chars)",
                    len(event.text), len(stripped),
                )
                event.text = stripped
        token = set_bot_api_url(self._bot_api_url)
        try:
            await super().handle_message(event)
        finally:
            reset_bot_api_url(token)

    @staticmethod
    def _strip_reentry_note(text: str) -> str:
        """Remove the leading `[System note: ...]\n\n` block that gateway/run.py
        prepends on session restore. Idempotent: if the text does not start
        with that block, returns it unchanged.
        """
        import re
        if not text.startswith("[System note:"):
            return text
        return re.sub(
            r"^\[System note:.*?\]\s*\n\n",
            "",
            text,
            count=1,
            flags=re.DOTALL,
        )

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        ws_url = self._bot_api_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        while not self._shutdown_event.is_set():
            try:
                async with self._session.ws_connect(ws_url) as ws:
                    self._ws_retry_count = 0
                    logger.info("[DaemonCraft] WebSocket connected")
                    while not self._shutdown_event.is_set():
                        msg = await ws.receive(timeout=30)
                        if msg.type == WSMsgType.TEXT:
                            await self._on_ws_message(msg.data)
                        elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._ws_retry_count += 1
                delay = min(2 ** self._ws_retry_count, 30)
                jitter = random.random()  # 0–1s uniform jitter
                sleep_time = delay + jitter
                logger.warning("[DaemonCraft] WebSocket error: %s — reconnecting in %.1fs", e, sleep_time)
                await asyncio.sleep(sleep_time)

    async def _on_ws_message(self, data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return

        msg_type = payload.get("type")
        if msg_type == "chat":
            messages = payload.get("data", [])
            if not isinstance(messages, list):
                return
            await self._handle_chat_batch(messages)
        elif msg_type == "quest_event":
            data = payload.get("data", {})
            await self._handle_quest_event(data)
        elif msg_type == "blueprint_updated":
            data = payload.get("data", {})
            await self._handle_blueprint_updated(data)
        elif msg_type == "heartbeat_context":
            data = payload.get("data", {})
            await self._handle_heartbeat_context(data)
        elif msg_type == "action_result":
            await self._handle_action_result(payload)
        elif msg_type == "interrupt":
            # Loop-to-gateway interrupt acknowledgment — no action needed
            pass
        elif msg_type == "status":
            pass
        else:
            logger.debug("[DaemonCraft] Unknown WS message type: %s", msg_type)

    async def _handle_chat_batch(self, messages: list) -> None:
        """Process a batch of chat messages with bot filtering and @mention classification.

        - Bot messages without @mention are silently dropped.
        - Human @mentions are treated as urgent (interrupts loop + immediate response).
        - All other human messages are queued normally.
        """
        new_messages = [m for m in messages if m.get("time", 0) > self._last_seen_timestamp]
        if not new_messages:
            return

        for entry in new_messages:
            self._last_seen_timestamp = max(self._last_seen_timestamp, entry.get("time", 0))

        # Dynamically discover all known bots from cast configs.
        # This is a live hook — no need to update .env files when bots change.
        def _discover_known_bots() -> set[str]:
            import yaml
            from pathlib import Path as _Path
            bots = set()
            casts_dir = _Path.home() / "Projects" / "DaemonCraft" / "agents" / "casts"
            try:
                for cf in sorted(casts_dir.glob("*.yaml")):
                    cfg = yaml.safe_load(cf.read_text()) or {}
                    for a in cfg.get("agents", []):
                        name = a.get("name", "")
                        if name:
                            bots.add(name.strip().lower())
            except Exception:
                pass
            # Also check env override
            override = os.getenv("MC_KNOWN_BOTS", "")
            if override:
                for u in override.split(","):
                    u = u.strip().lower()
                    if u:
                        bots.add(u)
            return bots

        known_bots = _discover_known_bots()

        urgent_msgs = []
        accepted_msgs = []
        import re

        # Build two regexes:
        # 1. @username!  — URGENT interrupt (exclamation forces immediate response)
        # 2. @username   — normal steer (queued, doesn't interrupt)
        urgent_re = re.compile(rf"\b@{re.escape(self._bot_username.lower())}!", re.IGNORECASE)
        mention_re = re.compile(rf"\b@{re.escape(self._bot_username.lower())}\b", re.IGNORECASE)

        for entry in new_messages:
            from_user = entry.get("from", "").lower()
            msg_text = entry.get("message", "")
            is_bot = from_user in known_bots
            mentions_bot = bool(mention_re.search(msg_text))
            is_urgent = bool(urgent_re.search(msg_text))

            if is_bot and not mentions_bot:
                continue  # Silently drop bot spam

            accepted_msgs.append(entry)

            # Only @username! (with exclamation) is urgent interrupt.
            # @username without ! is steer — queued, doesn't abort current turn.
            if is_urgent and not is_bot:
                urgent_msgs.append(entry)

        # Interrupt the loop for urgent human @mentions before generating response
        if urgent_msgs:
            senders = ", ".join({m.get("from", "Player") for m in urgent_msgs})
            logger.info("[DaemonCraft] Urgent @mention from %s — interrupting loop", senders)
            await self._interrupt_agent("urgent_mention")
        elif accepted_msgs:
            senders = ", ".join({m.get("from", "Player") for m in accepted_msgs})
            logger.info("[DaemonCraft] Chat from %s queued", senders)

        # Process all accepted messages through the gateway
        for entry in accepted_msgs:
            await self._handle_chat_entry(entry)

    async def _interrupt_agent(self, reason: str) -> None:
        """POST /agent/interrupt to abort the loop's in-progress LLM turn."""
        try:
            async with self._session.post(
                f"{self._bot_api_url}/agent/interrupt",
                json={"reason": reason},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("[DaemonCraft] /agent/interrupt failed: %s %s", resp.status, body)
                else:
                    logger.debug("[DaemonCraft] /agent/interrupt sent (%s)", reason)
        except Exception as e:
            logger.warning("[DaemonCraft] /agent/interrupt exception: %s", e)

    async def _force_pivot_interrupt(self, pivot_reason: str) -> None:
        """Abort the L4's stuck turn and queue a pivot directive as a system
        message. The system message is constructed so that it survives the
        re-entry note filter (it's not a [System note: ...] prefix) and the
        /-command filter (it doesn't start with /). The L4 receives the
        directive at the start of its next turn and must radically change
        category of action.
        """
        # 1. Abort the in-progress LLM turn via the bot server interrupt endpoint.
        await self._interrupt_agent(pivot_reason)

        # 2. Reset the tracker so the next turn starts fresh.
        self._stuck_pivot_tracker.reset_turn()

        # 3. Inject the pivot directive as a system message into the L4.
        # Mirrors the pattern used by plan_cancelled at line ~595.
        source = self.build_source(
            chat_id=self._group_chat_id(),
            chat_name="world",
            chat_type="group",
            user_id="system",
            user_name="System",
            thread_id="world",
        )
        source.profile = self._profile
        event = MessageEvent(
            text=f"[Pivot directive] {pivot_reason}",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={"pivot_reason": pivot_reason, "type": "stuck_pivot"},
            internal=True,
        )
        await self.handle_message(event)

    async def _handle_quest_event(self, data: dict) -> None:
        """Process a quest_event from the QuestEngine.

        Builds a narrative message and injects it into the gateway so the
        AIAgent can respond to the player (narrate phase changes, etc.).
        """
        message = data.get("message", "A quest event occurred.")
        event_type = data.get("event_type", "quest_event")
        from_phase = data.get("from_phase")
        to_phase = data.get("to_phase")

        # Build a natural-language description for the gateway AIAgent
        lines = [f"[Quest Event] {message}"]
        if from_phase and to_phase:
            lines.append(f"Phase transition: {from_phase} → {to_phase}")
        elif event_type:
            lines.append(f"Event type: {event_type}")
        event_text = "\n".join(lines)

        logger.info("[DaemonCraft] Quest event: %s", event_text.replace("\n", " | "))

        # Route to the world broadcast session (group chat)
        source = self.build_source(
            chat_id=self._group_chat_id(),
            chat_name="world",
            chat_type="group",
            user_id="quest_engine",
            user_name="QuestEngine",
            thread_id="world",
        )
        source.profile = self._profile

        event = MessageEvent(
            text=event_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
        )
        await self.handle_message(event)

    async def _handle_blueprint_updated(self, data: dict) -> None:
        """Process a blueprint_updated event from the dashboard.

        Notifies the gateway AIAgent that a blueprint was modified so it
        can reload or acknowledge the change.
        """
        name = data.get("name", "unknown")
        saved_at = data.get("saved_at", 0)

        event_text = (
            f"[Blueprint Updated] The blueprint '{name}' was edited via the dashboard "
            f"at {time.strftime('%H:%M:%S', time.localtime(saved_at / 1000))}. "
            f"Use mc_story(action='load_blueprint', name='{name}') to reload the latest version."
        )

        logger.info("[DaemonCraft] Blueprint updated: %s", name)

        source = self.build_source(
            chat_id=self._group_chat_id(),
            chat_name="world",
            chat_type="group",
            user_id="dashboard",
            user_name="Dashboard",
            thread_id="world",
        )
        source.profile = self._profile

        event = MessageEvent(
            text=event_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
        )
        await self.handle_message(event)

    async def _handle_action_result(self, payload: dict) -> None:
        """Forward action_result events to transform_tool_result hooks.

        Also record the result in the NarrateGateTracker so the next
        assistant message can be checked for past-tense narration that
        contradicts the verified tool outcome. Tied to t_a2c3facb:
        consumes the typed outcome/category fields directly (not strings).

        t_f8481d90: ALSO augment the narration verification with a
        synthetic world state injection that includes a visual pre-process
        of the area affected by the action. This is the "soft discard"
        — we don't strip the assistant text from history, but we give
        the LLM a strong anchor (the visual) for re-narrating correctly
        on the next turn.
        """
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        action_name = str(data.get("action") or "")
        # TYPED fields from the server (see agents/bot/lib/typed_result.js).
        # No string matching on result blobs.
        outcome = str(data.get("outcome") or "unknown")
        category = str(data.get("category") or "other")
        target = data.get("target")
        position_before = data.get("position_before")
        position_after = data.get("position_after")
        # Forward the original payload to the transform_tool_result hook
        # for any consumer that wants the full record (judge, ok, ts, etc.)
        import json as _json
        result_str = _json.dumps(data)
        # Tied to t_f8481d90: invoke_hook returns a sync iterator, not
        # awaitable. Just iterate it (transform_tool_result is a
        # fire-and-forget observer hook).
        for _ in self.invoke_hook("transform_tool_result", tool_name="mc_action_result", result=result_str):
            pass

        # NarrateGateTracker: capture this tool result with the typed fields.
        # No substring matching, no heuristic classification. The server
        # emits the outcome and category at the top level.
        pos_before = None
        pos_after = None
        try:
            pos_before = (
                (position_before.get("x"), position_before.get("y"), position_before.get("z"))
                if isinstance(position_before, dict) else None
            )
            pos_after = (
                (position_after.get("x"), position_after.get("y"), position_after.get("z"))
                if isinstance(position_after, dict) else None
            )
            self._narrate_gate_tracker.record_tool_result(
                tool_name=action_name or "mc_action",
                outcome=outcome,
                action_category=category,
                position_before=pos_before,
                position_after=pos_after,
                now=time.time(),
            )
        except Exception as _ng_err:
            logging.debug(f"NarrateGateTracker record failed: {_ng_err}")

        # t_f8481d90: build the visual-augmented narrate reminder.
        # If the last assistant message had past-tense narration that
        # contradicts this tool result, inject a synthetic world state
        # with the visual pre-process of the affected area. Cap at 2
        # injections per turn to avoid infinite loops.
        try:
            last_assistant_text = await self._get_last_assistant_text()
            if last_assistant_text:
                mismatch = self._narrate_gate_tracker.detect_narrate_mismatch(
                    last_assistant_text, now=time.time()
                )
                if mismatch and self._narrate_gate_tracker.should_inject_reminder(mismatch, now=time.time()):
                    await self._inject_narrate_mismatch_visual(
                        mismatch=mismatch,
                        action_name=action_name,
                        category=category,
                        target=target,
                        position_after=position_after,
                        last_assistant_text=last_assistant_text,
                    )
        except Exception as _v_err:
            logging.debug(f"Narrate visual inject failed: {_v_err}")

    async def _get_last_assistant_text(self) -> str:
        """Return the most recent assistant text from the world session.

        Used by t_f8481d90 (discard narrate) to compare narration
        against the verified tool result.
        """
        if not self._session_store:
            return ""
        session_id = self._get_world_session_id()
        if not session_id:
            return ""
        try:
            transcript = self._session_store.load_transcript(session_id)
            for msg in reversed(transcript):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                        return "\n".join(text_parts).strip()
                    return str(content or "").strip()
        except Exception:
            pass
        return ""

    async def _inject_narrate_mismatch_visual(
        self,
        mismatch,
        action_name: str,
        category: str,
        target,
        position_after,
        last_assistant_text: str,
    ) -> None:
        """Inject a synthetic world state that includes a visual pre-process
        of the area affected by the action, plus an explicit reminder.

        Tied to t_f8481d90 (discard narrate + visual inject). This is
        the "soft discard": we don't strip the assistant text from
        history, but we give the LLM a strong anchor (the visual) for
        re-narrating correctly on the next turn. The reminder text
        names the specific narration that contradicted the tool result
        and tells the LLM to anchor to the visual.
        """
        # Fetch a visual of the affected area. For movement, it's the
        # bot's current position. For build, it's the target cell. For
        # mine, the target cell.
        visual_block = ""
        try:
            if (position_after and isinstance(position_after, dict)
                    and self._session is not None):
                bx, by, bz = (
                    int(position_after.get("x", 0)),
                    int(position_after.get("y", 0)),
                    int(position_after.get("z", 0)),
                )
                radius = 4
                url = (
                    f"{self._bot_api_url}/blocks"
                    f"?x1={bx-radius}&y1={by-radius}&z1={bz-radius}"
                    f"&x2={bx+radius}&y2={by+radius}&z2={bz+radius}"
                    f"&format=visual"
                )
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        visual_block = (body.get("data") or {}).get("text", "") or ""
                        if len(visual_block) > 2500:
                            visual_block = visual_block[:2500] + "\n...[truncated]"
        except Exception as _v_err:
            logging.debug(f"Failed to fetch visual for narrate mismatch: {_v_err}")
            visual_block = "(visual fetch failed)"

        reminder_text = (
            f"[Verify-Before-Narrate] Your last narration said "
            f"'{mismatch.snippet}' but the most recent tool result "
            f"({action_name}) had outcome '{mismatch.actual_outcome}'. "
            f"The narration contradicts the verified tool result. "
            f"Below is the visual pre-process of the affected area "
            f"(radius 4 around the bot). Anchor your next narration "
            f"to this visual, not to your earlier intention. The visual "
            f"is the ground truth."
        )
        data = {
            "kind": "narrate_mismatch_visual",
            "reminder": reminder_text,
            "tool_name": action_name,
            "category": category,
            "outcome": mismatch.actual_outcome,
            "target": target,
            "position_after": position_after,
            "visual": visual_block,
            "snippet": mismatch.snippet,
            "mismatch_kind": mismatch.kind,
            "ts": time.time(),
        }
        await self._inject_synthetic_world_state(data)

    async def _handle_heartbeat_context(self, data: dict) -> None:
        """Process heartbeat_context with two-level event architecture.

        - Context-only updates: inject synthetic world-state into the session_store
          silently. This is an adapter-internal observation artifact, not a normal
          agent tool call, so it is exempt from any external authority/lease gate.
        - Wake-up events: inject synthetic world-state + force an agent turn with
          tool_choice="required". The agent MUST react with a tool call (or mc_no_op).
        - Active plans: every heartbeat while a plan is active forces a wake_up so
          the agent evaluates progress against the plan.
        - L4 verdict (GAP #5): body_session.l4_verdict is formatted into prompt as
          compact "[L4 last] ..." feedback line (observations+delta only, no prescription).

        In lab mode, drop heartbeats entirely. We don't update plan tracking, we
        don't poll the watchdog, and we don't create the L4 session in the
        session_store. The session is only created when a real user turn arrives
        (chat message, dashboard event, or explicit /command from the operator).
        This keeps lab mode truly dormant between operator actions.
        """
        if await self._is_lab_mode():
            return

        # StuckPivotTracker: detect same-objective / no-progress thrash and
        # interrupt the active L4 turn with a pivot message. Only fires in
        # autonomous mode (lab already returned above). Runs on every
        # heartbeat — cheap O(1) state update.
        body_session = data.get("body_session") or {}
        status = data.get("status") or {}
        pending_judges = body_session.get("pending_judges") or []
        pivot_reason = self._stuck_pivot_tracker.record_heartbeat(
            body_session=body_session,
            status=status,
            pending_judges=pending_judges,
            now=time.time(),
        )
        if pivot_reason:
            logger.warning("[DaemonCraft] Stuck pivot triggered: %s", pivot_reason)
            await self._force_pivot_interrupt(pivot_reason)

        plan = data.get("plan") or {}
        await self._update_plan_tracking(plan)

        # Run plan garbage collection before classification.
        # Tied to t_97b030a6 followup: in lab mode, plan GC is
        # silenced too. The plan is just a record; lab mode means
        # Nico is observing and not driving the bot, so a stale
        # plan should not fire events. Without this guard, the
        # auto-resumed session's stale plan GC'd and re-fired the
        # L4 in a loop (52 API calls, 14 min, 0 user input).
        if await self._is_lab_mode():
            # In lab mode, also clear any active plan so the L4
            # has no reference to the dead auto-resume plan.
            if self._plan_goal is not None:
                logger.info("[DaemonCraft] Lab mode: clearing stale plan '%s' on heartbeat", self._plan_goal[:40])
                self._plan_goal = None
                self._plan_tasks_snapshot = []
                self._plan_created_at = 0.0
                self._plan_last_progress_at = 0.0
            # Drop the heartbeat entirely.
            return
        gc_reason = await self._maybe_gc_plan()
        if gc_reason:
            logger.info("[DaemonCraft] Plan GC: %s", gc_reason)
            # Inject cancellation as a system event
            await self._inject_synthetic_world_state({
                "type": "plan_cancelled",
                "reason": gc_reason,
                "timestamp": int(time.time() * 1000),
            })
            # Force wake_up with the cancellation message
            source = self.build_source(
                chat_id=self._group_chat_id(),
                chat_name="world",
                chat_type="group",
                user_id="system",
                user_name="System",
                thread_id="world",
            )
            source.profile = self._profile
            event = MessageEvent(
                text=f"[System: {gc_reason} — set a new plan or continue with immediate actions.]",
                message_type=MessageType.TEXT,
                source=source,
                raw_message={"gc_reason": gc_reason},
                internal=True,
                )
            await self.handle_message(event)
            return

        event_type = await self._classify_heartbeat_event(data)
        logger.info("[DaemonCraft] Heartbeat classified as: %s", event_type)

        # World-state injection REMOVED — was flooding gAndy with scans every heartbeat,
        # interrupting McCompaii's own embodied_plan calls. L4 scans when HE decides.

        if event_type == "context":
            logger.debug("[DaemonCraft] Context-only heartbeat injected silently")
            return

        # Skip wake_up if there's an active user session — single controller
        if await self._is_lab_mode():
            logger.info("[DaemonCraft] Skipping wake_up: active user session detected")
            return

        # Cycle guard — skip wake-up if loop is repeating embodied_plan calls
        if await self._check_cycle("embodied_plan", {}):
            return

        # Wake-up event: force an agent turn with tool_choice=required
        plan_goal = self._plan_goal
        body = data.get("body_session") or {}

        # Build enriched prompt from body_session
        prompt_parts = []

        # Header: trigger + classification
        reason = body.get("heartbeat_reason", "unknown")
        if reason and reason != "idle":
            prompt_parts.append(f"[System: Body heartbeat — WAKE UP. Trigger: {reason}.")
        else:
            prompt_parts.append("[System: Body heartbeat — IDLE wake up.")

        # Body status — position only. Survival is L2's job, never L4's concern.
        pos = body.get("position", {})
        pos_str = f"({pos.get('x', '?')}, {pos.get('y', '?')}, {pos.get('z', '?')})" if pos else "unknown"
        prompt_parts.append(f" Body: {pos_str}. Your body handles survival automatically — you explore, document, build.\n")
        prompt_parts.append(" Nico is a spectator watching your stream. You are alone in this world. Never wait for him, never go to him, never change plans for him. If he wants something he'll say it in chat. Until then: explore, build paths, document places. You are the protagonist. Act.\n")
        # Body activity — narrative continuity, NOT a problem to solve. L2 fought? Fine. L2 ate? Fine.
        # This is for your story when you chat with humans. Never plan around it. Your body already handled it.
        body_act = (body.get("body_activity") or "").strip()
        if body_act:
            prompt_parts.append(f" [Info] Your body handled: {body_act}. Everything is fine — you focus on exploring.\n")

        # GAP #5: L4 last-action verdict (from judge) — injected into NEXT heartbeat only.
        # Reports WHAT happened (outcome + delta), never prescribes next action (LLM must reason).
        # Combines with L2 runner activity during the open-loop window for context.
        l4v = body.get("l4_verdict")
        if l4v and isinstance(l4v, dict):
            l2_sum = (body.get("runner_activity") or {}).get("summary") or ""
            l2_part = f" | L2: {l2_sum}" if l2_sum else ""
            ago = l4v.get("seconds_ago", 0)
            delta = l4v.get("delta", "0.0m")
            rc = l4v.get("reason_code") or ""
            outcome = l4v.get("outcome") or "?"
            act = l4v.get("action") or "?"
            # Compact one-line report; example: [L4 last] dig@540,115,-307: preempted RUNNER_ACTIVE delta=0.0m 14s ago | L2: 2 attacks, 1 flee
            verdict_line = f"[L4 last] {act}: {outcome} {rc} delta={delta} {ago}s ago{l2_part}".strip()
            prompt_parts.append(f" {verdict_line}.")



        # Action history (oldest -> newest)
        actions = body.get("action_history") or []
        if actions:
            a_strs = [f"{a.get('action', '?')}({a.get('status', '?')}, {a.get('secondsAgo', '?')}s ago)" for a in actions]
            prompt_parts.append(f" Recent actions: {' -> '.join(a_strs)}.")

        # Active task
        task_mode = body.get("mode", "idle")
        last_action = body.get("last_action")
        if last_action and task_mode not in ("idle", None):
            prompt_parts.append(f" Active task: {last_action} ({task_mode}).")
        else:
            prompt_parts.append(" Active task: none.")

        # Plan context
        if plan_goal:
            prompt_parts.append(f" Plan: '{plan_goal}' — {len(self._plan_tasks_snapshot)} tasks.")
            prompt_parts.append(" Evaluate progress based on the provided wake up event data. Continue, adjust, or wait.]")
        else:
            prompt_parts.append(" No active plan. START following your autonomous curriculum immediately. Take ONE concrete action now: gather, craft, build, or explore. Do not wait.]")

        prompt_text = "".join(prompt_parts)

        # Store for dashboard Bot Mind panel
        self._last_prompt = prompt_text

        # Auto-consume pending judges before dispatching to L4
        pending_judges = body.get("pending_judges") or []
        if pending_judges and self._session:
            try:
                l4_ticks = [j["captured_at_tick"] for j in pending_judges if j.get("initiator") == "l4_agent"]
                if l4_ticks:
                    async with self._session.post(
                        f"{self._bot_api_url}/judge/consume",
                        json={"ticks": l4_ticks},
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as resp:
                        result = await resp.json()
                        logger.debug("[DaemonCraft] Consumed %d judge entries, %d remaining",
                                     result.get("consumed", 0), result.get("remaining", 0))
            except Exception:
                pass

        source = self.build_source(
            chat_id=self._group_chat_id(),
            chat_name="world",
            chat_type="group",
            user_id="system",
            user_name="System",
            thread_id="world",
        )
        source.profile = self._profile

        event = MessageEvent(
            text=prompt_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            internal=True,
        )
        await self.handle_message(event)

    async def _update_plan_tracking(self, plan: dict) -> None:
        """Update internal plan snapshot and detect progress."""
        goal = plan.get("goal")
        tasks = plan.get("tasks", [])

        if not goal:
            # No active plan
            self._plan_goal = None
            self._plan_tasks_snapshot = []
            self._plan_created_at = 0.0
            self._plan_last_progress_at = 0.0
            return

        # Detect if this is a new plan
        if goal != self._plan_goal:
            self._plan_goal = goal
            self._plan_tasks_snapshot = [dict(t) for t in tasks]
            self._plan_created_at = time.time()
            self._plan_last_progress_at = time.time()
            logger.info("[DaemonCraft] New plan tracked: %s (%d tasks)", goal, len(tasks))
            return

        # Detect progress: compare task statuses
        progress_made = False
        if len(tasks) == len(self._plan_tasks_snapshot):
            for old, new in zip(self._plan_tasks_snapshot, tasks):
                if old.get("status") != new.get("status"):
                    progress_made = True
                    break
        elif len(tasks) != len(self._plan_tasks_snapshot):
            progress_made = True

        if progress_made:
            self._plan_last_progress_at = time.time()
            self._plan_tasks_snapshot = [dict(t) for t in tasks]
            logger.debug("[DaemonCraft] Plan progress detected: %s", goal)

    async def _maybe_gc_plan(self) -> Optional[str]:
        """Garbage-collect stale plans. Returns cancellation reason or None."""
        if not self._plan_goal:
            return None

        now = time.time()
        age = now - self._plan_created_at
        since_progress = now - self._plan_last_progress_at

        # GC if plan is older than timeout AND no progress in timeout period
        if age > self._plan_gc_timeout and since_progress > self._plan_gc_timeout:
            reason = (
                f"Plan '{self._plan_goal}' cancelled after {int(age)}s "
                f"with no progress for {int(since_progress)}s"
            )
            # Clear plan on bot server
            try:
                async with self._session.post(
                    f"{self._bot_api_url}/plan/update",
                    json={"action": "clear_goal"},
                ) as resp:
                    if resp.status < 400:
                        logger.info("[DaemonCraft] Plan cleared on bot server")
            except Exception as e:
                logger.warning("[DaemonCraft] Failed to clear plan on bot server: %s", e)

            # Reset local tracking
            self._plan_goal = None
            self._plan_tasks_snapshot = []
            self._plan_created_at = 0.0
            self._plan_last_progress_at = 0.0
            return reason

        return None

    async def _classify_heartbeat_event(self, data: dict) -> str:
        """Classify heartbeat as 'context' or 'wake_up'.

        Wake-up triggers:
        - Bot is stuck on a movement task (task_stuck in status)
        - Active plan exists (agent must evaluate progress every heartbeat)
        - Health decreased from previous known value
        - Nearby hostile entities (zombie, skeleton, creeper, spider)
        - Explicit damage events in events list

        In lab mode, NO wake_up triggers fire. Lab mode means: the
        agent only acts on user input. Heartbeats add to context but
        do not spawn agent turns. This is so the human operator has
        full control during testing.
        """
        # Lab mode silences ALL wake_up triggers. Only user input
        # (chat message) drives the L4. Tied to t_97b030a6 followup.
        # The check below is async because we need a HTTP roundtrip
        # to /controller/mode (the mode is in the bot server's memory
        # and may have changed since connect()). Cached for 5s.
        # FAIL-SAFE: if the fetch fails, assume lab mode. This is
        # because the alternative (stale "autonomous" cache) caused
        # a 52-API-call loop on 2026-06-02. Better to miss a wake-up
        # in autonomous mode than to fire one in lab mode.
        now = time.time()
        if (now - getattr(self, "_last_mode_check", 0)) > 5.0:
            self._last_mode_check = now
            try:
                async with self._session.get(
                    f"{self._bot_api_url}/controller/mode",
                    timeout=aiohttp.ClientTimeout(total=1.0),
                ) as resp:
                    if resp.status == 200:
                        md = await resp.json()
                        if md.get("ok"):
                            actual = md.get("data", {}).get("mode", "lab")
                            self._controller_mode_cache = actual
                        else:
                            # ok=false means error, assume lab
                            self._controller_mode_cache = "lab"
                    else:
                        # HTTP error, assume lab
                        self._controller_mode_cache = "lab"
            except Exception:
                # Connection error, timeout, anything — assume lab
                # (safer than assuming autonomous)
                self._controller_mode_cache = "lab"
        if getattr(self, "_controller_mode_cache", None) == "lab":
            return "context"

        status = data.get("status") or {}
        nearby = data.get("nearby") or {}
        events = data.get("events") or []
        plan = data.get("plan") or {}

        # Stuck on movement task — force wake_up so agent can react
        task_stuck = status.get("task_stuck")
        if task_stuck:
            events.append(f"Stuck: {task_stuck}")
            return "wake_up"

        # Active plan — force wake_up so agent evaluates progress
        if plan.get("goal"):
            events.append(f"Plan progress check: {plan['goal']}")
            return "wake_up"

        # Damage / health drop
        current_health = status.get("health")
        if current_health is not None and hasattr(self, "_last_health"):
            if current_health < self._last_health:
                logger.info("[DaemonCraft] Wake-up reason: health dropped %s -> %s", self._last_health, current_health)
                self._last_health = current_health
                return "wake_up"
        if current_health is not None:
            self._last_health = current_health

        # Explicit damage events
        for ev in events:
            ev_str = str(ev).lower()
            if any(k in ev_str for k in ("damage", "hurt", "attack", "hit", "died", "killed")):
                logger.info("[DaemonCraft] Wake-up reason: damage event '%s'", ev_str[:80])
                return "wake_up"

        # Death detected — force immediate wake-up so agent can react
        body = data.get("body_session") or {}
        current_deaths = body.get("deaths", 0)
        if current_deaths > getattr(self, "_last_deaths", 0):
            self._last_deaths = current_deaths
            last = body.get("last_death") or {}
            pos = last.get("position", {})
            logger.info("[DaemonCraft] Wake-up reason: death #%d at (%.1f, %.1f, %.1f)",
                        current_deaths, pos.get("x", 0), pos.get("y", 0), pos.get("z", 0))
            return "wake_up"
        self._last_deaths = current_deaths

        # Nearby hostile mobs
        hostile = {"zombie", "skeleton", "creeper", "spider", "enderman", "witch", "husk", "drowned", "phantom"}
        for ent in nearby.get("entities", [])[:12]:
            name = str(ent.get("name", ent) if isinstance(ent, dict) else ent).lower()
            if any(h in name for h in hostile):
                logger.info("[DaemonCraft] Wake-up reason: hostile entity '%s'", name)
                return "wake_up"

        # Bot stuck — critical, needs immediate reaction
        task = status.get("task")
        if task and task.get("status") == "stuck":
            logger.info("[DaemonCraft] Wake-up reason: bot stuck (%s)", task.get("error", "unknown")[:60])
            return "wake_up"

        # Anti-loop watchdog: same task signature across N heartbeats = stuck in a loop.
        # The L4 may keep choosing the same action (e.g. deathpoint, mine, goto) without
        # changing the world. We force a wake-up with an explicit loop message so the
        # agent sees "you've been doing X for N heartbeats, pivot."
        if task:
            action = task.get("action", "")
            tstatus = task.get("status", "")
            elapsed = int(task.get("elapsed_s", 0) or 0)
            signature = (action, tstatus, elapsed // 30)  # bucket elapsed by 30s
            self._task_signature_history.append(signature)
            if len(self._task_signature_history) > self._task_loop_threshold * 2:
                self._task_signature_history = self._task_signature_history[-self._task_loop_threshold * 2:]
            # Stale task: status="done" but elapsed_s huge = L4 is not acting on results
            if tstatus == "done" and elapsed > self._task_stale_seconds:
                logger.info(
                    "[DaemonCraft] Wake-up reason: stale task '%s' done for %ds (>%ds threshold)",
                    action, elapsed, self._task_stale_seconds,
                )
                return "wake_up"
            # Repeated signature: same action+status (bucketed) for N consecutive heartbeats
            if len(self._task_signature_history) >= self._task_loop_threshold:
                recent = self._task_signature_history[-self._task_loop_threshold:]
                if len(set(recent)) == 1:
                    logger.warning(
                        "[DaemonCraft] Wake-up reason: TASK LOOP detected — %s/%s repeated for %d heartbeats",
                        action, tstatus, self._task_loop_threshold,
                    )
                    return "wake_up"

        # Idle heartbeat: wake up Steve so he can act autonomously
        # (progress on achievements, scout, etc.) Throttle to avoid token spam.
        now = time.time()
        if now - self._last_idle_wake_up >= 90:
            self._last_idle_wake_up = now
            logger.info("[DaemonCraft] Wake-up reason: idle heartbeat (90s throttle)")
            return "wake_up"

        return "context"

    async def _inject_synthetic_world_state(self, data: dict) -> None:
        """Inject a fake assistant tool_call + tool result into the world session.

        The transcript shape still uses `mc_perceive` so downstream consumers can
        reuse their existing parsing path, but the gateway treats this as an
        internal observation event, not an actionable tool execution.
        """
        if not self._session_store:
            logger.debug("[DaemonCraft] No session_store available, skipping synthetic injection")
            return

        session_id = self._get_world_session_id()
        if not session_id:
            logger.debug("[DaemonCraft] No world session found, skipping synthetic injection")
            return

        tool_call_id = f"hb_{uuid.uuid4().hex[:12]}"

        # Build a concise JSON payload for the tool result
        payload = json.dumps(data, ensure_ascii=False, default=str)
        # Truncate if too large to avoid flooding context window
        if len(payload) > 4000:
            payload = payload[:4000] + "\n...[truncated]"

        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "mc_perceive", "arguments": "{}"},
                }
            ],
        }
        tool_msg = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": payload,
        }

        self._session_store.append_to_transcript(session_id, assistant_msg)

        # Run transform_tool_result hooks so plugins (e.g. altercraft scene-graph)
        # can consume synthetic mc_perceive on the same path as real tool results.
        try:
            from hermes_cli.plugins import invoke_hook
            for hook_result in invoke_hook(
                "transform_tool_result",
                tool_name="mc_perceive",
                args={},
                result=payload,
                task_id="",
                session_id=session_id,
                tool_call_id=tool_call_id,
                duration_ms=0,
            ):
                if isinstance(hook_result, str):
                    payload = hook_result
                    tool_msg["content"] = payload
                    break
        except Exception as _hook_exc:
            logger.debug("[DaemonCraft] transform_tool_result hook error: %s", _hook_exc)

        self._session_store.append_to_transcript(session_id, tool_msg)
        logger.info("[DaemonCraft] Synthetic world state injected into session %s", session_id)

    async def _inject_embodied_world_state(self, data: dict) -> None:
        """Query the body (Gemma-Andy via embodied service) for world state.

        Instead of injecting raw bot data as synthetic mc_perceive, we ask the
        body to scan the world and inject its processed response. This keeps
        the architecture pure: Steve only knows the world through his body.
        """
        if not self._session_store:
            logger.debug("[DaemonCraft] No session_store, skipping embodied injection")
            return

        session_id = self._get_world_session_id()
        if not session_id:
            logger.debug("[DaemonCraft] No world session, skipping embodied injection")
            return

        embodied_url = os.environ.get("EMBODIED_SERVICE_URL", "http://localhost:7790")
        intent = (
            "Scan the area. Report concisely: your position, the 5 most common "
            "nearby blocks with counts, any entities (players, mobs) with distances, "
            "inventory highlights (tools, key materials), and any hazards. "
            "Keep the report under 600 characters."
        )

        tool_call_id = f"hb_{uuid.uuid4().hex[:12]}"
        payload = None
        ok = False
        exc_info = None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{embodied_url}/intent",
                    json={
                        "intent": intent,
                        "autonomy_level": 1,
                        "deadline_seconds": 15,
                        "allowed_tools": [
                            "scan_nearby",
                            "get_inventory",
                            "ask_clarification",
                            "raise_guardian_event",
                            "report_execution_error",
                        ],
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        ok = body.get("ok", False)
                        if ok and body.get("execution_results"):
                            payload = json.dumps(body, ensure_ascii=False, default=str)
                        elif body.get("plan", {}).get("body_plan"):
                            payload = json.dumps(body["plan"], ensure_ascii=False, default=str)
        except Exception as exc:
            logger.warning("[DaemonCraft] Embodied world-state query failed: %s", exc)
            exc_info = str(exc)

        if not payload:
            payload = json.dumps({
                "_note": "Body unresponsive — do not act as if it is responding. Wait for the next heartbeat.",
                "error": exc_info or "embodied service unavailable",
            })

        if len(payload) > 4000:
            payload = payload[:4000] + "\n...[truncated]"

        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "embodied_plan", "arguments": json.dumps({"intent": intent})},
                }
            ],
        }
        tool_msg = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": payload,
        }

        self._session_store.append_to_transcript(session_id, assistant_msg)
        self._session_store.append_to_transcript(session_id, tool_msg)
        logger.info(
            "[DaemonCraft] Embodied world-state injected (body ok=%s, %d chars) into session %s",
            ok, len(payload), session_id,
        )

    def _get_world_session_id(self) -> Optional[str]:
        """Resolve the session_id for the world broadcast session."""
        if not self._session_store:
            return None
        source = SessionSource(
            platform=Platform.DAEMONCRAFT,
            chat_id=self._group_chat_id(),
            chat_type="group",
            user_id=f"{self._bot_username}:{self._session_epoch}",  # epoch prevents stale session reuse
            thread_id="world",
        )
        session_key = build_session_key(
            source,
            group_sessions_per_user=False,
            thread_sessions_per_user=False,
        )
        entries = getattr(self._session_store, "_entries", {})
        entry = entries.get(session_key)
        if entry:
            return entry.session_id
        return None

    async def _handle_chat_entry(self, entry: dict) -> None:
        from_ = entry.get("from", "")
        if not from_:
            return
        if from_.lower() == self._bot_username.lower():
            return  # Ignore self-echo

        # Authorization by UUID (preferred) or username fallback
        sender_uuid = entry.get("uuid")
        if self._allowed_users:
            allowed = False
            if sender_uuid and sender_uuid.lower() in self._allowed_users:
                allowed = True
            if from_.lower() in self._allowed_users:
                allowed = True
            if not allowed:
                logger.debug("[DaemonCraft] Ignored message from unauthorized user: %s", from_)
                return

        text = entry.get("message", "")
        if not text:
            return

        # Filter: any message that starts with "/" is a Minecraft server command
        # (e.g. "/gamemode creative", "/tp", "/time set day"). These are operator
        # instructions, not agent prompts — they must not be injected into the L4
        # session as a chat turn, otherwise the L4 reads them as user intents and
        # acts. If we want to tell the L4 something about a command, we do it in
        # natural language, not as a slash-command.
        stripped = text.lstrip()
        if stripped.startswith("/"):
            logger.info(
                "[DaemonCraft] Filtered /-command from %s (not injected to L4): %r",
                from_, stripped[:80],
            )
            self._write_event_to_queue({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "src": "gateway",
                "event": "filtered_command",
                "player": from_,
                "text": stripped,
            })
            return

        is_whisper = entry.get("whisper", False)
        is_private = entry.get("private", False)
        world = entry.get("world", "world")

        # Session mapping
        if is_whisper or is_private:
            # 1:1 session
            chat_id = from_
            chat_type = "dm"
            thread_id = None
        else:
            # Group session per world, scoped to this bot
            chat_id = self._group_chat_id(world)
            chat_type = "group"
            thread_id = world
            self._world_names.add(world)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=sender_uuid or from_,
            user_name=from_,
            thread_id=thread_id,
        )
        source.profile = self._profile

        # Always inject chat into the world session so autonomous McCompaii sees it.
        # When lab mode is active (human controls via CLI), mark internal to suppress
        # auto-response — the CLI session handles the reply. Still write to event queue
        # for the CLI bridge so it can pick up the message text.
        lab = await self._is_lab_mode()
        if lab:
            logger.info("[DaemonCraft] Chat from %s injected to world session (lab mode — no auto-response)", from_)
            self._write_event_to_queue({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "src": "gateway",
                "event": "chat",
                "player": from_,
                "text": text,
            })

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=entry,
            internal=lab,  # suppress auto-response when human is driving
        )

        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Cycle detection
    # ------------------------------------------------------------------

    async def _check_cycle(self, tool_name: str, args: dict) -> bool:
        """Check tool-call cycle. Returns True if cycle detected and action is 'interrupt'."""
        if self._cycle_detector is None:
            return False
        result = self._cycle_detector.record(tool_name, args)
        if result.triggered:
            if result.action == "interrupt":
                logger.warning(
                    "[DaemonCraft] Cycle detected for '%s' (%d/%d) — interrupting agent",
                    tool_name, result.count, result.window,
                )
                await self._interrupt_agent("cycle_detected")
                return True
            else:
                logger.warning(
                    "[DaemonCraft] Cycle detected for '%s' (%d/%d) — action=%s",
                    tool_name, result.count, result.window, result.action,
                )
        return False

    # ------------------------------------------------------------------
    # Dashboard feed (DC-123)
    # ------------------------------------------------------------------

    async def on_processing_complete(self, event, outcome) -> None:
        """POST the last assistant turn to /agent/log so the dashboard Bot Mind panel populates.

        Before DC-112 the agent_loop posted turns directly. After DC-112 cognition
        moved to the gateway but no one wired the log relay. This hook restores
        visibility without touching the loop.
        """
        if not self._bot_api_url or not self._session:
            return
        try:
            session_id = self._get_world_session_id()
            if not session_id or not self._session_store:
                return
            transcript = self._session_store.load_transcript(session_id)
            # Find the last assistant message in the transcript
            last_assistant = None
            tool_calls = []
            for msg in reversed(transcript):
                role = msg.get("role", "")
                if role == "assistant" and last_assistant is None:
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Extract text and tool_use blocks
                        text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                        tool_calls = [
                            {"name": b.get("name"), "input": b.get("input")}
                            for b in content if b.get("type") == "tool_use"
                        ]
                        last_assistant = "\n".join(text_parts).strip()
                    else:
                        last_assistant = str(content)
                    break

            if last_assistant is None and not tool_calls:
                return

            await self._session.post(
                f"{self._bot_api_url}/agent/log",
                json={
                    "turn": len(transcript),
                    "time": int(time.time() * 1000),
                    "prompt": "",
                    "response": last_assistant or "",
                    "tool_calls": tool_calls,
                    "error": None,
                },
            )

            # Write LLM response to event bridge so agent_loop can display it
            self._write_event_to_queue({
                "type": "agent_response",
                "text": last_assistant or "",
                "tool_calls": [tc.get("name", "?") for tc in tool_calls],
                "ts": int(time.time() * 1000),
            })
        except Exception as e:
            logger.debug("[DaemonCraft] on_processing_complete /agent/log post failed: %s", e)

        # DC-132 — emit a turn metric (best-effort; never raises).
        # Latency: time since the last user/perceive message in the transcript,
        # if we can find one. tokens_in/out: not yet exposed by AIAgent at this
        # hook, so we emit zero placeholders rather than fabricate values.
        try:
            self._emit_metric(
                "turn",
                tokens_in=0,
                tokens_out=0,
                latency_ms=None,
                tool_call_count=len(tool_calls),
            )
            for tc in tool_calls:
                self._emit_metric("tool", tool=tc.get("name") or "?", ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # DC-132 — JSONL metrics (mirrors agents/agent_loop.py emitter in daemoncraft)
    # ------------------------------------------------------------------

    def _emit_metric(self, kind: str, **fields) -> None:
        """Append a JSON line to ~/.hermes/metrics/<cast>/<date>.jsonl.

        Schema is documented in scripts/agent-metrics-report.py in the
        daemoncraft repo. This is the gateway counterpart to the heartbeat
        emitter in agent_loop.py — together they cover the four families
        the report script aggregates.

        Cast comes from DAEMONCRAFT_METRICS_CAST env var; falls back to the
        bot username so events still group sensibly if the operator hasn't
        set it. No env var → emitter still fires under the username.
        """
        try:
            cast = os.getenv("DAEMONCRAFT_METRICS_CAST", "").strip() or self._bot_username or "daemoncraft"
            metrics_root = Path(os.getenv("DAEMONCRAFT_METRICS_DIR", str(Path.home() / ".hermes" / "metrics")))
            now = _dt.datetime.utcnow()
            cast_dir = metrics_root / cast
            cast_dir.mkdir(parents=True, exist_ok=True)
            path = cast_dir / f"{now.date().isoformat()}.jsonl"
            record = {
                "ts": now.isoformat(timespec="seconds") + "Z",
                "cast": cast,
                "agent": self._bot_username or "?",
                "kind": kind,
                **fields,
            }
            # Single os.write() with O_APPEND — POSIX-atomic for writes
            # under PIPE_BUF (typically 4 KB on Linux). Prevents truncated
            # lines under concurrent writers / mid-write process kill.
            line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Log agent turn to bot server for dashboard display (skip heartbeat/context-only turns)
        if content and content.strip() and content != "None":
            await self._post_agent_log(content, metadata)

        # _world_names is populated lazily from inbound broadcasts. If the gateway
        # initiates an outbound broadcast before any inbound from that world, this
        # will default to DM (whisper). For now the agent only replies to inbound.
        is_group = self._is_group_chat_id(chat_id)
        payload: dict[str, Any] = {"message": content}

        if is_group:
            payload["target"] = "broadcast"
        else:
            payload["target"] = chat_id

        try:
            async with self._session.post(
                f"{self._bot_api_url}/chat/send",
                json=payload,
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("[DaemonCraft] /chat/send failed: %s %s", resp.status, body)
                    return SendResult(success=False, error=f"HTTP {resp.status}: {body}")
        except Exception as e:
            logger.warning("[DaemonCraft] /chat/send exception: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

        # DC-123: relay TTS to dashboard after successful outbound message.
        system_tts_skip = {"steer", "gateway shutting down", "synthetic mc_perceive", "heartbeat", "mc_perceive", "queued", "⏳"}
        is_system_msg = any(skip in content.lower() for skip in system_tts_skip)
        if (content and content.strip() not in ("PASS", "")
                and not is_system_msg
                and not (metadata or {}).get("suppress_tts")):
            asyncio.create_task(self._generate_and_relay_tts(content, chat_id))

        return SendResult(success=True)

    async def _post_agent_log(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Post agent turn to bot server /agent/log for dashboard display."""
        try:
            self._turn_counter += 1
            tool_calls = []
            if metadata and "tool_calls" in metadata:
                tool_calls = metadata["tool_calls"]
            payload = {
                "turn": self._turn_counter,
                "time": int(time.time() * 1000),
                "prompt": getattr(self, "_last_prompt", ""),
                "response": content,
                "tool_calls": tool_calls,
                "error": None,
            }
            async with self._session.post(
                f"{self._bot_api_url}/agent/log",
                json=payload,
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.debug("[DaemonCraft] /agent/log failed: %s %s", resp.status, body)
        except Exception as e:
            logger.debug("[DaemonCraft] /agent/log exception: %s", e)

    async def _generate_and_relay_tts(self, text: str, chat_id: str) -> None:
        """Generate TTS for outbound text and relay audio to the dashboard.

        DC-123 fix: before DC-112 agent_loop called TTS explicitly. After DC-112
        the gateway owns cognition but the TTS relay was never wired. This method
        closes that gap — it is called as a fire-and-forget task from send().
        """
        try:
            from tools.tts_tool import text_to_speech_tool, check_tts_requirements
            if not check_tts_requirements():
                return
            import re as _re, json as _json
            # Strip Minecraft formatting codes and markdown before synthesis.
            clean = _re.sub(r'§[0-9a-fklmnor]', '', text)
            clean = _re.sub(r'[*_`#\[\]()]', '', clean).strip()
            if not clean:
                return
            # Edge-TTS stutter fix: prepend zero-width space to prevent first-word repetition.
            clean = "\u200b" + clean
            tts_result = await asyncio.to_thread(text_to_speech_tool, text=clean[:4000])
            tts_data = _json.loads(tts_result)
            audio_path = tts_data.get("file_path")
            if audio_path and os.path.exists(audio_path):
                await self._copy_and_relay_tts(audio_path, chat_id)
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        except Exception as e:
            logger.debug("[DaemonCraft] TTS generation failed: %s", e)

    async def _copy_and_relay_tts(self, audio_path: str, chat_id: str) -> SendResult:
        """Copy audio to shared TTS cache and POST /tts/play to dashboards."""
        try:
            import shutil

            tts_dir = "/tmp/daemoncraft-tts"
            os.makedirs(tts_dir, exist_ok=True)
            filename = os.path.basename(audio_path)
            dest = os.path.join(tts_dir, filename)
            shutil.copy2(audio_path, dest)

            # Build public URL — bot API serves /tts/audio/:filename
            audio_url = f"{self._bot_api_url}/tts/audio/{filename}"

            async with self._session.post(
                f"{self._bot_api_url}/tts/play",
                json={"audio_url": audio_url, "chat_id": chat_id},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("[DaemonCraft] /tts/play failed: %s %s", resp.status, body)
                    return SendResult(success=False, error=f"HTTP {resp.status}: {body}")
            return SendResult(success=True)
        except Exception as e:
            logger.warning("[DaemonCraft] /tts/play exception: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def play_tts(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        """Relay TTS audio to dashboards and send transcript to Minecraft chat."""
        result = await self._copy_and_relay_tts(audio_path, chat_id)
        if not result.success:
            return result

        # Also send the full text to Minecraft chat so players can read it
        text = kwargs.get("text", "[Voice message]")
        return await self.send(chat_id, text)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # Minecraft has no typing indicator — no-op
        pass

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Relay TTS audio to dashboards via the bot API."""
        return await self._copy_and_relay_tts(audio_path, chat_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if self._is_group_chat_id(chat_id) else "dm"
        return {"name": chat_id, "type": chat_type, "chat_id": chat_id}


# ------------------------------------------------------------------
# Requirements check
# ------------------------------------------------------------------

def check_daemoncraft_requirements() -> bool:
    """DaemonCraft only needs aiohttp (already a core dep)."""
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False
