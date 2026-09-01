# test_coordinator_assemble_dispatcher.py — verifies coordinator-assemble.py
# (the C8 fan-in dispatcher) and coordinator/bin/lib/entry_point_shim.py.
#
# Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C8
# What this pins: (1) the dispatcher batches MULTIPLE subcommands into ONE
# process — the whole point of C8 per C7's 7.17x measurement — and (2) no
# subprocess is ever spawned by the in-process shim path (the REJECTED
# shape from C7, -0.5123, was exactly a subprocess-spawning forwarder).
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"
_LIB_DIR = _BIN_DIR / "lib"

for _p in (str(_LIB_DIR), str(_BIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_point_shim  # noqa: E402


def _load_dispatcher_module():
    path = _BIN_DIR / "coordinator-assemble.py"
    spec = importlib.util.spec_from_file_location("_test_coordinator_assemble", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rung_inventory_matches_fourteen_stems():
    expected = {
        "backlog-grind-assemble",
        "baton-assemble",
        "consolidate-assemble",
        "merge-assemble",
        "orient-assemble",
        "pickup-assemble",
        "plan-assemble",
        "quick-wrap-assemble",
        "review-assemble",
        "sizing-assemble",
        "staff-session-assemble",
        "workday-complete-assemble",
        "workday-start-inbox-blitz-assemble",
        "workstream-complete-assemble",
    }
    assert set(entry_point_shim.ASSEMBLE_TARGETS) == expected
    assert len(entry_point_shim.ASSEMBLE_TARGETS) == 14


def test_each_target_py_cmd_present_ps1_asymmetric():
    ps1_missing = {"plan-assemble", "workday-complete-assemble", "workday-start-inbox-blitz-assemble"}
    ps1_present_count = 0
    for name in entry_point_shim.ASSEMBLE_TARGETS:
        assert (_BIN_DIR / f"{name}.py").exists(), f"{name}.py missing"
        assert (_BIN_DIR / f"{name}.cmd").exists(), f"{name}.cmd missing"
        has_ps1 = (_BIN_DIR / f"{name}.ps1").exists()
        assert has_ps1 == (name not in ps1_missing)
        if has_ps1:
            ps1_present_count += 1
    assert ps1_present_count == 11


def test_assemble_targets_partition_engine_vs_by_path():
    # Review: code-reviewer — mirrors GATE's
    # test_gate_targets_partition_engine_vs_by_path so a future edit that
    # drops a target from _ENGINE_ENTRIES without adding it to
    # BY_PATH_TARGETS (or vice versa) fails loud here instead of surfacing
    # as a bare KeyError at `_ENGINE_ENTRIES[name]` inside run_target.
    engine = set(entry_point_shim._ENGINE_ENTRIES)
    by_path = set(entry_point_shim.BY_PATH_TARGETS)
    assert engine | by_path == set(entry_point_shim.ASSEMBLE_TARGETS)
    assert engine & by_path == set()
    assert by_path == {"workday-start-inbox-blitz-assemble"}


def test_unknown_target_raises():
    with pytest.raises(entry_point_shim.UnknownTargetError):
        entry_point_shim.run_target("not-a-real-target", [])


def test_run_target_no_subprocess_spawned(monkeypatch):
    def _forbidden(*a, **kw):
        raise AssertionError("run_target must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(os, "spawnv", _forbidden, raising=False)

    rc = entry_point_shim.run_target("baton-assemble", [])
    assert isinstance(rc, int)


def test_dispatcher_batches_multiple_subcommands_in_one_process(monkeypatch):
    dispatcher = _load_dispatcher_module()

    calls = []
    pid = os.getpid()

    def _fake_run_target(name, args):
        calls.append((name, args, os.getpid()))
        return 0

    # Patch the SOURCE module, not the dispatcher. `main()` does
    # `from entry_point_shim import ... run_target` at call time (moved
    # there from module scope by c992b99f73), so the dispatcher module has
    # no `run_target` attribute to replace and setattr raised AttributeError.
    monkeypatch.setattr(entry_point_shim, "run_target", _fake_run_target)

    def _forbidden(*a, **kw):
        raise AssertionError("dispatcher must not spawn a subprocess for batching")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    rc = dispatcher.main(["baton-assemble", "--", "consolidate-assemble", "sizing-assemble"])

    assert rc == 0
    assert [c[0] for c in calls] == ["baton-assemble", "consolidate-assemble", "sizing-assemble"]
    assert all(c[2] == pid for c in calls), "every subcommand must run in the SAME process"


def test_dispatcher_unknown_subcommand_is_usage_error():
    dispatcher = _load_dispatcher_module()
    rc = dispatcher.main(["not-a-real-target"])
    assert rc == 2


def test_dispatcher_empty_argv_is_usage_error():
    dispatcher = _load_dispatcher_module()
    rc = dispatcher.main([])
    assert rc == 2


def test_dispatcher_first_nonzero_exit_wins(monkeypatch):
    dispatcher = _load_dispatcher_module()

    def _fake_run_target(name, args):
        return {"baton-assemble": 0, "consolidate-assemble": 5, "sizing-assemble": 9}[name]

    # Patch the SOURCE module, not the dispatcher. `main()` does
    # `from entry_point_shim import ... run_target` at call time (moved
    # there from module scope by c992b99f73), so the dispatcher module has
    # no `run_target` attribute to replace and setattr raised AttributeError.
    monkeypatch.setattr(entry_point_shim, "run_target", _fake_run_target)
    rc = dispatcher.main(["baton-assemble", "consolidate-assemble", "sizing-assemble"])
    assert rc == 5
