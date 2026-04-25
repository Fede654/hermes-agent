#!/usr/bin/env python3
"""altercraft_multiagent — Lattice-native multi-agent orchestration for AlterCraft.

Replaces the shell-script civilization mode with a Python director that spawns N
agents, coordinates them via claim-based task allocation, and manages anti-
collision spatial locks.  Merge-ready: all helpers are importable; the CLI is
optional.

Usage (CLI):
    python -m agent.altercraft_multiagent clio builder-1 gatherer-1 \
        --duration 30m --mc-host 10.10.20.1 --mc-port 25565 \
        --rcon-pass-file /tmp/rcon.pass --rcon-port 25575

Usage (imported):
    from agent.altercraft_multiagent import SwarmDirector
    director = SwarmDirector(personas=["clio","builder-1"], ...)
    director.spawn_all()
    director.assign_task("builder-1", {"type":"build","region":[[0,64,0],[5,68,5]]})
    director.run_director_loop()          # blocks until duration expires
    director.stop_all()
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# ── reuse existing altercraft primitives ────────────────────────────────────
HERMES_SRC = Path(__file__).resolve().parents[1]
if str(HERMES_SRC) not in sys.path:
    sys.path.insert(0, str(HERMES_SRC))

from agent.altercraft_runner import (
    BotServer,
    ReactiveLoop,
    _build_agent as _build_single_agent,
    _free_port,
    _load_profile,
    _profile_dir,
)
from agent.altercraft_movement_loop import _Rcon as _BaseRcon

logger = logging.getLogger("altercraft_multiagent")


# ────────────────────────────────────────────────────────────────────────────
# 1.  Shared infrastructure  (RCON mutex + spatial ledger)
# ────────────────────────────────────────────────────────────────────────────

class RconClient:
    """Thread-safe RCON wrapper.  All server-side commands go through here."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._rcon = _BaseRcon(host, port, password)
        self._lock = threading.Lock()

    def send(self, cmd: str) -> str:
        with self._lock:
            return self._rcon.send(cmd)


