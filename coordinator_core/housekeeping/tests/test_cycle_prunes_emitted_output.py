"""
Tests for the emitted-output prune leg of coordinator_core.housekeeping.cycle.run.

Negative-spec: does not re-test `prune_emitted_output`'s own classification
(ops/fleet/tests/test_prune_emitted.py) -- only that the cycle calls it, reports
its result, and never lets it fail the cycle.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.housekeeping import cycle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

_OLD = time.time() - 7200.0


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )


def _script(plans: Path, name: str) -> Path:
    path = plans / name
    path.write_text("// emitted\n", encoding="utf-8")
    os.utime(path, (_OLD, _OLD))
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (plans / "2026-01-01-live.md").write_text(
        "---\nstatus: executing\n---\n\nbody\n", encoding="utf-8"
    )
    _git(root, "add", "docs/plans/2026-01-01-live.md")
    _git(root, "commit", "-q", "-m", "seed")
    _script(plans, "2026-01-01-live.workflow.mjs")
    _script(plans, "2026-01-02-gone.workflow.mjs")
    return root


def test_cycle_deletes_only_the_orphan(repo: Path) -> None:
    result = cycle.run(repo, 5)
    plans = repo / "docs" / "plans"
    assert result["emitted_prune_error"] is None
    assert result["emitted_pruned"] == ["docs/plans/2026-01-02-gone.workflow.mjs"]
    assert isinstance(result["emitted_retained_count"], int)
    assert not (plans / "2026-01-02-gone.workflow.mjs").exists()
    assert (plans / "2026-01-01-live.workflow.mjs").exists()


def test_raising_prune_is_reported_not_fatal(repo: Path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(cycle, "prune_emitted_output", boom)
    result = cycle.run(repo, 5)
    assert "prune exploded" in result["emitted_prune_error"]
    assert result["emitted_pruned"] == []
    assert (repo / "docs" / "plans" / "2026-01-02-gone.workflow.mjs").exists()


def test_a_raising_prune_leg_is_reported_not_fatal(
    repo: Path, monkeypatch
) -> None:
    # The leg reuses the cycle's own common_dir, so the prune call is its only failure point.
    def raising(*args, **kwargs):
        raise OSError("common dir gone")

    monkeypatch.setattr(cycle, "prune_emitted_output", raising)
    result = cycle.run(repo, 5)
    assert "common dir gone" in result["emitted_prune_error"]
    assert result["emitted_pruned"] == []


def test_prune_leg_process_time_under_500ms(repo: Path, monkeypatch) -> None:
    real = cycle.prune_emitted_output
    spent = {}

    def timed(*args, **kwargs):
        start = time.process_time()
        out = real(*args, **kwargs)
        spent["s"] = time.process_time() - start
        return out

    monkeypatch.setattr(cycle, "prune_emitted_output", timed)
    result = cycle.run(repo, 5)
    assert result["emitted_prune_error"] is None
    assert spent["s"] < 0.5
