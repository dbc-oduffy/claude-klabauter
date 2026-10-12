"""The ceremony budget is a one-directional ratchet on PROCESS TIME.

`ipc.CEREMONY_BUDGET_SECS` is the process-time bar every ceremony op is held to. Measured
today: a sync handler's thread CPU plus the CPU `run_git` charges for its git children
(Windows only). A coroutine handler is not measured; the rule binds it and no check
enforces it yet. An op over the bar is reported and
returns its result unchanged. The only wall-clock limit on a ceremony op is the
backstop, `ipc.DISPATCH_TIMEOUT_SECS`, which means "hung", never "slow"
(state/rulings/2026-10-12-apm-ceremony-budget-process-time.md).

This module fails on any edit that raises the bar above `PINNED_CEILING_SECS`, lets an
override row or the env knob widen a ceremony op past the backstop, or re-introduces a
wall-clock clamp at the bar. `PINNED_CEILING_SECS` is a second, independent copy of the
number: lifting the constant without lifting this one fails the suite, and lifting both
is a deliberate act with a PM ruling behind it (DR-348).

Negative spec -- what this module does NOT assert:
  - That any ceremony op COMPLETES within the budget. That is a latency property,
    measured by per-op KPI tests.
  - That a ceremony op cannot resolve BELOW the backstop; an operator dialling
    `COORDINATOR_DISPATCH_TIMEOUT_SECS` down for a fast-fail run is a supported use.
  - The CPU of an async handler or of children started outside `run_git`: the overrun
    report covers a sync handler's thread plus CPU charged through
    `ipc.charge_op_cpu_secs`, which only `run_git` calls.

Spec backlink: docs/decisions/DR-348-the-ceremony-budget-is-a-ratchet.md
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from coordinator_core import ipc
from coordinator_core.op_scopes import OP_KEY_SCOPE

PINNED_CEILING_SECS = 2.0
PINNED_BACKSTOP_SECS = 30.0

CEREMONY_OPS = sorted(op for op in OP_KEY_SCOPE if ipc.is_ceremony_method(op))


def test_budget_constant_is_at_or_below_the_pinned_ceiling():
    assert ipc.CEREMONY_BUDGET_SECS <= PINNED_CEILING_SECS, (
        f"ceremony budget raised to {ipc.CEREMONY_BUDGET_SECS}s, above the pinned "
        f"{PINNED_CEILING_SECS}s ceiling. The budget ratchets DOWN only. An op that "
        f"needs a wider cap is an op with a defect -- make it cheaper. Raising this "
        f"pair requires a PM ruling recorded in DR-348."
    )


def test_the_backstop_is_not_widened():
    assert ipc.DISPATCH_TIMEOUT_SECS <= PINNED_BACKSTOP_SECS


def test_the_keying_table_actually_holds_ceremony_ops():
    """Guards the guard: an empty CEREMONY_OPS would make the sweeps below vacuous."""
    assert CEREMONY_OPS


@pytest.mark.parametrize("method", CEREMONY_OPS)
def test_every_ceremony_op_resolves_to_the_backstop(method):
    assert ipc._timeout_for(method) == ipc.DISPATCH_TIMEOUT_SECS == PINNED_BACKSTOP_SECS


def test_an_unlisted_future_ceremony_op_resolves_to_the_backstop():
    assert ipc._timeout_for("ceremony.not_yet_written") == PINNED_BACKSTOP_SECS


def test_env_knob_cannot_widen_a_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "9999")
    assert ipc._timeout_for("ceremony.scoped_git_commit") == PINNED_BACKSTOP_SECS
    assert ipc._timeout_for("ceremony.wsc_tail") == PINNED_BACKSTOP_SECS


def test_env_knob_may_still_narrow_a_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.5")
    assert ipc._timeout_for("ceremony.wsc_tail") == pytest.approx(0.5)


def test_no_override_row_widens_a_ceremony_op_past_the_backstop():
    offenders = {
        op: secs
        for op, secs in ipc._OP_TIMEOUT_OVERRIDES.items()
        if ipc.is_ceremony_method(op) and secs > PINNED_BACKSTOP_SECS
    }
    assert not offenders, f"ceremony override rows exceed the backstop: {offenders}. Delete them."


def test_an_injected_widening_row_is_still_ignored(monkeypatch):
    monkeypatch.setitem(ipc._OP_TIMEOUT_OVERRIDES, "ceremony.scoped_git_commit", 150.0)
    assert ipc._timeout_for("ceremony.scoped_git_commit") == PINNED_BACKSTOP_SECS


def test_non_ceremony_ops_resolve_to_the_global_guard():
    assert ipc._timeout_for("ping") == ipc._resolve_dispatch_timeout_secs()


PINNED_CEREMONY_PACKAGE = "coordinator_core.ops.ceremony."


def _ceremony_package_ops():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    return sorted(
        op
        for op, module in OP_MODULE_MAP.items()
        if isinstance(module, str) and module.startswith(PINNED_CEREMONY_PACKAGE)
    )


def test_ops_implemented_in_the_ceremony_package_are_ceremony_ops():
    package_ops = _ceremony_package_ops()
    assert package_ops
    for op in package_ops:
        assert ipc.is_ceremony_method(op), f"{op} lives in the ceremony package but is not ceremony"
        assert ipc._timeout_for(op) <= PINNED_BACKSTOP_SECS


def test_ceremony_package_membership_survives_a_cold_interpreter():
    """The import-free signals (prefix or alias table) must cover every package op.

    `ipc._owning_module_is_ceremony` never imports, so it answers False in a fresh
    interpreter; an op resting on it alone is budgeted or not depending on what the
    process happened to import.
    """
    for op in _ceremony_package_ops():
        assert op.startswith("ceremony.") or op in ipc._CEREMONY_PACKAGE_ALIASES, (
            f"{op} lives in {PINNED_CEREMONY_PACKAGE} but is ceremony ONLY by the "
            f"warm-process backstop. Add it to _CEREMONY_PACKAGE_ALIASES or move it out."
        )


@pytest.mark.spawns_process
def test_regenerate_cache_is_a_ceremony_op_in_a_cold_interpreter():
    import subprocess
    import sys

    probe = (
        "import sys;from coordinator_core import ipc;"
        "assert 'coordinator_core.ops._registry_map' not in sys.modules;"
        "assert ipc.is_ceremony_method('orientation.regenerate_cache')"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    assert "orientation.regenerate_cache" in ipc._CEREMONY_PACKAGE_ALIASES


def test_a_renamed_ceremony_op_is_still_recognised_as_ceremony(monkeypatch):
    def _renamed_handler(params, ctx=None, repo_root=None):
        return {}

    _renamed_handler.__module__ = PINNED_CEREMONY_PACKAGE + "scoped_git_commit"
    monkeypatch.setitem(ipc._REGISTRY, "commit.scoped_git_commit", _renamed_handler)

    assert ipc.is_ceremony_method("commit.scoped_git_commit")
    assert ipc._timeout_for("commit.scoped_git_commit") == PINNED_BACKSTOP_SECS


def test_membership_is_a_union_not_an_intersection():
    assert ipc.is_ceremony_method("ceremony.init_anchor_injection_state")


def test_membership_resolution_never_imports_the_ops_package(monkeypatch):
    import importlib

    calls = []
    real_import_module = importlib.import_module

    def _tracking_import(name, *args, **kwargs):
        calls.append(name)
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _tracking_import)
    ipc.is_ceremony_method("some.unregistered.op")
    ipc.is_ceremony_method("ceremony.wsc_tail")
    ipc._timeout_for("some.unregistered.op")

    assert not calls, f"membership resolution imported modules: {calls}"


def test_env_knob_cannot_widen_a_non_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "9999")
    baseline = ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("ping") <= baseline
    assert ipc._timeout_for("no.such.op") <= baseline


def test_env_knob_may_still_narrow_a_non_ceremony_op(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.5")
    assert ipc._timeout_for("ping") == pytest.approx(0.5)


def test_the_bar_is_below_the_backstop():
    assert ipc.CEREMONY_BUDGET_SECS < ipc.DISPATCH_TIMEOUT_SECS


def test_dump_op_timeouts_projects_the_wall_deadline_and_the_bar():
    from coordinator_core.invoke.__main__ import _dump_op_timeouts

    payload = _dump_op_timeouts()
    assert payload["__ceremony_budget__"] <= PINNED_CEILING_SECS
    for method in CEREMONY_OPS:
        assert method in payload
        assert payload[method] == ipc._timeout_for(method)


# ---------------------------------------------------------------------------
# Dispatch behaviour: the bar is process time, reported not enforced
# ---------------------------------------------------------------------------

_PROBE_OP = "ceremony.budget_probe"


def _dispatch_probe(monkeypatch, handler, *, rows):
    monkeypatch.setitem(ipc._REGISTRY, _PROBE_OP, handler)
    monkeypatch.setitem(ipc._OP_KEY_SCOPE, _PROBE_OP, "none")
    monkeypatch.setattr(ipc, "record_op_process_time", lambda **kw: rows.append(kw))
    msg = {"jsonrpc": "2.0", "id": 1, "method": _PROBE_OP, "params": {}}
    return asyncio.run(ipc._dispatch_message_impl(msg))


def _overruns(rows):
    return [r for r in rows if r.get("source_path") == "ceremony_budget_overrun"]


@pytest.mark.slow
@pytest.mark.deliberate_wall_clock(reason="a 5s sleep must not be cut off by a process-time budget")
def test_a_low_cpu_ceremony_op_over_the_bar_in_wall_time_is_not_cut_off(monkeypatch):
    rows: list = []

    def _sleeper(params, ctx=None, repo_root=None):
        time.sleep(5.0)
        return {"ok": True}

    response = _dispatch_probe(monkeypatch, _sleeper, rows=rows)

    assert response["result"] == {"ok": True}
    assert not _overruns(rows)


@pytest.mark.slow
def test_a_cpu_bound_ceremony_op_past_the_bar_is_reported_and_returns(monkeypatch):
    rows: list = []

    def _burner(params, ctx=None, repo_root=None):
        end = time.thread_time() + ipc.CEREMONY_BUDGET_SECS + 0.3
        while time.thread_time() < end:
            pass
        return {"ok": True}

    response = _dispatch_probe(monkeypatch, _burner, rows=rows)

    assert response["result"] == {"ok": True}
    overruns = _overruns(rows)
    assert len(overruns) == 1
    assert overruns[0]["op"] == _PROBE_OP
    assert overruns[0]["process_ms"] > ipc.CEREMONY_BUDGET_SECS * 1000.0
    assert overruns[0]["measurement_scope"] == ipc.MEASUREMENT_SCOPE_THREAD_AND_CHILDREN


def test_child_cpu_charged_to_the_dispatch_counts_toward_the_bar(monkeypatch):
    rows: list = []

    def _spawner(params, ctx=None, repo_root=None):
        ipc.charge_op_cpu_secs(ipc.CEREMONY_BUDGET_SECS + 0.5)
        return {"ok": True}

    response = _dispatch_probe(monkeypatch, _spawner, rows=rows)

    assert response["result"] == {"ok": True}
    assert len(_overruns(rows)) == 1


def test_a_cheap_ceremony_op_records_no_overrun(monkeypatch):
    rows: list = []
    response = _dispatch_probe(monkeypatch, lambda p, ctx=None, repo_root=None: {"ok": 1}, rows=rows)
    assert response["result"] == {"ok": 1}
    assert not _overruns(rows)


def test_charge_outside_a_dispatch_is_a_noop():
    ipc.charge_op_cpu_secs(5.0)


_ONE_TICK_MS = 15.625
_GIT_OP = "ceremony.budget_probe_git"
_SLEEP_OP = "ceremony.budget_probe_sleep"


def _git_spawner(params, ctx=None, repo_root=None):
    from coordinator_core.git.run import run_git

    start = time.thread_time()
    for _ in range(20):
        assert run_git(["--version"]).ok
    return {"thread_ms": (time.thread_time() - start) * 1000.0}


def _sleeper(params, ctx=None, repo_root=None):
    time.sleep(0.5)
    return {"ok": True}


async def _dispatch_both(monkeypatch, rows):
    monkeypatch.setattr(ipc, "CEREMONY_BUDGET_SECS", 0.0)
    monkeypatch.setattr(ipc, "record_op_process_time", lambda **kw: rows.append(kw))
    for op, handler in ((_GIT_OP, _git_spawner), (_SLEEP_OP, _sleeper)):
        monkeypatch.setitem(ipc._REGISTRY, op, handler)
        monkeypatch.setitem(ipc._OP_KEY_SCOPE, op, "none")
    msgs = [
        {"jsonrpc": "2.0", "id": i, "method": op, "params": {}}
        for i, op in enumerate((_GIT_OP, _SLEEP_OP), start=1)
    ]
    return await asyncio.gather(*(ipc._dispatch_message_impl(m) for m in msgs))


@pytest.mark.spawns_process
@pytest.mark.skipif(sys.platform != "win32", reason="POSIX charges no child CPU")
def test_a_git_child_is_charged_to_its_op_and_not_to_a_concurrent_sibling(monkeypatch):
    rows: list = []
    git_resp, sleep_resp = asyncio.run(_dispatch_both(monkeypatch, rows))

    thread_ms = git_resp["result"]["thread_ms"]
    by_op = {r["op"]: r["process_ms"] for r in _overruns(rows)}
    assert by_op[_GIT_OP] - thread_ms > _ONE_TICK_MS
    assert by_op.get(_SLEEP_OP, 0.0) < _ONE_TICK_MS
    assert sleep_resp["result"] == {"ok": True}


@pytest.mark.spawns_process
def test_run_git_outside_a_dispatch_charges_nothing_and_still_returns_its_result():
    from coordinator_core.git.run import GitResult, run_git

    assert ipc._op_cpu_secs_var.get() is None
    result = run_git(["--version"])
    assert isinstance(result, GitResult)
    assert result.ok and result.stdout.startswith("git version")
    assert not result.timed_out


@pytest.mark.deliberate_wall_clock(reason="the hung envelope fires on a wall-clock backstop CPU time cannot see")
def test_a_hung_ceremony_op_returns_the_hung_envelope_at_the_backstop(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.2")
    rows: list = []

    def _hung(params, ctx=None, repo_root=None):
        time.sleep(0.8)
        return {"late": True}

    response = _dispatch_probe(monkeypatch, _hung, rows=rows)

    assert "result" not in response
    text = json.dumps(response["error"])
    assert "hung" in text
    assert "abandoned, not stopped" in text
