"""bash_guard_commit_path_spawns: spawn attribution, leg tracking and the report shape through the real hook entry."""
from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks import bash_guard_commit_path_spawns as bench


def test_recorder_splits_git_from_other_spawns_and_names_the_leg() -> None:
    rec = bench._Recorder()
    rec.stack.append("claims")
    rec.record_spawn(["git", "status"])
    rec.record_spawn(["C:/Program Files/Git/cmd/git.exe", "diff"])
    rec.record_spawn([sys.executable, "-c", "pass"])
    rec.stack.pop()
    rec.record_spawn(["git", "log"])
    assert [(e["leg"], e["argv"][-1]) for e in rec.git] == [
        ("claims", "status"), ("claims", "diff"), (bench._NO_LEG, "log"),
    ]
    assert len(rec.other) == 1


def test_tracked_legs_names_the_active_leg_and_restores_the_binding() -> None:
    class _Mod:
        @staticmethod
        def leg(name):
            import contextlib
            return contextlib.nullcontext()

    original = _Mod.leg
    rec = bench._Recorder()
    with bench._tracked_legs(rec, [(_Mod, "leg")]):
        with _Mod.leg("outer"):
            with _Mod.leg("inner"):
                assert rec.current == "inner"
            assert rec.current == "outer"
    assert rec.current == bench._NO_LEG
    assert _Mod.leg is original
