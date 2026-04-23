"""altercraft_movement_loop — Karpathy loop over embodied movement primitives.

Launches a Hermes-Clio spawn, runs N mission trials ("walk from A to B and
report"), observes per-trial metrics (success, time, final distance to
goal), then asks an auxiliary LLM to propose a strategy tweak for the next
batch. Inject the tweak into the agent's system prompt, run another batch,
compare. Persistent output at ~/.hermes/research-jobs/altercraft-mvt-<ulid>/.

Usage:

    python -m agent.altercraft_movement_loop clio \\
        --start 15,71,41 --goal 40,71,60 \\
        --trials 3 --iterations 2 \\
        --trial-timeout 60

All trials run inside a single spawned bot session — between trials the
bot is teleported back to the start via RCON `tp <username> <coords>`.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

logger = logging.getLogger("altercraft_movement_loop")


# ────────────────────────────────────────────────────────────────────────────
# Helpers reused from altercraft_runner design
# ────────────────────────────────────────────────────────────────────────────

def _profile_dir(persona: str) -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "profiles" / f"altercraft-{persona}"


def _load_profile(persona: str) -> dict:
    cfg_path = _profile_dir(persona) / "config.yaml"
    with open(cfg_path, "r") as f:
        data = yaml.safe_load(f) or {}
    return data.get("profile", {})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(api_url: str, timeout_s: float = 45.0) -> None:
    start = time.monotonic()
    last = None
    while time.monotonic() - start < timeout_s:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception as e:
            last = e
        time.sleep(0.5)
    raise TimeoutError(f"/health not up: {last}")


def _parse_coords(s: str) -> tuple[float, float, float]:
    parts = [float(x) for x in s.replace(" ", "").split(",")]
    if len(parts) != 3:
        raise ValueError(f"bad coord {s!r}: expected x,y,z")
    return tuple(parts)  # type: ignore


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# ────────────────────────────────────────────────────────────────────────────
# RCON client (local, talks to Paper's RCON port over anyVPN)
# ────────────────────────────────────────────────────────────────────────────

class _Rcon:
    def __init__(self, host: str, port: int, password: str) -> None:
        self.host, self.port, self.password = host, port, password

    def _pkt(self, i: int, t: int, b: str) -> bytes:
        payload = struct.pack("<ii", i, t) + b.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    def _recv(self, sock: socket.socket) -> bytes:
        raw = b""
        while len(raw) < 4:
            raw += sock.recv(4 - len(raw))
        size = struct.unpack("<i", raw)[0]
        data = b""
        while len(data) < size:
            data += sock.recv(size - len(data))
        return data

    def send(self, cmd: str) -> str:
        with socket.create_connection((self.host, self.port), timeout=5) as s:
            s.sendall(self._pkt(1, 3, self.password))
            self._recv(s)
            s.sendall(self._pkt(2, 2, cmd))
            data = self._recv(s)
        return data[8:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")


# ────────────────────────────────────────────────────────────────────────────
# Bot server child
# ────────────────────────────────────────────────────────────────────────────

class _Bot:
    def __init__(self, *, mc_host: str, mc_port: int, mc_username: str,
                 api_port: int, run_dir: Path):
        self.mc_host, self.mc_port, self.mc_username = mc_host, mc_port, mc_username
        self.api_port, self.run_dir = api_port, run_dir
        self.api_url = f"http://127.0.0.1:{api_port}"
        self.proc: Optional[subprocess.Popen] = None
        self._log = None

    def start(self) -> None:
        altercraft_dir = Path.home() / ".hermes" / "altercraft"
        env = os.environ.copy()
        env.update({
            "MC_HOST": self.mc_host,
            "MC_PORT": str(self.mc_port),
            "MC_USERNAME": self.mc_username,
            "MC_AUTH": "offline",
            "API_PORT": str(self.api_port),
            "FAIR_PLAY": "true",
        })
        self._log = open(self.run_dir / "bot-server.log", "a", buffering=1)
        self.proc = subprocess.Popen(
            ["node", "server.js"],
            cwd=str(altercraft_dir),
            env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_health(self.api_url, timeout_s=45)
        logger.info("bot up (pid=%d) at %s", self.proc.pid, self.api_url)

    def stop(self, graceful: float = 10.0) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=graceful)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(timeout=5)
        if self._log:
            try: self._log.close()
            except Exception: pass


# ────────────────────────────────────────────────────────────────────────────
# Direct HTTP helpers (bypass agent for status polling + teleport)
# ────────────────────────────────────────────────────────────────────────────

def _get_status(api_url: str) -> dict:
    r = httpx.get(f"{api_url}/status", timeout=5)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body)


def _position_from_status(status: dict) -> Optional[tuple[float, float, float]]:
    pos = status.get("position") or status.get("pos")
    if isinstance(pos, dict):
        try:
            return float(pos["x"]), float(pos["y"]), float(pos["z"])
        except Exception:
            return None
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        return tuple(float(x) for x in pos)  # type: ignore
    return None


# ────────────────────────────────────────────────────────────────────────────
# AIAgent construction (per iteration, to re-inject updated strategy)
# ────────────────────────────────────────────────────────────────────────────

def _build_agent(profile: dict, strategy: str, session_id: str, iter_idx: int):
    hermes_src = Path(__file__).resolve().parents[1]
    if str(hermes_src) not in sys.path:
        sys.path.insert(0, str(hermes_src))
    from run_agent import AIAgent

    m = profile.get("model", {}) or {}
    model = m.get("default") or "kimi-k2.6"
    provider = m.get("provider")
    base_url = m.get("base_url")
    api_key = m.get("api_key")

    identity = profile.get("identity", {}) or {}
    mc_username = (profile.get("environment") or {}).get("mc_username")
    persona_desc = (identity.get("description") or "").strip()

    strategy_block = ""
    if strategy.strip():
        strategy_block = (
            "\n\nMovement strategy (learned from prior trials — follow it):\n"
            + strategy.strip() + "\n"
        )

    sys_prompt = (
        f"You are embodied in Minecraft as '{mc_username}' on AlterCraft (anyVPN-only). "
        "You perceive and act through the altercraft_* tools: altercraft_look, "
        "altercraft_goto (async pathfind), altercraft_status (position/health), "
        "altercraft_say/whisper (chat), altercraft_stop (cancel movement). "
        "When asked to reach coordinates, always call altercraft_goto, then "
        "poll altercraft_status to verify arrival. Be terse — this is a "
        "research setting. Report final position numerically.\n\n"
        f"Character:\n{persona_desc}"
        f"{strategy_block}"
    )

    agent = AIAgent(
        model=model,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        enabled_toolsets=["altercraft"],
        quiet_mode=True,
        platform="cli",
        session_id=f"altercraft-mvt:{session_id}:iter{iter_idx}",
        skip_context_files=True,
        skip_memory=True,
        ephemeral_system_prompt=sys_prompt,
        max_iterations=m.get("max_turns", 12),
        save_trajectories=True,
    )
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
    return agent


# ────────────────────────────────────────────────────────────────────────────
# Trial + iteration
# ────────────────────────────────────────────────────────────────────────────

def _run_trial(*, agent, rcon: _Rcon, api_url: str, username: str,
               start: tuple[float, float, float], goal: tuple[float, float, float],
               trial_idx: int, timeout_s: float) -> dict:
    logger.info("trial %d: reset to %s, goal %s", trial_idx, start, goal)
    # Teleport to start
    rcon.send(f"tp {username} {start[0]} {start[1]} {start[2]}")
    time.sleep(2.0)

    status_before = _get_status(api_url)
    pos_before = _position_from_status(status_before) or start

    prompt = (
        f"Mission trial #{trial_idx+1}.\n"
        f"Start: ({pos_before[0]:.1f}, {pos_before[1]:.1f}, {pos_before[2]:.1f})\n"
        f"Goal:  ({goal[0]:.1f}, {goal[1]:.1f}, {goal[2]:.1f})\n\n"
        "Walk to the goal. Call altercraft_goto, then poll altercraft_status "
        "until you arrive or it's clearly stuck. Report final position as "
        "`FINAL:(x,y,z)` on the last line."
    )

    t0 = time.monotonic()
    try:
        reply = agent.chat(prompt)
    except Exception as e:
        logger.exception("trial %d: agent.chat crashed: %s", trial_idx, e)
        reply = f"[agent-crash:{e}]"
    t1 = time.monotonic()

    # Verify final position via direct HTTP — don't trust the agent's word
    try:
        status_after = _get_status(api_url)
        pos_after = _position_from_status(status_after) or pos_before
    except Exception as e:
        logger.warning("trial %d: status fetch failed: %s", trial_idx, e)
        pos_after = pos_before

    dist = _distance(pos_after, goal)
    success = dist < 3.0

    return {
        "trial": trial_idx,
        "start": list(pos_before),
        "goal": list(goal),
        "final": list(pos_after),
        "distance_to_goal": round(dist, 3),
        "success": success,
        "duration_s": round(t1 - t0, 2),
        "timeout_s": timeout_s,
        "agent_reply": reply[:800] if isinstance(reply, str) else str(reply)[:800],
    }


def _aggregate(trials: list[dict]) -> dict:
    n = len(trials) or 1
    succ = sum(1 for t in trials if t["success"])
    mean_dist = sum(t["distance_to_goal"] for t in trials) / n
    mean_time = sum(t["duration_s"] for t in trials) / n
    return {
        "trials": n,
        "success_count": succ,
        "success_rate": round(succ / n, 3),
        "mean_distance_to_goal": round(mean_dist, 3),
        "mean_duration_s": round(mean_time, 2),
    }


def _propose_strategy(profile: dict, history: list[dict]) -> str:
    """Ask an auxiliary Hermes LLM for a concise strategy improvement."""
    from run_agent import AIAgent

    m = profile.get("model", {}) or {}
    agent = AIAgent(
        model=m.get("default") or "kimi-k2.6",
        provider=m.get("provider"),
        base_url=m.get("base_url"),
        api_key=m.get("api_key"),
        enabled_toolsets=[],  # pure text reasoning
        quiet_mode=True,
        platform="cli",
        session_id=f"altercraft-mvt-judge:{uuid.uuid4().hex[:8]}",
        skip_context_files=True,
        skip_memory=True,
        ephemeral_system_prompt=(
            "You are a research supervisor tuning a Minecraft agent's "
            "movement strategy. Return ONLY the refined strategy text — "
            "3-5 short bullets max, actionable, concrete. No preamble."
        ),
        max_iterations=3,
        save_trajectories=True,
    )
    agent._delegate_depth = 0
    agent.terminal_cwd = os.getcwd()
    agent.cwd = os.getcwd()
    agent._subdirectory_hints = None
    agent._delegate_spinner = None
    agent.tool_progress_callback = lambda *a, **k: None
    for attr in ("providers_allowed", "providers_ignored",
                 "providers_order", "provider_sort"):
        setattr(agent, attr, getattr(agent, attr, None))

    prev = history[-1]
    summary = "\n".join(
        f"  trial {t['trial']}: success={t['success']} "
        f"dist={t['distance_to_goal']} time={t['duration_s']}s "
        f"reply={t['agent_reply'][:120]!r}"
        for t in prev["trials"]
    )

    user = (
        f"Iteration {prev['iter']} results\n"
        f"Strategy used: {prev.get('strategy_used') or '(none — baseline)'}\n"
        f"Aggregates: {prev['aggregate']}\n"
        f"Per-trial:\n{summary}\n\n"
        "Propose a refined movement strategy (3-5 bullets max)."
    )
    return agent.chat(user).strip()


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("persona")
    p.add_argument("--start", required=True, help="x,y,z start coord (teleport point)")
    p.add_argument("--goal",  required=True, help="x,y,z goal coord")
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--iterations", type=int, default=2)
    p.add_argument("--trial-timeout", type=float, default=60.0)
    p.add_argument("--mc-host", default=None)
    p.add_argument("--mc-port", type=int, default=None)
    p.add_argument("--rcon-pass-file", default="/tmp/rcon.pass")
    p.add_argument("--rcon-port", type=int, default=25575)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    profile = _load_profile(args.persona)
    env_cfg = profile.get("environment") or {}
    if args.mc_host:
        mc_host, mc_port = args.mc_host, (args.mc_port or 25565)
    else:
        host, port = (env_cfg.get("mc_server") or "10.10.20.1:25565").rsplit(":", 1)
        mc_host, mc_port = host, (args.mc_port or int(port))
    mc_username = env_cfg.get("mc_username") or f"Hermes-{args.persona.capitalize()}"

    start = _parse_coords(args.start)
    goal  = _parse_coords(args.goal)

    rcon_password = Path(args.rcon_pass_file).read_text().strip()
    rcon = _Rcon(host=mc_host, port=args.rcon_port, password=rcon_password)

    run_id = f"{int(time.time()*1000):013x}-{uuid.uuid4().hex[:8]}"
    run_dir = Path.home() / ".hermes" / "research-jobs" / f"altercraft-mvt-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("run %s dir=%s", run_id, run_dir)

    api_port = _free_port()
    os.environ["MC_API_URL"] = f"http://127.0.0.1:{api_port}"

    bot = _Bot(mc_host=mc_host, mc_port=mc_port, mc_username=mc_username,
               api_port=api_port, run_dir=run_dir)

    def _sig(_a, _b):
        logger.info("signal received; stopping")
        bot.stop()
        sys.exit(130)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT,  _sig)

    try:
        bot.start()
    except Exception as e:
        logger.exception("bot start failed: %s", e)
        return 2

    spec = {
        "run_id": run_id,
        "persona": args.persona,
        "mc_server": f"{mc_host}:{mc_port}",
        "mc_username": mc_username,
        "start": list(start),
        "goal":  list(goal),
        "trials_per_iter": args.trials,
        "iterations": args.iterations,
        "trial_timeout_s": args.trial_timeout,
        "started_at": time.time(),
    }
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=2))

    history: list[dict] = []
    strategy = ""  # baseline

    try:
        for it in range(args.iterations):
            iter_dir = run_dir / f"iter-{it}"
            iter_dir.mkdir(exist_ok=True)
            (iter_dir / "strategy.md").write_text(strategy or "(baseline — no strategy addendum)\n")
            logger.info("=== iteration %d (strategy len=%d) ===", it, len(strategy))

            agent = _build_agent(profile, strategy, run_id, it)

            trials: list[dict] = []
            for t_idx in range(args.trials):
                t = _run_trial(agent=agent, rcon=rcon,
                               api_url=bot.api_url, username=mc_username,
                               start=start, goal=goal,
                               trial_idx=t_idx, timeout_s=args.trial_timeout)
                trials.append(t)
                (iter_dir / f"trial-{t_idx}.json").write_text(json.dumps(t, indent=2))
                logger.info("  trial %d: success=%s dist=%.2f dur=%.1fs",
                            t_idx, t["success"], t["distance_to_goal"], t["duration_s"])

            agg = _aggregate(trials)
            iter_record = {
                "iter": it,
                "strategy_used": strategy,
                "trials": trials,
                "aggregate": agg,
            }
            (iter_dir / "results.json").write_text(json.dumps(iter_record, indent=2))
            logger.info("  aggregate: %s", agg)
            history.append(iter_record)

            # After all iterations ran, stop
            if it < args.iterations - 1:
                logger.info("proposing strategy improvement...")
                try:
                    proposal = _propose_strategy(profile, history)
                except Exception as e:
                    logger.exception("judge failed: %s", e)
                    proposal = ""
                logger.info("proposed strategy (len=%d chars):\n%s",
                            len(proposal), proposal[:600])
                strategy = proposal.strip()

    finally:
        bot.stop()

    result = {
        "run_id": run_id,
        "spec": spec,
        "iterations": history,
        "ended_at": time.time(),
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    logger.info("done. run_dir=%s", run_dir)

    # Human-readable summary
    print("\n=== SUMMARY ===")
    for rec in history:
        agg = rec["aggregate"]
        print(f" iter {rec['iter']}: success_rate={agg['success_rate']} "
              f"mean_dist={agg['mean_distance_to_goal']} "
              f"mean_dur={agg['mean_duration_s']}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
