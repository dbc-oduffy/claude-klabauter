"""repo_setup_budget_profile: the DR-344 process-time bar and per-step spawn naming on both roots."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import repo_setup_budget_profile as rb

pytestmark = pytest.mark.complete


def test_profile_fresh_and_rerun_are_under_the_bar_and_name_spawns_by_step(tmp_path: Path) -> None:
    rows = {r.axis: r for r in rb.profile(tmp_path)}
    assert set(rows) == {"fresh", "rerun"}
    for row in rows.values():
        assert 0.0 <= row.process_time_ms < rb.BAR_MS, (row.axis, row.process_time_ms)
        assert row.spawns == sum(len(v) for v in row.attribution["spawn_argv"].values())
        assert set(row.attribution["steps"]) >= {"hook", "git_config", "dirs"}
        assert row.attribution["steps"]["git_config"]["spawns"] == len(
            row.attribution["spawn_argv"].get("git_config", [])
        )
    assert rows["fresh"].attribution["created"] > 0
    assert rows["rerun"].attribution["created"] == 0
    assert not any("claude" in a.lower() for v in rows["fresh"].attribution["spawn_argv"].values() for a in v)


def test_profile_restores_popen_and_home_env(tmp_path: Path) -> None:
    before = (subprocess.Popen.__init__, os.environ.get("HOME"), os.environ.get("CLAUDE_CONFIG_DIR"))
    rb.profile(tmp_path)
    assert (subprocess.Popen.__init__, os.environ.get("HOME"), os.environ.get("CLAUDE_CONFIG_DIR")) == before


def test_over_bar_gate_fires_only_above_the_bar() -> None:
    under = rb.Row("fresh", rb.BAR_MS, 0)
    over = rb.Row("fresh", rb.BAR_MS + 0.1, 0)
    assert rb.over_bar([under]) is False
    assert rb.over_bar([under, over]) is True
