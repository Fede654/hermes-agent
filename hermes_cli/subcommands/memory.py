"""``hermes memory`` subcommand parser."""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_yes_flag


def build_memory_parser(subparsers, *, cmd_memory: Callable) -> None:
    """Attach the ``memory`` subcommand to ``subparsers``."""
    memory_parser = subparsers.add_parser(
        "memory", help="Configure external memory provider",
        description="Set up and manage external memory provider plugins.\n\n"
            "Bundled providers: retaindb, byterover.\n"
            "Catalog providers (e.g. honcho, hindsight, supermemory,\n"
            "mem0, openviking, holographic): hermes plugins install <name>.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active.")
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration")
    _setup_parser.add_argument(
        "provider", nargs="?", default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker")
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    _reset_parser = memory_sub.add_parser(
        "reset", help="Erase all built-in memory (MEMORY.md and USER.md)")
    add_yes_flag(_reset_parser)
    _reset_parser.add_argument(
        "--target", choices=["all", "memory", "user"], default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'")
    audit = memory_sub.add_parser("audit", help="Audit deployed knowledge locally, without messaging delivery")
    audit_sub = audit.add_subparsers(dest="audit_command")
    audit_sub.add_parser("tick", help="Check all routed sessions; review only idle sessions with new material")
    run = audit_sub.add_parser("run", help="Check and audit one idle session")
    run.add_argument("session_id")
    audit_sub.add_parser("status", help="List recent local audit runs")
    show = audit_sub.add_parser("show", help="Read one local audit report")
    show.add_argument("run_id")
    memory_parser.set_defaults(func=cmd_memory)
