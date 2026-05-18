"""Tests for config consistency between research_tool and research_job_tool (HRM-102)."""
from __future__ import annotations

import pytest

from tools.research_job_tool import research_job


def test_research_job_accepts_acceptance_criterion():
    """research_job must accept acceptance_criterion without error."""
    # Only verify the function signature accepts the param
    import inspect
    sig = inspect.signature(research_job)
    assert "acceptance_criterion" in sig.parameters


def test_research_job_accepts_timeout_sec():
    """research_job must accept timeout_sec without error."""
    import inspect
    sig = inspect.signature(research_job)
    assert "timeout_sec" in sig.parameters
