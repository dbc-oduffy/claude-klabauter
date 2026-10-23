"""Shared n-plan wave fixture for `roadmap.blitz_land` tests (re-land and budget cases).

Reuses test_blitz_land.py's record helpers. `build_wave` writes everything under `tmp_path`;
the gate-read stand-in is installed separately by `install_fixture_gate`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap.tests.test_blitz_land import _baton, _fixture_report, _plan, _repo


def install_fixture_gate(monkeypatch) -> None:
    """Swap the gate read and the shipped_in reachability check for tmp-corpus stand-ins."""
    monkeypatch.setattr(bl, "shipped_in_refusals", lambda root, shas: {})
    monkeypatch.setattr(
        bl.gate_v2, "compute_gate_report", lambda root, roadmap_id=None, **k: _fixture_report(root, roadmap_id)
    )


def build_wave(tmp_path: Path, n: int) -> Tuple[Path, Dict[str, Any]]:
    """Return `(worktree_root, wave_result)` for a fire of `n` ready batons.

    Each baton is linked by `deliverable_id` to a draft plan the prep gate passes.
    """
    root = _repo(tmp_path)
    ready = []
    for i in range(n):
        did = f"dlv-w-{i}"
        _baton(root, f"w-{i}", deliverable_id=did)
        plan = _plan(root, f"w-plan-{i}", "draft", deliverable_id=did)
        ready.append({"batonId": f"w-{i}", "planPath": plan})
    return root, {"waveIndex": 0, "ready": ready}
