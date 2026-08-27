# test_coordinator_gate_dispatcher.py — verifies coordinator-gate.py (the
# C10 fan-in dispatcher for the check-*/verify-*/assert- family) and its
# additions to coordinator/bin/lib/entry_point_shim.py.
#
# Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C10
# What this pins: (1) the dispatcher batches MULTIPLE subcommands into ONE
# process — the whole point of C10 per C7's 7.17x measurement, now applied
# to the 60-entry-point family the plan's § Problem opening figure names —
# and (2) no subprocess is ever spawned by the in-process shim path (the
# REJECTED shape from C7, -0.5123, was exactly a subprocess-spawning
# forwarder) for the converted (GATE_ENGINE_ENTRIES) subset.
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
    path = _BIN_DIR / "coordinator-gate.py"
    spec = importlib.util.spec_from_file_location("_test_coordinator_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rung_inventory_matches_sixty_stems():
    assert len(entry_point_shim.GATE_TARGETS) == 60
    for name in entry_point_shim.GATE_TARGETS:
        assert (_BIN_DIR / f"{name}.py").exists(), f"{name}.py missing"
        assert (_BIN_DIR / f"{name}.cmd").exists(), f"{name}.cmd missing"


def test_gate_targets_partition_engine_vs_by_path():
    engine = set(entry_point_shim.GATE_ENGINE_ENTRIES)
    by_path = set(entry_point_shim.GATE_BY_PATH_TARGETS)
    assert engine | by_path == set(entry_point_shim.GATE_TARGETS)
    assert engine & by_path == set()
    # Converted subset, per this dispatch's own read-in-full bar.
    assert engine == {
        "assert-no-dangling-plan-backlinks",
        "assert-plan-sizing-citation",
        "check-em-environment",
        "check-posix-exec-assumptions",
        "check-pcli-drift-gate",
    }


def test_unknown_gate_target_raises():
    with pytest.raises(entry_point_shim.UnknownTargetError):
        entry_point_shim.run_gate_target("not-a-real-target", [])


def test_run_gate_target_engine_entry_no_subprocess_spawned(monkeypatch):
    def _forbidden(*a, **kw):
        raise AssertionError("run_gate_target must not spawn a subprocess for an ENGINE_ENTRIES target")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(os, "spawnv", _forbidden, raising=False)

    rc = entry_point_shim.run_gate_target("check-em-environment", [])
    assert rc == 0


def test_run_gate_target_by_path_catches_internal_sys_exit():
    # assert-cwd.py's own main(argv) RETURNS an int (does not sys.exit
    # internally) -- exercise the plain-return probe on a BY_PATH target.
    rc = entry_point_shim.run_gate_target("assert-cwd", [])
    assert rc == 2  # usage error: no argv


def test_dispatcher_batches_multiple_subcommands_in_one_process(monkeypatch):
    dispatcher = _load_dispatcher_module()

    calls = []
    pid = os.getpid()

    def _fake_run_gate_target(name, args):
        calls.append((name, args, os.getpid()))
        return 0

    monkeypatch.setattr(dispatcher, "run_gate_target", _fake_run_gate_target)

    def _forbidden(*a, **kw):
        raise AssertionError("dispatcher must not spawn a subprocess for batching")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    rc = dispatcher.main(["check-em-environment", "--", "assert-cwd", "check-rag-state"])

    assert rc == 0
    assert [c[0] for c in calls] == ["check-em-environment", "assert-cwd", "check-rag-state"]
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

    def _fake_run_gate_target(name, args):
        return {"check-em-environment": 0, "assert-cwd": 5, "check-rag-state": 9}[name]

    monkeypatch.setattr(dispatcher, "run_gate_target", _fake_run_gate_target)
    rc = dispatcher.main(["check-em-environment", "assert-cwd", "check-rag-state"])
    assert rc == 5
