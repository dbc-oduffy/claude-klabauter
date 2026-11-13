"""Tests for the atlas catch-up leg of coordinator_core.housekeeping.cycle.run.

Negative-spec: does not re-test catch_up's own mechanics (cartography/tests) -- only that the
cycle calls it, reports its result, and never lets it change exit_code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.cartography import atlas_catchup
from coordinator_core.housekeeping import cycle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (plans / "2026-01-01-live.md").write_text("---\nstatus: executing\n---\n\nbody\n", encoding="utf-8")
    _git(root, "add", "docs/plans/2026-01-01-live.md")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def test_cycle_result_carries_atlas_catchup(repo: Path) -> None:
    result = cycle.run(repo, 5)
    assert result["atlas_catchup_error"] is None
    assert set(result["atlas_catchup"]) == set(atlas_catchup.CatchUpResult.__dataclass_fields__)


def test_raising_catch_up_is_reported_and_exit_code_unchanged(repo: Path, monkeypatch) -> None:
    baseline = cycle.run(repo, 5)

    def boom(_repo):
        raise RuntimeError("catchup exploded")

    monkeypatch.setattr(atlas_catchup, "catch_up", boom)
    result = cycle.run(repo, 5)
    assert "catchup exploded" in result["atlas_catchup_error"]
    assert result["atlas_catchup"] is None
    for key in ("closed", "close_error", "conflicts", "archived", "failed"):
        assert result[key] == baseline[key]
