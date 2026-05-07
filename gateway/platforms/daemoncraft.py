"""
DaemonCraft platform adapter for Hermes Gateway.

Routes Minecraft chat (player whispers + world broadcasts) through the
Hermes AIAgent, while the body adapter handles embodiment (movement,
quest engine, sensors).

The adapter consumes the Bot API WebSocket and HTTP endpoints via the
Body Protocol abstraction (daemoncraft_body.py), supporting both
Hermescraft (Mineflayer) and Mindcraft sidecar bodies.

  - WS /ws       : inbound chat events (array snapshot)
  - POST /chat/send   : outbound text
  - POST /tts/play    : outbound TTS relay to dashboards
  - GET  /agent/log   : recent loop turns for context injection
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Set

import aiohttp
from aiohttp import WSMsgType

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.platforms.daemoncraft_body import make_body, Body
from gateway.session import SessionSource, build_session_key

logger = logging.getLogger(__name__)

META_NO_CLAMP = "_no_clamp"  # Set in metadata to bypass gateway-side char clamping (used by TTS transcripts)


GATEWAY_HANDLES_QUEST_EVENTS = os.getenv("GATEWAY_HANDLES_QUEST_EVENTS", "0") == "1"
GATEWAY_HANDLES_CHAT = os.getenv("GATEWAY_HANDLES_CHAT", "0") == "1"


class DaemonCraftAdapter(BasePlatformAdapter):
    """Gateway adapter for DaemonCraft (Minecraft bot API)."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DAEMONCRAFT)
        self._bot_api_url: str = (config.extra or {}).get("bot_api_url", "")
        self._bot_username: str = (config.extra or {}).get("bot_username", "")
        self._profile: str = (config.extra or {}).get("profile", "")
        self._body_kind: str = (config.extra or {}).get("body_kind", "hermescraft")
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

        # Body abstraction (replaces hard-coded aiohttp calls)
        self._body: Optional[Body] = None
        if self._bot_api_url and self._bot_username:
            self._body = make_body(self._body_kind, self._bot_api_url, self._bot_username)
            logger.info(
                "[DaemonCraft] Body: %s @ %s as %s (screenshot=%s impersonation=%s set_goal=%s act=%s)",
                self._body.kind,
                self._body.api_url,
                self._body.username,
                self._body.supports_screenshot,
                self._body.supports_impersonation,
                self._body.supports_set_goal,
                self._body.supports_act,
            )

        # Plan tracking for heartbeat-driven progress evaluation and GC
        self._plan_goal: Optional[str] = None
        self._plan_tasks_snapshot: list = []
        self._plan_created_at: float = 0.0
        self._plan_last_progress_at: float = 0.0
        self._plan_gc_timeout: int = (config.extra or {}).get("plan_gc_timeout_seconds", 300)
        self._turn_counter: int = 0  # Sequential turn counter for agent logs

        # Phase-8 cycle detector. Disabled by default (n=0). Configurable via:
        #   MC_CYCLE_N      — minimum identical sigs in window to trigger
        #   MC_CYCLE_WINDOW — window size
        #   MC_CYCLE_ACTION — 'warn' | 'interrupt'
        cycle_n = int(os.getenv("MC_CYCLE_N", "0") or 0)
        cycle_window = int(os.getenv("MC_CYCLE_WINDOW", "6") or 6)
        cycle_action = (os.getenv("MC_CYCLE_ACTION") or "warn").lower()
        self._cycle_detector: Optional[CycleDetector] = None
        if cycle_n > 0:
            self._cycle_detector = CycleDetector(n=cycle_n, window=cycle_window, action=cycle_action)
            logger.info("[DaemonCraft] Cycle detector: n=%d window=%d action=%s", cycle_n, cycle_window, cycle_action)

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
        if self._body is None:
            logger.error("[DaemonCraft] body adapter failed to initialise")
            return False

        self._last_seen_timestamp = int(time.time() * 1000)
        self._shutdown_event.clear()
        self._session = aiohttp.ClientSession()
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

        Sets the bot_api_url context variable so that any minecraft tools
        dispatched for this message target the correct bot server.
        """
        from tools import minecraft_tools
        token = minecraft_tools._bot_api_url_ctx.set(self._bot_api_url)
        try:
            await super().handle_message(event)
        finally:
            minecraft_tools._bot_api_url_ctx.reset(token)

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        if self._body is None or self._session is None:
            logger.error("[DaemonCraft] Cannot start WS loop: body or session missing")
            return

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
            if GATEWAY_HANDLES_CHAT:
                await self._handle_chat_batch(messages)
            else:
                logger.debug("[DaemonCraft] Chat received but GATEWAY_HANDLES_CHAT=0, ignoring")
        elif msg_type == "quest_event":
            if GATEWAY_HANDLES_QUEST_EVENTS:
                data = payload.get("data", {})
                await self._handle_quest_event(data)
            else:
                logger.debug("[DaemonCraft] Quest event received but GATEWAY_HANDLES_QUEST_EVENTS=0, ignoring")
        elif msg_type == "blueprint_updated":
            if GATEWAY_HANDLES_QUEST_EVENTS:
                data = payload.get("data", {})
                await self._handle_blueprint_updated(data)
            else:
                logger.debug("[DaemonCraft] Blueprint update received but GATEWAY_HANDLES_QUEST_EVENTS=0, ignoring")
        elif msg_type == "heartbeat_context":
            data = payload.get("data", {})
            await self._handle_heartbeat_context(data)
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

        # Load known bots from env (same source as agent_loop.py)
        known_bots = set(
            u.strip().lower()
            for u in os.getenv("MC_KNOWN_BOTS", self._bot_username).split(",")
            if u.strip()
        )

        urgent_msgs = []
        accepted_msgs = []
        import re

        # Build a regex that matches @username with word boundaries,
        # tolerating trailing punctuation like @pamplinas, or @pamplinas!
        mention_re = re.compile(rf"\b@{re.escape(self._bot_username.lower())}\b", re.IGNORECASE)

        for entry in new_messages:
            from_user = entry.get("from", "").lower()
            msg_text = entry.get("message", "")
            is_bot = from_user in known_bots
            mentions_bot = bool(mention_re.search(msg_text))

            if is_bot and not mentions_bot:
                continue  # Silently drop bot spam

            accepted_msgs.append(entry)

            # Only human @mentions are urgent (bots never interrupt, even with @mention)
            if mentions_bot and not is_bot:
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
        if self._body is None or self._session is None:
            return
        try:
            result = await self._body.interrupt(self._session, reason)
            if not result.get("ok"):
                logger.warning("[DaemonCraft] /agent/interrupt failed: %s", result.get("error"))
            else:
                logger.debug("[DaemonCraft] /agent/interrupt sent (%s)", reason)
        except Exception as e:
            logger.warning("[DaemonCraft] /agent/interrupt exception: %s", e)

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

    async def _handle_heartbeat_context(self, data: dict) -> None:
        """Process heartbeat_context with two-level event architecture.

        - Context-only updates: inject synthetic mc_perceive tool result silently
          into the session_store. No LLM turn is forced.
        - Wake-up events: inject synthetic tool result + force an agent turn with
          tool_choice="required". The agent MUST react with a tool call (or mc_no_op).
        - Active plans: every heartbeat while a plan is active forces a wake_up so
          the agent evaluates progress against the plan.
        """
        plan = data.get("plan") or {}
        await self._update_plan_tracking(plan)

        # Run plan garbage collection before classification
        gc_reason = await self._maybe_gc_plan()
        if gc_reason:
            logger.info("[DaemonCraft] Plan GC: %s", gc_reason)
            # Inject cancellation as a system event
            await self._inject_synthetic_perceive({
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
                tool_choice="required",
            )
            await self.handle_message(event)
            return

        event_type = self._classify_heartbeat_event(data)
        logger.info("[DaemonCraft] Heartbeat classified as: %s", event_type)

        # Always inject synthetic perceive into session store
        await self._inject_synthetic_perceive(data)

        if event_type == "context":
            logger.debug("[DaemonCraft] Context-only heartbeat injected silently")
            return

        # Wake-up event: force an agent turn with tool_choice=required
        plan_goal = self._plan_goal
        if plan_goal and event_type == "wake_up":
            prompt_text = (
                f"[System: Evaluate progress on plan '{plan_goal}'. "
                f"Current tasks: {len(self._plan_tasks_snapshot)}. "
                f"Use mc_plan(action='get_plan') to review, mc_plan(action='update_task') to mark progress, "
                f"or other tools to advance the active task.]"
            )
        else:
            prompt_text = "[System: React to the perceptual update above using available tools.]"

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
            tool_choice="required",
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
            # Clear plan on bot server via body adapter
            if self._body and self._session:
                try:
                    await self._body._post(
                        self._session, "/plan/update", {"action": "clear_goal"}
                    )
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

    def _classify_heartbeat_event(self, data: dict) -> str:
        """Classify heartbeat as 'context' or 'wake_up'.

        Wake-up triggers:
        - Bot is stuck on a movement task (task_stuck in status)
        - Active plan exists (agent must evaluate progress every heartbeat)
        - Health decreased from previous known value
        - Nearby hostile entities (zombie, skeleton, creeper, spider)
        - Explicit damage events in events list
        """
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
                self._last_health = current_health
                return "wake_up"
        if current_health is not None:
            self._last_health = current_health

        # Explicit damage events
        for ev in events:
            ev_str = str(ev).lower()
            if any(k in ev_str for k in ("damage", "hurt", "attack", "hit", "died", "killed")):
                return "wake_up"

        # Nearby hostile mobs
        hostile = {"zombie", "skeleton", "creeper", "spider", "enderman", "witch", "husk", "drowned", "phantom"}
        for ent in nearby.get("entities", [])[:12]:
            name = str(ent.get("name", ent) if isinstance(ent, dict) else ent).lower()
            if any(h in name for h in hostile):
                return "wake_up"

        return "context"

    async def _maybe_mutate_plan_from_chat(self, player_message: str) -> None:
        """DC-107: Auto-create plan when player asks for something actionable.

        If no active plan exists and the player's message contains actionable
        keywords, create a plan goal directly via the bot server so the loop
        can execute it on the next heartbeat.
        """
        if self._body is None or self._session is None:
            return
        try:
            # Fetch current plan
            plan = await self._body.get_plan(self._session)
            if not isinstance(plan, dict):
                plan = {}

            # If plan already has a goal, don't overwrite
            if plan.get("goal"):
                return

            # Simple heuristic: detect actionable intent in player message
            msg_lower = player_message.lower()
            action_keywords = [
                "build", "make", "craft", "gather", "collect", "mine", "get",
                "go to", "go ", "find", "kill", "attack", "defend", "protect",
                "plant", "harvest", "farm", "cook", "smelt", "enchant",
                "explore", "dig", "place", "build me", "make me", "can you",
                "please", "help me", "need", "want", "bring", "fetch",
                "create", "construct", "destroy", "remove", "clear",
            ]
            if not any(kw in msg_lower for kw in action_keywords):
                return

            # Sanitize goal text
            goal = player_message.strip()
            if len(goal) > 200:
                goal = goal[:200] + "..."

            epoch = plan.get("epoch", 0)
            result = await self._body._post(
                self._session,
                "/action/plan",
                {
                    "action": "set_goal",
                    "goal": goal,
                    "tasks": [],
                    "expected_epoch": epoch,
                },
            )
            if result and result.get("ok"):
                logger.info("[DaemonCraft] Auto-created plan from chat: %s", goal)
            else:
                logger.warning("[DaemonCraft] Plan mutation failed: %s", result.get("error") if result else "no response")
        except Exception as e:
            logger.warning("[DaemonCraft] Plan mutation check failed: %s", e)

    async def _inject_synthetic_perceive(self, data: dict) -> None:
        """Inject a fake assistant tool_call + tool result into the world session."""
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
        self._session_store.append_to_transcript(session_id, tool_msg)
        logger.info("[DaemonCraft] Synthetic mc_perceive injected into session %s", session_id)

    def _get_world_session_id(self) -> Optional[str]:
        """Resolve the session_id for the world broadcast session."""
        if not self._session_store:
            return None
        source = SessionSource(
            platform=Platform.DAEMONCRAFT,
            chat_id=self._group_chat_id(),
            chat_type="group",
            user_id=self._bot_username,
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

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=entry,
        )

        await self.handle_message(event)

        # DC-107: If the player asked for something actionable and no plan exists,
        # auto-create a plan so the loop can execute it on next heartbeat.
        await self._maybe_mutate_plan_from_chat(text)

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
        if self._body is None or self._session is None:
            logger.warning("[DaemonCraft] Cannot send: body or session missing")
            return SendResult(success=False, error="body not initialised")

        # Log agent turn to bot server for dashboard display
        await self._post_agent_log(content, metadata)

        # _world_names is populated lazily from inbound broadcasts. If the gateway
        # initiates an outbound broadcast before any inbound from that world, this
        # will default to DM (whisper). For now the agent only replies to inbound.
        is_group = self._is_group_chat_id(chat_id)
        target = "broadcast" if is_group else chat_id

        result = await self._body.chat(self._session, content)
        if result.get("ok"):
            return SendResult(success=True)
        return SendResult(success=False, error=result.get("error", "unknown"), retryable=True)

    async def _post_agent_log(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Post agent turn to bot server /agent/log for dashboard display."""
        if self._body is None or self._session is None:
            return
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
            await self._body.log_turn(self._session, payload)
        except Exception as e:
            logger.debug("[DaemonCraft] /agent/log exception: %s", e)

    async def _copy_and_relay_tts(self, audio_path: str, chat_id: str) -> SendResult:
        """Copy audio to shared TTS cache and POST /tts/play to dashboards."""
        if self._body is None or self._session is None:
            return SendResult(success=False, error="body not initialised")
        try:
            import shutil

            tts_dir = "/tmp/daemoncraft-tts"
            os.makedirs(tts_dir, exist_ok=True)
            filename = os.path.basename(audio_path)
            dest = os.path.join(tts_dir, filename)
            shutil.copy2(audio_path, dest)

            # Build public URL — bot API serves /tts/audio/:filename
            audio_url = f"{self._bot_api_url}/tts/audio/{filename}"

            result = await self._body._post(
                self._session, "/tts/play", {"audio_url": audio_url, "chat_id": chat_id}
            )
            if result and result.get("ok"):
                return SendResult(success=True)
            return SendResult(
                success=False,
                error=result.get("error", "HTTP error") if result else "no response",
            )
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

    async def _check_cycle(self, name: str, args: Any) -> bool:
        """Feed a tool call to the cycle detector. Returns True if an interrupt is warranted."""
        if self._cycle_detector is None:
            return False
        result = self._cycle_detector.record(name, args)
        if result.triggered:
            logger.warning(
                "[DaemonCraft] CYCLE DETECTED: tool=%s sig=%s count=%d/%d action=%s",
                name, result.sig, result.count, result.window, result.action,
            )
            if result.action == "interrupt":
                await self._interrupt_agent("cycle_detected")
                return True
            # warn / log / chat actions are advisory — log only
        return False


