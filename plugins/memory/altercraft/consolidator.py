"""
Altercraft Consolidator — promotes raw perceive episodes to library summaries.

Run as: python -m plugins.memory.altercraft.consolidator --persona <name> [--dry-run]
Or called from a Hermes profile's cron/curator hook.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Regex to extract (x, y, z) from body strings like "mc_perceive at (100,64,-200)"
_POS_RE = re.compile(r"\((-?\d+),(-?\d+),(-?\d+)\)")

# Hostile mob type names (best-effort subset)
_HOSTILE = frozenset(
    [
        "zombie",
        "skeleton",
        "creeper",
        "spider",
        "witch",
        "enderman",
        "blaze",
        "ghast",
        "slime",
        "magma_cube",
        "wither",
        "phantom",
        "drowned",
        "husk",
        "pillager",
        "ravager",
        "vindicator",
        "evoker",
        "shulker",
    ]
)


# ─── DB helpers ───────────────────────────────────────────────────────────────


def get_unconsolidated_episodes(
    conn: sqlite3.Connection,
    persona_id: int,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return perceive episodes that have not yet been consolidated.

    The schema's ``consolidated`` INTEGER column (0/1) is the canonical flag.
    As a secondary guard, episodes whose ``detail`` starts with
    ``'[consolidated]'`` are also skipped (legacy/manual mark support).

    Returns list of dicts with keys: id, ts, kind, body, detail, persona_id.
    """
    rows = conn.execute(
        """
        SELECT id, ts, kind, body, detail, persona_id
        FROM episodes
        WHERE persona_id = ?
          AND kind = 'perceive'
          AND consolidated = 0
          AND (detail IS NULL OR detail NOT LIKE '[consolidated]%')
        ORDER BY ts ASC
        LIMIT ?
        """,
        (persona_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Position helpers ─────────────────────────────────────────────────────────


def _parse_pos(body: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Extract (x, y, z) ints from a body string. Returns None on failure."""
    if not body:
        return None
    m = _POS_RE.search(body)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _chunk_key(x: int, z: int) -> Tuple[int, int]:
    """Convert block coordinates to chunk coordinates (divide by 16, floor)."""
    # Python's // handles negative numbers correctly (floor division)
    return x // 16, z // 16


# ─── Tag extraction ───────────────────────────────────────────────────────────


def _extract_tags(episodes: List[Dict[str, Any]]) -> List[str]:
    """Best-effort: extract entity/mob names from detail JSON snippets."""
    tags: set = set()
    for ep in episodes:
        detail = ep.get("detail")
        if not detail:
            continue
        try:
            data = json.loads(detail)
            # Support {"entities": [...], "mobs": [...]} or flat list
            candidates: list = []
            if isinstance(data, dict):
                candidates.extend(data.get("entities", []))
                candidates.extend(data.get("mobs", []))
                candidates.extend(data.get("nearby_entities", []))
            elif isinstance(data, list):
                candidates = data
            for item in candidates:
                if isinstance(item, str):
                    tags.add(item.lower())
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("type") or item.get("kind")
                    if name:
                        tags.add(str(name).lower())
        except Exception:
            # Best-effort: ignore parse errors
            pass
    return sorted(tags)


def _hostile_in_tags(tags: List[str]) -> List[str]:
    return [t for t in tags if any(h in t for h in _HOSTILE)]


# ─── Last health extraction ───────────────────────────────────────────────────


def _last_health(episodes: List[Dict[str, Any]]) -> Optional[float]:
    """Extract health from the most recent episode in the group."""
    for ep in reversed(episodes):
        detail = ep.get("detail")
        if not detail:
            continue
        try:
            data = json.loads(detail)
            if isinstance(data, dict):
                h = data.get("health") or data.get("hp")
                if h is not None:
                    return float(h)
        except Exception:
            pass
    return None


# ─── Core consolidation ───────────────────────────────────────────────────────


def consolidate_batch(
    world_conn: sqlite3.Connection,
    library_conn: sqlite3.Connection,
    persona: str,
    persona_id: int,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Process one batch of unconsolidated perceive episodes.

    1. Fetch up to 50 unconsolidated episodes.
    2. Group by chunk boundary (floor(x/16), floor(z/16)).
    3. For each group with >= 3 episodes:
       a. Build a summary string.
       b. Extract entity tags.
       c. Write to library (unless dry_run).
       d. Mark source episodes consolidated (unless dry_run).
    4. Return {"processed": N, "consolidated": M, "skipped": K}.
    """
    from .library import upsert_library_episode  # local import to avoid circulars

    episodes = get_unconsolidated_episodes(world_conn, persona_id)
    processed = len(episodes)
    consolidated = 0
    skipped = 0

    # Group episodes by chunk key (episodes without parseable position → skipped)
    groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    no_pos: List[Dict[str, Any]] = []

    for ep in episodes:
        pos = _parse_pos(ep.get("body"))
        if pos is None:
            no_pos.append(ep)
            continue
        x, _y, z = pos
        ck = _chunk_key(x, z)
        groups.setdefault(ck, []).append(ep)

    skipped += len(no_pos)

    for (cx, cz), group in groups.items():
        if len(group) < 3:
            skipped += len(group)
            continue

        n = len(group)
        tags = _extract_tags(group)
        health = _last_health(group)
        hostile = _hostile_in_tags(tags)

        health_str = f", last health {health:.1f}" if health is not None else ""
        hostile_str = (
            f", hostile mobs: {', '.join(hostile)}" if hostile else ""
        )
        summary = (
            f"Visited chunk ({cx},{cz}): {n} observations{health_str}{hostile_str}"
        )

        episode_id = f"chunk_{cx}_{cz}_{persona}"

        if not dry_run:
            try:
                upsert_library_episode(
                    library_conn,
                    episode_id=episode_id,
                    world="altercraft",
                    persona=persona,
                    kind="chunk_summary",
                    summary=summary,
                    tags=tags or None,
                )
            except Exception:
                logger.warning(
                    "consolidator: upsert_library_episode failed for %s",
                    episode_id,
                    exc_info=True,
                )

            # Mark source episodes as consolidated
            ids = [ep["id"] for ep in group]
            placeholders = ",".join("?" for _ in ids)
            try:
                world_conn.execute(
                    f"UPDATE episodes SET consolidated = 1 WHERE id IN ({placeholders})",
                    ids,
                )
                world_conn.commit()
            except Exception:
                logger.warning(
                    "consolidator: failed to mark episodes consolidated for chunk (%s,%s)",
                    cx,
                    cz,
                    exc_info=True,
                )

        consolidated += n

    return {"processed": processed, "consolidated": consolidated, "skipped": skipped}


# ─── Entry point ──────────────────────────────────────────────────────────────


def run_consolidator(persona: str, dry_run: bool = False) -> Dict[str, int]:
    """Open both DBs, run one batch, close, return stats."""
    from .world import connect, get_or_create_persona
    from .library import open_library

    world_conn: Optional[sqlite3.Connection] = None
    library_conn: Optional[sqlite3.Connection] = None
    try:
        world_conn = connect(persona)
        library_conn = open_library(persona)
        persona_id = get_or_create_persona(world_conn, persona)
        stats = consolidate_batch(
            world_conn,
            library_conn,
            persona,
            persona_id,
            dry_run=dry_run,
        )
        return stats
    except Exception:
        logger.warning(
            "run_consolidator failed for persona=%s", persona, exc_info=True
        )
        return {"processed": 0, "consolidated": 0, "skipped": 0}
    finally:
        if world_conn is not None:
            try:
                world_conn.close()
            except Exception:
                pass
        if library_conn is not None:
            try:
                library_conn.close()
            except Exception:
                pass


# ─── Cron registration ────────────────────────────────────────────────────────


def register_consolidator_cron_job(
    persona: str,
    schedule: str = "every 5 minutes",
    deliver: str = "local",
    model: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Register (or replace) a Hermes cron job that runs the consolidator.

    The job is stored in ``~/.hermes/cron/jobs.json`` and is picked up by the
    gateway's background ticker (``cron.scheduler.tick()``) every 60 s.

    The prompt instructs the cron agent to call ``run_consolidator`` via the
    ``altercraft_memory`` toolset — no shell process is spawned; the agent
    executes the consolidation inline.

    Args:
        persona:  Persona name (e.g. "clio").
        schedule: Human-readable schedule string accepted by ``cron.jobs.parse_schedule``
                  (e.g. "every 5 minutes", "every 1 hour", "0 */4 * * *").
        deliver:  Delivery target for the job output (default: "local").
        model:    Optional model override.
        dry_run:  When True, remove any existing job with the same name and
                  return a preview dict without writing to disk.

    Returns:
        The created (or preview) job dict.
    """
    from cron.jobs import create_job, load_jobs, save_jobs  # lazy: heavy import

    job_name = f"altercraft-consolidator-{persona}"
    prompt = (
        f"Run the Altercraft episode consolidator for persona '{persona}'. "
        "Call run_consolidator once, report how many episodes were processed "
        "and consolidated, then stop. If there is nothing to consolidate, "
        "respond with [SILENT]."
    )

    # Remove any existing job with the same name to avoid duplicates.
    jobs = load_jobs()
    existing_ids = [j["id"] for j in jobs if j.get("name") == job_name]
    if existing_ids:
        jobs = [j for j in jobs if j["id"] not in existing_ids]
        if not dry_run:
            save_jobs(jobs)
        logger.info(
            "register_consolidator_cron_job: removed %d existing job(s) named '%s'",
            len(existing_ids),
            job_name,
        )

    job = create_job(
        prompt=prompt,
        schedule=schedule,
        name=job_name,
        deliver=deliver,
        model=model,
        enabled_toolsets=["memory", "altercraft_memory"],
    )

    if not dry_run:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
        logger.info(
            "register_consolidator_cron_job: registered job '%s' (id=%s, schedule=%s)",
            job_name,
            job["id"],
            schedule,
        )

    return job


# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import time

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Altercraft episode consolidator")
    parser.add_argument("--persona", required=True, help="Persona name (e.g. clio)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Read-only: do not write to library or mark episodes",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=False,
        help=(
            "Run continuously, sleeping ALTERCRAFT_CONSOLIDATOR_INTERVAL seconds "
            "between runs (default 300). Ctrl-C to stop."
        ),
    )
    parser.add_argument(
        "--register-cron",
        action="store_true",
        default=False,
        help=(
            "Register a Hermes cron job for this persona instead of running directly. "
            "Use --schedule to set the interval (default: 'every 5 minutes')."
        ),
    )
    parser.add_argument(
        "--schedule",
        default="every 5 minutes",
        help="Schedule string for --register-cron (default: 'every 5 minutes')",
    )
    args = parser.parse_args()

    if args.register_cron:
        job = register_consolidator_cron_job(
            persona=args.persona,
            schedule=args.schedule,
            dry_run=args.dry_run,
        )
        print(json.dumps(job, indent=2, default=str))
    elif args.loop:
        interval = int(
            __import__("os").environ.get("ALTERCRAFT_CONSOLIDATOR_INTERVAL", "300")
        )
        logger.info(
            "Consolidator loop started for persona='%s', interval=%ds (dry_run=%s)",
            args.persona,
            interval,
            args.dry_run,
        )
        try:
            while True:
                result = run_consolidator(args.persona, dry_run=args.dry_run)
                logger.info("consolidator tick: %s", result)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Consolidator loop stopped.")
    else:
        result = run_consolidator(args.persona, dry_run=args.dry_run)
        print(result)
