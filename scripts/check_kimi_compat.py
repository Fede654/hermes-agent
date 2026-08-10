#!/usr/bin/env python3
"""Check the local Kimi Code contract used by Hermes.

This is deliberately a no-network, no-token-output preflight.  Use it before
deploying the Kimi lane or when Kimi Code changes its local installation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.auth import kimi_coding_compatibility_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    report = kimi_coding_compatibility_report()
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        status = "OK" if report["ok"] else "FAILED"
        print(f"Kimi Code compatibility: {status}")
        for key, value in sorted(report["observed"].items()):
            print(f"  {key}: {value}")
        for warning in report["warnings"]:
            print(f"  warning: {warning}")
        for error in report["errors"]:
            print(f"  error: {error}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())