# ------------------------------------------------------------------
# CycleDetector (ported from DaemonCraft agents/safety.py)
# ------------------------------------------------------------------

def _canonicalize(args: Any) -> str:
    """Stable JSON for any args structure."""
    try:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return args
        return json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return repr(args)


def _signature(name: str, args: Any) -> str:
    payload = f"{name}|{_canonicalize(args)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass
class CycleResult:
    triggered: bool
    sig: Optional[str]
    count: int
    window: int
    action: str


@dataclass
class CycleDetector:
    """Ring-buffer cycle detector."""
    n: int = 4
    window: int = 6
    action: str = "log"
    _buf: Deque[str] = field(default_factory=deque)
    _last_triggered_sig: Optional[str] = None

    def __post_init__(self) -> None:
        self._buf = deque(maxlen=max(self.window, self.n))

    def record(self, name: str, args: Any) -> CycleResult:
        sig = _signature(name, args)
        self._buf.append(sig)
        return self.evaluate()

    def evaluate(self) -> CycleResult:
        if len(self._buf) < self.n:
            return CycleResult(False, None, 0, len(self._buf), self.action)
        counts: Dict[str, int] = {}
        for s in self._buf:
            counts[s] = counts.get(s, 0) + 1
        top_sig, top_count = max(counts.items(), key=lambda kv: kv[1])
        if top_count >= self.n:
            if top_sig == self._last_triggered_sig:
                return CycleResult(False, top_sig, top_count, len(self._buf), self.action)
            self._last_triggered_sig = top_sig
            return CycleResult(True, top_sig, top_count, len(self._buf), self.action)
        if self._last_triggered_sig and self._last_triggered_sig != top_sig:
            self._last_triggered_sig = None
        return CycleResult(False, top_sig, top_count, len(self._buf), self.action)

    def reset(self) -> None:
        self._buf.clear()
        self._last_triggered_sig = None


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
