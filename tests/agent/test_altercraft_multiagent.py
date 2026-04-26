import multiprocessing
import sys
from pathlib import Path

import pytest

from agent.altercraft_multiagent import SpatialLedger


def _reserve_worker(backing_file: str) -> None:
    """Run in a subprocess so a deadlock can be killed."""
    ledger = SpatialLedger(backing_file=Path(backing_file))
    ledger.reserve(0, 0, 0, "test")


def test_spatialledger_reserve_with_backing_file_does_not_deadlock(tmp_path: Path) -> None:
    """Regression for HRM-63: reserve() + _persist() used to deadlock because
    _persist re-acquired the non-reentrant Lock while reserve already held it."""
    backing_file = tmp_path / "ledger.jsonl"
    p = multiprocessing.Process(target=_reserve_worker, args=(str(backing_file),))
    p.start()
    p.join(timeout=1.0)
    if p.is_alive():
        p.terminate()
        p.join(timeout=1.0)
        pytest.fail("SpatialLedger.reserve() deadlocked with backing_file set")
    assert p.exitcode == 0, f"Subprocess exited with code {p.exitcode}"


def test_spatialledger_release_after_reserve_persists_state(tmp_path: Path) -> None:
    """Reserve, release, then verify the backing file reflects the empty state
    and a fresh SpatialLedger can be instantiated from the same path."""
    backing_file = tmp_path / "ledger.jsonl"

    ledger = SpatialLedger(backing_file=backing_file)
    assert ledger.reserve(0, 0, 0, "test", ttl=3600.0) is True
    assert (0, 0, 0) in ledger._table

    assert ledger.release(0, 0, 0, "test") is True
    assert (0, 0, 0) not in ledger._table

    # Re-instantiate from the same backing_file path; should not raise.
    ledger2 = SpatialLedger(backing_file=backing_file)
    assert ledger2._file == backing_file

    # Verify the file no longer contains the released block.
    lines = [ln for ln in backing_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 0
