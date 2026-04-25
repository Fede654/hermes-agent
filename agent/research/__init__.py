"""Hermes AutoResearch — Karpathy inner loop + Autogenesis AOOR for Hermes.

Public API re-exports for convenience. Internal callers should import from
the submodules directly to keep dependency graph explicit.

Pattern parallel: agent.research is a self-contained orchestration module
in the same shape as ``cron/`` and ``gateway/`` — a directory bundle of
related primitives, not a flat collection of agent.research_*.py files.
"""
from agent.research.supervisor import ResearchSupervisor, TaskSpec
from agent.research.runner import (
    DelegateSandboxResult,
    ExperimentHistory,
    ExperimentResult,
    ExperimentRunner,
    HermesExperimentConfig,
)
from agent.research.metrics import UniversalMetricParser
from agent.research.evolution import EvolutionStore, LessonEntry, LessonCategory

__all__ = [
    "ResearchSupervisor",
    "TaskSpec",
    "DelegateSandboxResult",
    "ExperimentHistory",
    "ExperimentResult",
    "ExperimentRunner",
    "HermesExperimentConfig",
    "UniversalMetricParser",
    "EvolutionStore",
    "LessonEntry",
    "LessonCategory",
]
