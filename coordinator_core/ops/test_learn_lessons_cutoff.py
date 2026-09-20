"""Characterization + parity tests for coordinator_core.ops.learn_lessons_cutoff —
the shared COMPLETE-sentinel cutoff oracle promoted out of
`central_run_due._find_cutoff` and `coordinator/bin/learn-lessons-age-sweep.py
:: derive_cutoff`.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C1
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
from pathlib import Path

import pytest

from coordinator_core.ops.learn_lessons_cutoff import (
    _claude_home,
    derive_cutoff,
    resolve_runs_dir,
)

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "coordinator" / "bin"


def _load_age_sweep_module():
    loader = importlib.machinery.SourceFileLoader(
        "learn_lessons_age_sweep", str(_BIN_DIR / "learn-lessons-age-sweep.py")
    )
    spec = importlib.util.spec_from_loader("learn_lessons_age_sweep", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _make_run(runs_dir: Path, date: str, *, complete: bool) -> Path:
    d = runs_dir / f"learn-lessons-{date}"
    d.mkdir(parents=True)
    if complete:
        (d / "COMPLETE").touch()
    return d


class TestResolveRunsDir:
    def test_uses_claude_home_slash_tasks(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/tmp/fake-home")
        monkeypatch.delenv("HOME", raising=False)
        assert resolve_runs_dir() == Path(os.path.join("/tmp/fake-home", ".claude", "tasks"))

    def test_claude_home_env_overrides_home_not_full_path(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/tmp/fake-home")
        assert _claude_home() == os.path.join("/tmp/fake-home", ".claude")


class TestDeriveCutoff:
    def test_two_completed_runs_later_wins(self, tmp_path):
        runs_dir = tmp_path / "tasks"
        _make_run(runs_dir, "2026-01-01", complete=True)
        _make_run(runs_dir, "2026-02-01", complete=True)
        assert derive_cutoff(runs_dir) == "2026-02-01"

    def test_completed_beside_later_sentinel_less_run_earlier_wins(self, tmp_path):
        runs_dir = tmp_path / "tasks"
        _make_run(runs_dir, "2026-01-01", complete=True)
        _make_run(runs_dir, "2026-02-01", complete=False)
        assert derive_cutoff(runs_dir) == "2026-01-01"

    def test_empty_tree_returns_none(self, tmp_path):
        runs_dir = tmp_path / "tasks"
        runs_dir.mkdir()
        assert derive_cutoff(runs_dir) is None

    def test_missing_runs_dir_returns_none_not_raise(self, tmp_path):
        runs_dir = tmp_path / "tasks-does-not-exist"
        assert derive_cutoff(runs_dir) is None

    def test_non_directory_entry_named_like_a_run_is_ignored(self, tmp_path):
        runs_dir = tmp_path / "tasks"
        runs_dir.mkdir()
        (runs_dir / "learn-lessons-2026-01-01").write_text("not a dir")
        assert derive_cutoff(runs_dir) is None

    def test_malformed_date_prefix_still_matches_and_excluded_only_by_sentinel(self, tmp_path):
        runs_dir = tmp_path / "tasks"
        _make_run(runs_dir, "2026-13-45", complete=True)
        assert derive_cutoff(runs_dir) == "2026-13-45"


class TestParityWithBinAgeSweep:
    """Both derivations (this module and the standalone bin script) must
    return the same verdict over one shared fixture tree — the bin script is
    NOT edited (its own docstring declares "no coordinator_core import"), so
    this test loads it by file path and cross-checks in-process."""

    @pytest.mark.parametrize(
        "build_tree",
        [
            "two_completed_later_wins",
            "completed_beside_sentinel_less_earlier_wins",
            "empty_tree",
            "non_directory_entry",
        ],
    )
    def test_parity(self, tmp_path, build_tree):
        runs_dir = tmp_path / "tasks"
        if build_tree == "two_completed_later_wins":
            _make_run(runs_dir, "2026-01-01", complete=True)
            _make_run(runs_dir, "2026-02-01", complete=True)
        elif build_tree == "completed_beside_sentinel_less_earlier_wins":
            _make_run(runs_dir, "2026-01-01", complete=True)
            _make_run(runs_dir, "2026-02-01", complete=False)
        elif build_tree == "empty_tree":
            runs_dir.mkdir()
        elif build_tree == "non_directory_entry":
            runs_dir.mkdir()
            (runs_dir / "learn-lessons-2026-01-01").write_text("not a dir")

        age_sweep_mod = _load_age_sweep_module()
        assert derive_cutoff(runs_dir) == age_sweep_mod.derive_cutoff(runs_dir)
