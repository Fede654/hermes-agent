#!/usr/bin/env python3
"""Internal entry point for the disposable knowledge audit reviewer."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.environments import activate_dependencies

activate_dependencies(Path(__file__).resolve().parents[1])

from agent.memory_audit_worker import main

if __name__ == '__main__':
    main(*sys.argv[1:])
