"""Distill autofire tick cost: process time and spawn count of `autofire.on_idle_tick`.

Three legs, all in-process, process time (`time.process_time`) and spawn count, never wall clock:
  - deciding tick: this repo (`--repo`), debt recomputed; a no-op launcher stands in for the
    real one and the claim file is redirected to a scratch directory, so nothing is fired and
    the repo is not written.
  - no-change tick: a synthetic below-threshold repo whose inputs did not change since the last
    decision (stat gate: stats only), interval gate reset each iteration.
  - interval-gated tick: a tick inside AUTOFIRE_INTERVAL_SECS (a dict lookup).
Lazy imports are paid once before measuring; the warm server holds them after its first tick.
Exit 0 always: this measures, it does not gate. AC4 bars: no-change < 5ms and 0 spawns,
deciding < 200ms.

Usage: python coordinator_core/benchmarks/distill_autofire_tick_cost.py [--repo PATH] [--iterations N]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.distill import autofire
from coordinator_core.distill.autofire_contract import AUTOFIRE_THRESHOLD
from coordinator_core.ops.workflow_fire import fire

NO_CHANGE_BAR_MS = 5.0
DECIDING_BAR_MS = 200.0
_LOG = "## Run r-bench\n- archive/specs/2026-01/spec-0.md -> DISTILLED, folded (run: r-bench)\n"


class _SpawnCounter:
    """Counts process creations (Popen, posix_spawn, fork, system) while active."""

    def __init__(self) -> None:
        self.count = 0
        self._undo: list[Callable[[], None]] = []

    def _wrap(self, owner: Any, name: str) -> None:
        original = getattr(owner, name, None)
        if original is None:
            return

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.count += 1
            return original(*args, **kwargs)

        setattr(owner, name, counted)
        self._undo.append(lambda: setattr(owner, name, original))

    def __enter__(self) -> "_SpawnCounter":
        for owner, name in ((subprocess, "run"), (subprocess, "Popen"), (os, "posix_spawn"), (os, "fork"), (os, "system")):
            self._wrap(owner, name)
        return self

    def __exit__(self, *exc: Any) -> None:
        for undo in reversed(self._undo):
            undo()


def _noop_launcher(repo_root: Path) -> dict:
    return {"fire_id": None}


def _tick(repo: Path, launcher: Optional[Callable] = None) -> bool:
    return autofire.on_idle_tick([repo], launcher=launcher or _noop_launcher)


def _reset() -> None:
    autofire._next_tick.clear()
    autofire._stamps.clear()
    autofire.last_decisions.clear()


def _measure(repo: Path, iterations: int, *, reset_gate: bool, reset_stamps: bool) -> dict[str, Any]:
    """Mean process ms and spawns per tick over `iterations`, with the gates reset as asked."""
    cpu = 0.0
    with _SpawnCounter() as spawns:
        for _ in range(iterations):
            if reset_gate:
                autofire._next_tick.clear()
            if reset_stamps:
                autofire._stamps.clear()
            before = time.process_time()
            _tick(repo)
            cpu += time.process_time() - before
    return {
        "iterations": iterations,
        "process_ms_per_tick": round(cpu * 1000 / iterations, 4),
        "spawns_total": spawns.count,
    }


def _synthetic_below_threshold(root: Path) -> Path:
    repo = root / "synthetic-repo"
    month = repo / "archive" / "specs" / "2026-01"
    month.mkdir(parents=True)
    for i in range(AUTOFIRE_THRESHOLD):
        (month / f"spec-{i}.md").write_text("x\n", encoding="utf-8")
    (repo / "state").mkdir()
    (repo / "state" / "distillation-log.md").write_text(_LOG, encoding="utf-8")
    return repo


def measure(repo: Path, iterations: int = 200) -> dict[str, Any]:
    original_claim = autofire.CLAIM_REL
    original_registry_dir = fire._registry_dir
    with tempfile.TemporaryDirectory(prefix="autofire-bench-") as scratch:
        scratch_dir = Path(scratch)
        # An absolute CLAIM_REL makes `repo / CLAIM_REL` resolve to the scratch claim, so the
        # measured repo is never written.
        autofire.CLAIM_REL = scratch_dir / "claim.json"
        try:
            synthetic = _synthetic_below_threshold(scratch_dir)

            _reset()
            _tick(repo)  # pay lazy imports once
            _reset()
            for claim in scratch_dir.glob("claim.json"):
                claim.unlink()
            deciding = _measure(repo, 1, reset_gate=True, reset_stamps=True)
            deciding["decision"] = autofire.last_decisions[repo.resolve()].reason
            deciding["debt"] = autofire.last_decisions[repo.resolve()].debt
            for claim in scratch_dir.glob("claim.json"):
                claim.unlink()

            # The synthetic repo is not a git repo, so its fire registry resolves to scratch.
            fire._registry_dir = lambda cwd=None: scratch_dir / "registry"
            _reset()
            _tick(synthetic)  # the decision that stamps the inputs
            no_change = _measure(synthetic, iterations, reset_gate=True, reset_stamps=False)
            no_change["decision"] = autofire.last_decisions[synthetic.resolve()].reason

            _reset()
            _tick(synthetic)
            gated = _measure(synthetic, iterations, reset_gate=False, reset_stamps=False)
        finally:
            fire._registry_dir = original_registry_dir
            autofire.CLAIM_REL = original_claim
            _reset()
    return {
        "repo": str(repo),
        "threshold": AUTOFIRE_THRESHOLD,
        "deciding_tick": deciding,
        "no_change_tick": no_change,
        "interval_gated_tick": gated,
        "under_bar": {
            "no_change_under_5ms_zero_spawns": no_change["process_ms_per_tick"] < NO_CHANGE_BAR_MS
            and no_change["spawns_total"] == 0,
            "deciding_under_200ms": deciding["process_ms_per_tick"] < DECIDING_BAR_MS,
        },
    }


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--iterations", type=int, default=200)
    ns = ap.parse_args(argv)
    print(json.dumps(measure(ns.repo.resolve(), ns.iterations), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
