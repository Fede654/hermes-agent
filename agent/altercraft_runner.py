"""altercraft_runner — spawn a Hermes persona into the AlterCraft world.

Two-process design, forked+supervised by this module:

  1. Node.js bot server (``~/.hermes/altercraft/server.js``) running as a
     child process. This is the Mineflayer "body" that connects to the
     Paper server over anyVPN and exposes a localhost HTTP API.

  2. AIAgent (Hermes, Python) constructed in-process. It uses the
     ``altercraft`` toolset (see ``tools/altercraft_tool.py``) whose HTTP
     client targets the child's API. The outer reactive loop polls chat
     events and feeds addressed-to-me messages into the agent as single
     ``chat()`` turns.

Usage:

    python -m agent.altercraft_runner <persona> [--duration Nm|Ns|Nh]
                                                [--model MODEL]
                                                [--mc-host HOST]
                                                [--mc-port PORT]
                                                [--dry-run]

Profile is loaded from ``~/.hermes/profiles/altercraft-<persona>/config.yaml``.
All session state written to
``~/.hermes/profiles/altercraft-<persona>/sessions/<session-id>/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

logger = logging.getLogger("altercraft_runner")

# ────────────────────────────────────────────────────────────────────────────
# Memory helpers (lazy import to avoid circular deps)
# ────────────────────────────────────────────────────────────────────────────

def _maybe_import_memory():
    try:
        from agent import altercraft_memory as mem
        return mem
    except Exception:
        return None


def _memory_enabled(profile: dict) -> bool:
    mem = profile.get("memory") or {}
    return bool(mem.get("enabled", False))


def _max_events(profile: dict) -> int:
    mem = profile.get("memory") or {}
    return mem.get("max_events", 500)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def _parse_duration(s: str) -> float:
    """Parse '30m', '2h', '45s', or bare seconds into seconds."""
    m = re.fullmatch(r"(\d+)\s*([smh]?)", s.strip().lower())
    if not m:
        raise ValueError(f"bad duration {s!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return {"s": 1, "m": 60, "h": 3600}[unit] * n


def _args():
    p = argparse.ArgumentParser(description="Spawn a Hermes persona into AlterCraft.")
    p.add_argument("persona", help="Persona name, e.g. 'clio'. Profile at ~/.hermes/profiles/altercraft-<persona>/.")
    p.add_argument("--duration", default="30m", help="Session duration (e.g. 30m, 2h, 45s). Default 30m.")
    p.add_argument("--model", default=None, help="Override model from profile.")
    p.add_argument("--mc-host", default=None, help="Override mc_server host from profile.")
    p.add_argument("--mc-port", type=int, default=None, help="Override mc_server port.")
    p.add_argument("--dry-run", action="store_true", help="Load profile, skip spawning.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


# ────────────────────────────────────────────────────────────────────────────
# Profile loading
# ────────────────────────────────────────────────────────────────────────────

def _profile_dir(persona: str) -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "profiles" / f"altercraft-{persona}"


def _load_profile(persona: str) -> dict:
    cfg_path = _profile_dir(persona) / "config.yaml"
    if not cfg_path.is_file():
        raise FileNotFoundError(f"profile config missing: {cfg_path}")
    with open(cfg_path, "r") as f:
        data = yaml.safe_load(f) or {}
    return data.get("profile", {})


# ────────────────────────────────────────────────────────────────────────────
# Network helpers
# ────────────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Bind-and-release on an ephemeral port. Works for "best-effort" free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(api_url: str, timeout_s: float = 30.0) -> None:
    start = time.monotonic()
    last_err = None
    while time.monotonic() - start < timeout_s:
        try:
            with httpx.Client(timeout=2.0) as c:
                r = c.get(f"{api_url}/health")
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    raise TimeoutError(f"bot server at {api_url} not healthy after {timeout_s}s (last: {last_err})")


# ────────────────────────────────────────────────────────────────────────────
# Node bot-server child process
# ────────────────────────────────────────────────────────────────────────────

class BotServer:
    """Manages the node server.js child process."""

    def __init__(self, *, mc_host: str, mc_port: int, mc_username: str,
                 api_port: int, session_dir: Path, fair_play: bool = True):
        self.mc_host = mc_host
        self.mc_port = mc_port
        self.mc_username = mc_username
        self.api_port = api_port
        self.session_dir = session_dir
        self.fair_play = fair_play
        self.api_url = f"http://127.0.0.1:{api_port}"
        self.proc: Optional[subprocess.Popen] = None
        self._log_file = None

    def start(self) -> None:
        altercraft_dir = Path.home() / ".hermes" / "altercraft"
        server_js = altercraft_dir / "server.js"
        if not server_js.is_file():
            raise FileNotFoundError(f"server.js missing: {server_js}")

        env = os.environ.copy()
        env.update({
            "MC_HOST": self.mc_host,
            "MC_PORT": str(self.mc_port),
            "MC_USERNAME": self.mc_username,
            "MC_AUTH": "offline",
            "API_PORT": str(self.api_port),
            "FAIR_PLAY": "true" if self.fair_play else "false",
        })
        self.session_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.session_dir / "server.log"
        self._log_file = open(log_path, "a", buffering=1)
        logger.info("spawning node server: %s (cwd=%s, API :%d)",
                    server_js, altercraft_dir, self.api_port)
        self.proc = subprocess.Popen(
            ["node", str(server_js)],
            cwd=str(altercraft_dir),
            env=env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logger.info("node server pid=%d; waiting for /health", self.proc.pid)
        try:
            _wait_for_health(self.api_url, timeout_s=45)
        except TimeoutError:
            self.stop()
            raise
        logger.info("bot server healthy at %s", self.api_url)

    def stop(self, *, graceful_seconds: float = 10.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            logger.info("node server already exited (rc=%s)", self.proc.returncode)
        else:
            logger.info("sending SIGTERM to node server pid=%d", self.proc.pid)
            try:
                self.proc.terminate()
                self.proc.wait(timeout=graceful_seconds)
            except subprocess.TimeoutExpired:
                logger.warning("node server did not exit gracefully; SIGKILL")
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Reactive loop
# ────────────────────────────────────────────────────────────────────────────

class ReactiveLoop:
    """Polls chat events; on addressed messages, runs an AIAgent turn."""

    def __init__(self, *, agent, api_url: str, mc_username: str,
                 min_seconds_between_responses: float = 10.0,
                 poll_interval_s: float = 5.0,
                 session_dir: Path,
                 persona: str = "",
                 memory_enabled: bool = False,
                 max_events: int = 500):
        self.agent = agent
        self.api_url = api_url
        self.mc_username = mc_username
        self.min_gap = min_seconds_between_responses
        self.poll = poll_interval_s
        self.session_dir = session_dir
        self.last_response_ts = 0.0
        self.last_seen_ms = int(time.time() * 1000)
        self._stop = threading.Event()
        self._events_fh = open(session_dir / "events.jsonl", "a", buffering=1)
        self.persona = persona
        self.memory_enabled = memory_enabled
        self.max_events = max_events
        self._mem = _maybe_import_memory()

    def stop(self) -> None:
        self._stop.set()

    def _fetch_events(self) -> list[dict]:
        try:
            with httpx.Client(timeout=5.0) as c:
                r = c.get(f"{self.api_url}/chat", params={"since": self.last_seen_ms, "limit": 50})
            if r.status_code != 200:
                logger.warning("/chat returned %s", r.status_code)
                return []
            body = r.json()
        except Exception as e:
            logger.warning("fetch chat failed: %s", e)
            return []
        if isinstance(body, dict):
            # server.js returns {"ok": true, "data": {"messages": [...]}}.
            data = body.get("data") or {}
            if isinstance(data, dict):
                body = data.get("messages") or data.get("events") or body.get("messages") or body.get("events") or []
            else:
                body = data if isinstance(data, list) else (body.get("messages") or body.get("events") or [])
        return body or []

    def _is_addressed(self, event: dict) -> bool:
        # Heuristics that match server.js chat conventions:
        # - `private: true`  → this was a DM to us specifically
        # - `Name:` or `Name,` prefix in the text → routed to us
        # - Our name appears anywhere in the text → mention
        text = (event.get("message") or event.get("text") or "").strip()
        etype = event.get("type") or event.get("kind") or ""
        speaker = (event.get("from") or event.get("username")
                   or event.get("speaker") or "")
        if speaker == self.mc_username:
            return False  # don't respond to self
        if event.get("private") is True:
            return True
        if etype in ("whisper", "dm"):
            return True
        lt = text.lower()
        un = self.mc_username.lower()
        if lt.startswith(f"{un}:") or lt.startswith(f"{un},"):
            return True
        if un in lt:
            return True
        return False

    def _record_event(self, event: dict, verdict: str) -> None:
        try:
            self._events_fh.write(json.dumps({
                "ts": time.time(),
                "event": event,
                "verdict": verdict,
            }, default=str) + "\n")
        except Exception:
            pass

    def _advance_seen(self, event: dict) -> None:
        ts = event.get("time") or event.get("timestamp") or event.get("ts")
        if isinstance(ts, (int, float)) and ts > self.last_seen_ms:
            self.last_seen_ms = int(ts) + 1

    def _respond(self, event: dict) -> None:
        text = (event.get("message") or event.get("text") or "").strip()
        speaker = (event.get("from") or event.get("username")
                   or event.get("speaker") or "someone")
        # Strip the "Name:" routing prefix if present
        prefix = f"{self.mc_username}:"
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].lstrip()
        prefix2 = f"{self.mc_username},"
        if text.lower().startswith(prefix2.lower()):
            text = text[len(prefix2):].lstrip()

        user_msg = (
            f"In-world chat event:\n"
            f"- speaker: {speaker}\n"
            f"- to: {self.mc_username}\n"
            f"- text: {text}\n\n"
            "Use altercraft_look to ground yourself if needed. "
            "Reply briefly with altercraft_say (public) or altercraft_whisper "
            f"(to {speaker} privately). One short response; then stop."
        )
        logger.info("agent turn triggered by %s: %r", speaker, text[:120])
        try:
            reply = self.agent.chat(user_msg)
            logger.info("agent turn complete: %r", (reply or "")[:200])
        except Exception as e:
            logger.exception("agent turn crashed: %s", e)
            return
        self.last_response_ts = time.monotonic()

    def run(self) -> None:
        logger.info("reactive loop started (mc_username=%s, poll=%.1fs, min_gap=%.1fs)",
                    self.mc_username, self.poll, self.min_gap)
        while not self._stop.is_set():
            cutoff = self.last_seen_ms  # snapshot; server ignores `since` so filter client-side
            events = self._fetch_events()
            for ev in events:
                ts = ev.get("time") or ev.get("timestamp") or ev.get("ts") or 0
                if not isinstance(ts, (int, float)) or ts <= cutoff:
                    continue  # already seen
                self._advance_seen(ev)
                addressed = self._is_addressed(ev)
                self._record_event(ev, "addressed" if addressed else "ignored")
                if not addressed:
                    continue
                gap = time.monotonic() - self.last_response_ts
                if gap < self.min_gap:
                    logger.info("rate-limit: %.1fs since last reply, waiting", gap)
                    continue
                self._respond(ev)
            self._stop.wait(self.poll)
        try:
            self._events_fh.close()
        except Exception:
            pass
        logger.info("reactive loop exited")


# ────────────────────────────────────────────────────────────────────────────
# AIAgent construction
# ────────────────────────────────────────────────────────────────────────────

def _build_agent(profile: dict, session_id: str, model_override: Optional[str]):
    """Construct a Hermes AIAgent configured per the profile."""
    # Allow importing run_agent from the hermes-agent source tree we're in.
    hermes_src = Path(__file__).resolve().parents[1]
    if str(hermes_src) not in sys.path:
        sys.path.insert(0, str(hermes_src))

    from run_agent import AIAgent  # noqa: E402

    m = profile.get("model", {}) or {}
    model = model_override or m.get("default") or "kimi-k2.6"
    provider = m.get("provider")
    # base_url / api_key only passed if the profile explicitly declares them.
    # Otherwise we let Hermes resolve provider auth through its own layer
    # (e.g. hermes model login / ~/.hermes auth cache).
    base_url = m.get("base_url")
    api_key = m.get("api_key")
    temperature = m.get("temperature")
    toolsets = list(profile.get("toolsets", ["altercraft", "memory"]))
    if "altercraft" not in toolsets:
        toolsets.insert(0, "altercraft")

    identity = profile.get("identity", {}) or {}
    mc_username = (profile.get("environment") or {}).get("mc_username") or "HermesBot"
    persona_desc = (identity.get("description") or "").strip()
    voice = (identity.get("voice") or "").strip()
    goals = identity.get("goals") or []
    goals_text = "\n".join(f"- {g}" for g in goals) if goals else ""

    # Load persistent memory summary if enabled
    memory_para = ""
    if _memory_enabled(profile):
        mem_mod = _maybe_import_memory()
        if mem_mod:
            try:
                summary = mem_mod.summarize_memory(
                    (profile.get("name") or "").replace("altercraft-", "")
                )
                if summary:
                    memory_para = f"\n\nPersistent memory:\n{summary}\n"
            except Exception as exc:
                logger.warning("memory summary failed: %s", exc)

    ephemeral_prompt = (
        f"You are embodied in Minecraft as '{mc_username}'. "
        f"You live on the AlterCraft server hosted on the AlterMundi anyVPN. "
        f"You act through the `altercraft_*` tools — altercraft_look to see, "
        f"altercraft_say/altercraft_whisper to speak, altercraft_goto to move, "
        f"altercraft_status/altercraft_inventory to self-check. "
        f"Before speaking or moving, use altercraft_look once to ground "
        f"yourself. Keep replies short and natural — you are speaking as a "
        f"character, not delivering paragraphs. If a message is not clearly "
        f"addressed to you, you can stay silent.\n\n"
        f"Character:\n{persona_desc}"
    )
    if voice:
        ephemeral_prompt += f"\n\nVoice / tone:\n{voice}"
    if goals_text:
        ephemeral_prompt += f"\n\nGoals:\n{goals_text}"
    if memory_para:
        ephemeral_prompt += memory_para

    max_tokens_cap = (profile.get("budget") or {}).get("max_tokens_per_task")
    max_turns = m.get("max_turns") or 20

    agent = AIAgent(
        model=model,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        enabled_toolsets=toolsets,
        quiet_mode=True,
        platform="cli",
        session_id=f"altercraft:{session_id}",
        skip_context_files=True,
        skip_memory=True,
        ephemeral_system_prompt=ephemeral_prompt,
        max_iterations=max_turns,
        save_trajectories=True,
    )

    # Patch attributes some codepaths expect (mirrors research_job_runner).
    agent._delegate_depth = 0
    agent.terminal_cwd = os.getcwd()
    agent.cwd = os.getcwd()
    agent._subdirectory_hints = None
    agent._delegate_spinner = None
    agent.tool_progress_callback = lambda *a, **k: None
    agent.providers_allowed = getattr(agent, "providers_allowed", None)
    agent.providers_ignored = getattr(agent, "providers_ignored", None)
    agent.providers_order = getattr(agent, "providers_order", None)
    agent.provider_sort = getattr(agent, "provider_sort", None)
    return agent, model, toolsets


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = _args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    profile = _load_profile(args.persona)
    env_cfg = profile.get("environment") or {}
    mc_server = env_cfg.get("mc_server", "10.10.20.1:25565")
    if args.mc_host:
        mc_host = args.mc_host
        mc_port = args.mc_port or int(mc_server.rsplit(":", 1)[1])
    else:
        host, port = mc_server.rsplit(":", 1)
        mc_host = host
        mc_port = args.mc_port or int(port)
    mc_username = env_cfg.get("mc_username") or f"Hermes-{args.persona.capitalize()}"

    # ULID-like: sortable timestamp prefix + random tail. No external dep.
    session_id = f"{int(time.time()*1000):013x}-{uuid.uuid4().hex[:12]}"
    session_dir = _profile_dir(args.persona) / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    logger.info("persona=%s session=%s", args.persona, session_id)

    if args.dry_run:
        logger.info("--dry-run: would spawn bot to %s:%d as %s, API on free port",
                    mc_host, mc_port, mc_username)
        return 0

    duration_s = _parse_duration(args.duration)
    api_port = _free_port()

    os.environ["MC_API_URL"] = f"http://127.0.0.1:{api_port}"

    bot = BotServer(
        mc_host=mc_host, mc_port=mc_port, mc_username=mc_username,
        api_port=api_port, session_dir=session_dir,
    )

    loop: Optional[ReactiveLoop] = None

    def _sigterm(_signo, _frame):
        logger.info("received signal; shutting down")
        if loop:
            loop.stop()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    try:
        bot.start()
    except Exception as e:
        logger.exception("bot server failed to start: %s", e)
        return 2

    try:
        agent, model_used, toolsets = _build_agent(profile, session_id, args.model)
    except Exception as e:
        logger.exception("failed to build AIAgent: %s", e)
        bot.stop()
        return 3

    (session_dir / "session.json").write_text(json.dumps({
        "persona": args.persona,
        "session_id": session_id,
        "mc_server": f"{mc_host}:{mc_port}",
        "mc_username": mc_username,
        "api_port": api_port,
        "model": model_used,
        "toolsets": toolsets,
        "started_at": time.time(),
    }, indent=2))

    behavior = profile.get("behavior") or {}
    loop = ReactiveLoop(
        agent=agent,
        api_url=f"http://127.0.0.1:{api_port}",
        mc_username=mc_username,
        min_seconds_between_responses=float(behavior.get("min_seconds_between_responses", 10)),
        poll_interval_s=5.0,
        session_dir=session_dir,
    )

    deadline = time.monotonic() + duration_s

    def _timer_stop():
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        logger.info("duration elapsed (%.0fs); stopping loop", duration_s)
        loop.stop()

    timer_thread = threading.Thread(target=_timer_stop, daemon=True)
    timer_thread.start()

    try:
        loop.run()
    finally:
        logger.info("stopping bot server")
        bot.stop()

    (session_dir / "session.md").write_text(
        f"# altercraft session {session_id}\n\n"
        f"Persona: {args.persona}\n"
        f"Duration: {args.duration}\n"
        f"MC: {mc_username} @ {mc_host}:{mc_port}\n"
        f"Ended: {time.asctime()}\n"
    )

    logger.info("session complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