class SpatialLedger:
    """In-memory block-reservation table.  Optionally backed by a JSONL file."""

    def __init__(self, backing_file: Optional[Path] = None) -> None:
        self._table: dict[tuple[int, int, int], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._file = backing_file

    def _now(self) -> float:
        return time.monotonic()

    def _sweep(self) -> None:
        now = self._now()
        stale = [k for k, (_, exp) in self._table.items() if exp < now]
        for k in stale:
            del self._table[k]

    def reserve(self, x: int, y: int, z: int, owner: str, ttl: float = 60.0) -> bool:
        """Attempt to reserve a single block.  True on success, False if taken."""
        key = (x, y, z)
        with self._lock:
            self._sweep()
            if key in self._table:
                return False
            self._table[key] = (owner, self._now() + ttl)
            self._persist()
            return True

    def reserve_envelope(self, c1, c2, owner: str, ttl: float = 60.0) -> list[tuple[int, int, int]]:
        """Reserve every integer block inside axis-aligned bounding box.
        Returns list of *failed* coordinates (empty == full success)."""
        x1, y1, z1 = map(int, c1)
        x2, y2, z2 = map(int, c2)
        lo, hi = lambda a, b: (min(a, b), max(a, b))
        xl, xh = lo(x1, x2); yl, yh = lo(y1, y2); zl, zh = lo(z1, z2)
        failed: list[tuple[int, int, int]] = []
        with self._lock:
            self._sweep()
            for x in range(xl, xh + 1):
                for y in range(yl, yh + 1):
                    for z in range(zl, zh + 1):
                        if (x, y, z) in self._table:
                            failed.append((x, y, z))
            if not failed:
                exp = self._now() + ttl
                for x in range(xl, xh + 1):
                    for y in range(yl, yh + 1):
                        for z in range(zl, zh + 1):
                            self._table[(x, y, z)] = (owner, exp)
                self._persist()
        return failed

    def release(self, x: int, y: int, z: int, owner: str) -> bool:
        key = (x, y, z)
        with self._lock:
            if key not in self._table:
                return True
            if self._table[key][0] != owner:
                return False
            del self._table[key]
            self._persist()
            return True

    def release_all(self, owner: str) -> int:
        with self._lock:
            keys = [k for k, (o, _) in self._table.items() if o == owner]
            for k in keys:
                del self._table[k]
            self._persist()
            return len(keys)

    def _persist(self) -> None:
        if self._file is None:
            return
        tmp = self._file.with_suffix(".tmp")
        with self._lock:
            rows = [
                {"x": k[0], "y": k[1], "z": k[2], "owner": v[0], "expiry": v[1]}
                for k, v in self._table.items()
            ]
        tmp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        tmp.rename(self._file)


# ────────────────────────────────────────────────────────────────────────────
# 2.  AgentHandle  (wraps one BotServer + ReactiveLoop)
# ────────────────────────────────────────────────────────────────────────────

class AgentHandle:
    """Lightweight wrapper around a single spawned agent."""

    def __init__(self, persona: str, profile: dict, mc_host: str, mc_port: int,
                 api_port: int, session_dir: Path, model_override: Optional[str] = None):
        self.persona = persona
        self.profile = profile
        self.mc_username = (profile.get("environment") or {}).get("mc_username") or f"Hermes-{persona.capitalize()}"
        self.api_url = f"http://127.0.0.1:{api_port}"
        self.session_dir = session_dir
        self._bot = BotServer(
            mc_host=mc_host, mc_port=mc_port, mc_username=self.mc_username,
            api_port=api_port, session_dir=session_dir,
        )
        self._agent: Any = None
        self._loop: Optional[ReactiveLoop] = None
        self._model_override = model_override

    def start(self) -> None:
        self._bot.start()
        os.environ["MC_API_URL"] = self.api_url
        self._agent, model_used, toolsets = _build_single_agent(
            self.profile, self.session_dir.name, self._model_override
        )
        (self.session_dir / "session.json").write_text(json.dumps({
            "persona": self.persona,
            "session_id": self.session_dir.name,
            "mc_username": self.mc_username,
            "api_url": self.api_url,
            "model": model_used,
            "toolsets": toolsets,
            "started_at": time.time(),
        }, indent=2))
        behavior = self.profile.get("behavior") or {}
        self._loop = ReactiveLoop(
            agent=self._agent,
            api_url=self.api_url,
            mc_username=self.mc_username,
            min_seconds_between_responses=float(
                behavior.get("min_seconds_between_responses", 10)
            ),
            poll_interval_s=5.0,
            session_dir=self.session_dir,
        )
        # Run loop in its own thread so the director can multiplex many agents.
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()
        logger.info("agent %s loop started", self.mc_username)

    def stop(self) -> None:
        if self._loop:
            self._loop.stop()
        if getattr(self, "_thread", None):
            self._thread.join(timeout=10)
        self._bot.stop()
        logger.info("agent %s stopped", self.mc_username)

    def whisper(self, target: str, message: str) -> str:
        """Direct API helper — bypasses the LLM loop for director commands."""
        import httpx
        try:
            r = httpx.post(
                f"{self.api_url}/action/chat_to",
                json={"player": target, "message": message},
                timeout=5.0,
            )
            return f"[{self.mc_username}] -> {target}: {message}"
        except Exception as e:
            return f"Error whispering from {self.mc_username}: {e}"

    def say(self, message: str) -> str:
        import httpx
        try:
            r = httpx.post(
                f"{self.api_url}/action/chat",
                json={"message": message},
                timeout=5.0,
            )
            return f"[{self.mc_username}] : {message}"
        except Exception as e:
            return f"Error saying from {self.mc_username}: {e}"

    def fetch_status(self) -> dict:
        import httpx
        try:
            r = httpx.get(f"{self.api_url}/status", timeout=5.0)
            return r.json() if r.status_code == 200 else {"error": r.status_code}
        except Exception as e:
            return {"error": str(e)}


# ────────────────────────────────────────────────────────────────────────────
# 3.  SwarmDirector  (multi-agent spawn + coordination)
# ────────────────────────────────────────────────────────────────────────────

class SwarmDirector:
    """Spawns N agents, aggregates state, assigns tasks, and guards collisions."""

    def __init__(
        self,
        personas: list[str],
        *,
        mc_host: str = "10.10.20.1",
        mc_port: int = 25565,
        rcon_host: str = "10.10.20.1",
        rcon_port: int = 25575,
        rcon_pass_file: str = "/tmp/rcon.pass",
        duration_s: float = 1800.0,
        model_override: Optional[str] = None,
        stagger_s: float = 5.0,
    ) -> None:
        self.personas = personas
        self.mc_host = mc_host
        self.mc_port = mc_port
        self.rcon = RconClient(
            rcon_host, rcon_port,
            Path(rcon_pass_file).read_text().strip()
        )
        self.duration_s = duration_s
        self.model_override = model_override
        self.stagger_s = stagger_s

        run_id = f"{int(time.time()*1000):013x}-{uuid.uuid4().hex[:8]}"
        self.run_dir = Path.home() / ".hermes" / "research-jobs" / f"altercraft-swarm-{run_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.run_dir / "locks"
        self.locks_dir.mkdir(exist_ok=True)

        self.ledger = SpatialLedger(backing_file=self.locks_dir / "blocks.jsonl")
        self.agents: dict[str, AgentHandle] = {}
        self._stop = threading.Event()

        # Simple task board (could be promoted to SQLite later)
        self._tasks: list[dict] = []
        self._task_lock = threading.Lock()

    def _new_session_dir(self, persona: str) -> Path:
        sid = f"{int(time.time()*1000):013x}-{uuid.uuid4().hex[:12]}"
        d = _profile_dir(persona) / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def spawn_all(self) -> None:
        for p in self.personas:
            prof = _load_profile(p)
            api_port = _free_port()
            session_dir = self._new_session_dir(p)
            handle = AgentHandle(
                persona=p, profile=prof, mc_host=self.mc_host,
                mc_port=self.mc_port, api_port=api_port,
                session_dir=session_dir, model_override=self.model_override,
            )
            logger.info("spawning %s on port %d -> %s", p, api_port, session_dir)
            handle.start()
            self.agents[handle.mc_username] = handle
            if self.stagger_s:
                time.sleep(self.stagger_s)
        (self.run_dir / "swarm.json").write_text(json.dumps({
            "run_id": self.run_dir.name,
            "agents": [
                {"persona": a.persona, "username": a.mc_username, "api_url": a.api_url}
                for a in self.agents.values()
            ],
            "started_at": time.time(),
        }, indent=2))

    def stop_all(self) -> None:
        self._stop.set()
        for h in self.agents.values():
            h.stop()
        logger.info("all agents stopped")

    def broadcast(self, message: str) -> None:
        for h in self.agents.values():
            h.say(message)

    def add_task(self, task: dict) -> str:
        tid = f"task-{uuid.uuid4().hex[:6]}"
        task["id"] = tid
        task.setdefault("status", "pending")
        task.setdefault("owner", None)
        task.setdefault("deadline", time.time() + 600)
        with self._task_lock:
            self._tasks.append(task)
        return tid

    def assign_task(self, username: str, task_id: str) -> bool:
        with self._task_lock:
            for t in self._tasks:
                if t["id"] == task_id:
                    if t["status"] != "pending":
                        return False
                    t["status"] = "claimed"
                    t["owner"] = username
                    break
            else:
                return False
        agent = self.agents.get(username)
        if agent:
            agent.whisper(username, f"DIRECTIVE {task_id}: {json.dumps(t)}")
        return True

    def release_task(self, task_id: str) -> None:
        with self._task_lock:
            for t in self._tasks:
                if t["id"] == task_id:
                    t["status"] = "pending"
                    t["owner"] = None
                    break

    def get_swarm_status(self) -> dict:
        return {
            "agents": {
                uname: h.fetch_status()
                for uname, h in self.agents.items()
            },
            "tasks": list(self._tasks),
            "reservations": len(self.ledger._table),
        }

    def _lattice_create_task(self, title: str, body: str = "") -> Optional[str]:
        """Stub: shells out to `lattice task create`.  Replace with API call when available."""
        try:
            # Assumes `lattice` CLI is on PATH and auth is ambient.
            r = subprocess.run(
                ["lattice", "task", "create", "--title", title, "--body", body],
                capture_output=True, text=True, timeout=15,
            )
            # naive parse: last word is the task ID
            return r.stdout.strip().split()[-1] if r.returncode == 0 else None
        except Exception as e:
            logger.warning("lattice create failed: %s", e)
            return None

    def _lattice_link(self, parent_id: str, child_id: str) -> None:
        try:
            subprocess.run(
                ["lattice", "task", "link", "--from", parent_id, "--to", child_id, "--type", "related_to"],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.warning("lattice link failed: %s", e)

    def lattice_register(self) -> None:
        """Create a Lattice parent task and link every agent task to it."""
        parent = self._lattice_create_task(
            f"altercraft-swarm:{self.run_dir.name}",
            f"Multi-agent run with {len(self.personas)} agents.\nDir: {self.run_dir}"
        )
        if not parent:
            return
        (self.run_dir / "lattice_parent.txt").write_text(parent)
        for h in self.agents.values():
            cid = self._lattice_create_task(f"altercraft:{h.persona}", f"API: {h.api_url}")
            if cid:
                self._lattice_link(parent, cid)

    def _director_tick(self) -> None:
        # 1. health-check all agents
        for uname, h in list(self.agents.items()):
            st = h.fetch_status()
            if st.get("error"):
                logger.warning("agent %s unreachable: %s", uname, st["error"])
        # 2. expire stale tasks
        now = time.time()
        with self._task_lock:
            for t in self._tasks:
                if t["status"] == "claimed" and t.get("deadline", now + 1) < now:
                    logger.info("task %s expired from %s", t["id"], t["owner"])
                    t["status"] = "pending"
                    t["owner"] = None
        # 3. (optional) auto-assign idle agents to pending tasks
        # TODO: hook in LLM director logic here

    def run_director_loop(self, tick_s: float = 10.0) -> None:
        deadline = time.monotonic() + self.duration_s
        logger.info("director loop starting; deadline in %.0fs", self.duration_s)
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._director_tick()
            self._stop.wait(tick_s)
        logger.info("director loop exiting")


# ────────────────────────────────────────────────────────────────────────────
# 4.  CLI
# ────────────────────────────────────────────────────────────────────────────

def _parse_duration(s: str) -> float:
    import re
    m = re.fullmatch(r"(\d+)\s*([smh]?)", s.strip().lower())
    if not m:
        raise ValueError(f"bad duration {s!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return {"s": 1, "m": 60, "h": 3600}[unit] * n


def main() -> int:
    p = argparse.ArgumentParser(description="Spawn N AlterCraft agents and coordinate them.")
    p.add_argument("personas", nargs="+", help="Persona names (profile dirs altercraft-<persona>)")
    p.add_argument("--duration", default="30m")
    p.add_argument("--model", default=None)
    p.add_argument("--mc-host", default="10.10.20.1")
    p.add_argument("--mc-port", type=int, default=25565)
    p.add_argument("--rcon-host", default="10.10.20.1")
    p.add_argument("--rcon-port", type=int, default=25575)
    p.add_argument("--rcon-pass-file", default="/tmp/rcon.pass")
    p.add_argument("--stagger", type=float, default=5.0)
    p.add_argument("--tick", type=float, default=10.0)
    p.add_argument("--lattice", action="store_true", help="Register tasks in Lattice")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    director = SwarmDirector(
        personas=args.personas,
        mc_host=args.mc_host,
        mc_port=args.mc_port,
        rcon_host=args.rcon_host,
        rcon_port=args.rcon_port,
        rcon_pass_file=args.rcon_pass_file,
        duration_s=_parse_duration(args.duration),
        model_override=args.model,
        stagger_s=args.stagger,
    )

    try:
        director.spawn_all()
    except Exception as e:
        logger.exception("spawn failed: %s", e)
        director.stop_all()
        return 2

    if args.lattice:
        try:
            director.lattice_register()
        except Exception as e:
            logger.warning("lattice registration failed: %s", e)

    try:
        director.run_director_loop(tick_s=args.tick)
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        director.stop_all()
        # Write final summary
        summary = director.get_swarm_status()
        (director.run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
