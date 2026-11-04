#!/usr/bin/env python3
"""Print a bounded audit evidence packet; never mutate the audited profile.

Provisioning can run this in an hourly no-agent job. An empty result needs no LLM
call. This preparation command does not itself execute or commit an audit.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.memory_audit_window import prepare_window
from hermes_constants import get_hermes_home


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session_id')
    parser.add_argument('--watermark', type=int, required=True)
    parser.add_argument('--idle-hours', type=float, default=6)
    parser.add_argument('--window-days', type=float, default=2)
    parser.add_argument('--max-messages', type=int, default=120)
    args = parser.parse_args()
    window = prepare_window(
        get_hermes_home() / 'state.db', args.session_id, args.watermark,
        now=time.time(), idle_hours=args.idle_hours, window_days=args.window_days,
        max_messages=args.max_messages,
    )
    print(json.dumps(asdict(window) if window else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
