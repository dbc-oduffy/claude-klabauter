#!/usr/bin/env python3
"""test_sweep_terminal_plans.py — self-contained test suite for sweep-terminal-plans.py.

Port of: coordinator/hooks/scripts/tests/test-sweep-terminal-plans.sh (example-doctrine-repo e34f2484,
2026-07-22) — P9/P10 cases only (the deleted native-path leg); P1-P8 continue to exercise
cs_sweep_terminal_plans, which is still live bash and out of scope here. Retargets the
two-call dry/act dispatch shape onto the Python trampoline: route() (not route_mutation())
against fleet.archive_completed_plans, true acted-count stdout contract, and WARN-not-raise
on an op-level exit_code==2 partial refusal.

Runs bash-free: `python3 test_sweep_terminal_plans.py` (or via the coordinator test runner).
Exit 0 = all tests pass; non-zero = at least one failure.

Spec backlink: docs/plans/2026-07-06-dr215-fleet-ops-ceremony-wiring.md § C3 / KD-2 / KD-3
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md § Wave F1 (facade collapse)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PASS = 0
FAIL = 0


def _pass(label: str) -> None:
    global PASS
    print(f"  PASS: {label}")
    PASS += 1


def _fail(label: str, detail: str = "") -> None:
    global FAIL
    print(f"  FAIL: {label}")
    if detail:
        print(f"    {detail}")
    FAIL += 1


def _load_module():
    """Import sweep-terminal-plans.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "sweep-terminal-plans.py")
    spec = importlib.util.spec_from_file_location("sweep_terminal_plans_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv=None, fake_route=None):
    """Run mod.main(argv or []) with stdout/stderr captured; optionally fake cc_invoke.route."""
    orig_route = mod.cc_invoke.route
    if fake_route is not None:
        mod.cc_invoke.route = fake_route
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv if argv is not None else [])
    finally:
        mod.cc_invoke.route = orig_route
    return rc, out.getvalue(), err.getvalue()


# ===========================================================================
# Two-call dry/act shape: Call 1 dry_run:true previews candidates via route();
# Call 2 dry_run:false acts on them, also via route() (never route_mutation()).
# ===========================================================================
def test_two_call_dry_then_act_via_route():
    mod = _load_module()
    calls = []

    def fake_route(op, params, repo_root, legacy_fn):
        calls.append(dict(params))
        assert op == "fleet.archive_completed_plans"
        if params.get("dry_run") is True:
            return {"exit_code": 0, "candidates": [{"id": "docs/plans/p1.md"}, {"id": "docs/plans/p2.md"}]}
        return {"exit_code": 0, "acted": [{"id": "docs/plans/p1.md"}, {"id": "docs/plans/p2.md"}]}

    rc, out, err = _run_main_capturing(mod, argv=["/tmp/fake-repo"], fake_route=fake_route)

    if rc == 0:
        _pass("two-call shape: exit 0")
    else:
        _fail("two-call shape: exit 0", f"got rc={rc}")

    if len(calls) == 2:
        _pass("two-call shape: exactly two route() dispatches")
    else:
        _fail("two-call shape: exactly two route() dispatches", f"got {len(calls)}: {calls!r}")

    if calls and calls[0].get("dry_run") is True and calls[0].get("candidate_ids") == []:
        _pass("two-call shape: Call 1 is dry_run=True with empty candidate_ids seed")
    else:
        _fail("two-call shape: Call 1 is dry_run=True with empty candidate_ids seed", f"calls: {calls!r}")

    if len(calls) > 1 and calls[1].get("dry_run") is False and calls[1].get("candidate_ids") == [
        "docs/plans/p1.md",
        "docs/plans/p2.md",
    ]:
        _pass("two-call shape: Call 2 is dry_run=False with dry-run's candidate ids")
    else:
        _fail(
            "two-call shape: Call 2 is dry_run=False with dry-run's candidate ids",
            f"calls: {calls!r}",
        )

    if out.strip() == "2":
        _pass("two-call shape: true acted count (2) printed to stdout")
    else:
        _fail("two-call shape: true acted count (2) printed to stdout", f"stdout: {out!r}")

    if "WARN" in err:
        _fail("two-call shape: no WARN on full success", f"stderr: {err!r}")
    else:
        _pass("two-call shape: no WARN on full success")


# ===========================================================================
# Empty candidates from dry-run -> print 0, Call 2 (act) skipped entirely.
# ===========================================================================
def test_empty_candidates_skips_act_call():
    mod = _load_module()
    act_called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        if params.get("dry_run") is True:
            return {"exit_code": 0, "candidates": []}
        act_called["n"] += 1
        return {"exit_code": 0, "acted": []}

    rc, out, _err = _run_main_capturing(mod, argv=["/tmp/fake-repo"], fake_route=fake_route)

    if rc == 0:
        _pass("empty candidates: exit 0")
    else:
        _fail("empty candidates: exit 0", f"got rc={rc}")

    if act_called["n"] == 0:
        _pass("empty candidates: act call (Call 2) never dispatched")
    else:
        _fail("empty candidates: act call (Call 2) never dispatched", f"called {act_called['n']} times")

    if out.strip() == "0":
        _pass("empty candidates: '0' printed to stdout")
    else:
        _fail("empty candidates: '0' printed to stdout", f"stdout: {out!r}")


# ===========================================================================
# Op-level partial refusal on the ACT call (exit_code==2) -> WARNs (does not
# raise), still prints the true acted count from the partial result.
# ===========================================================================
def test_act_call_partial_exit_code_warns_not_raises():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        if params.get("dry_run") is True:
            return {"exit_code": 0, "candidates": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}
        return {"exit_code": 2, "acted": [{"id": "p1"}], "failed": [{"id": "p2"}, {"id": "p3"}]}

    rc, out, err = _run_main_capturing(mod, argv=["/tmp/fake-repo"], fake_route=fake_route)

    if rc == 0:
        _pass("act partial exit_code=2: exit 0 (WARN, not raise)")
    else:
        _fail("act partial exit_code=2: exit 0", f"got rc={rc}")

    if out.strip() == "1":
        _pass("act partial exit_code=2: true partial acted count (1) printed")
    else:
        _fail("act partial exit_code=2: true partial acted count (1) printed", f"stdout: {out!r}")

    if "WARN" in err and "partial" in err:
        _pass("act partial exit_code=2: WARN on stderr")
    else:
        _fail("act partial exit_code=2: WARN on stderr", f"stderr: {err!r}")


# ===========================================================================
# Dry-run call transport failure (route() raises) -> print 0, WARN on stderr,
# exit 0 (best-effort ceremony, log-and-continue).
# ===========================================================================
def test_dry_run_transport_failure_prints_zero():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated dry-run transport failure")

    rc, out, err = _run_main_capturing(mod, argv=["/tmp/fake-repo"], fake_route=fake_route)

    if rc == 0:
        _pass("dry-run transport failure: exit 0")
    else:
        _fail("dry-run transport failure: exit 0", f"got rc={rc}")

    if out.strip() == "0":
        _pass("dry-run transport failure: '0' printed")
    else:
        _fail("dry-run transport failure: '0' printed", f"stdout: {out!r}")

    if "WARN" not in err and "transport error" in err:
        _pass("dry-run transport failure: stderr mentions transport error")
    else:
        _fail("dry-run transport failure: stderr mentions transport error", f"stderr: {err!r}")


# ===========================================================================
# Act call transport failure (route() raises on Call 2) -> print 0, exit 0.
# ===========================================================================
def test_act_call_transport_failure_prints_zero():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        if params.get("dry_run") is True:
            return {"exit_code": 0, "candidates": [{"id": "p1"}]}
        raise RuntimeError("simulated act-call transport failure")

    rc, out, err = _run_main_capturing(mod, argv=["/tmp/fake-repo"], fake_route=fake_route)

    if rc == 0:
        _pass("act transport failure: exit 0")
    else:
        _fail("act transport failure: exit 0", f"got rc={rc}")

    if out.strip() == "0":
        _pass("act transport failure: '0' printed")
    else:
        _fail("act transport failure: '0' printed", f"stdout: {out!r}")

    if "act call failed" in err:
        _pass("act transport failure: stderr mentions act call failed")
    else:
        _fail("act transport failure: stderr mentions act call failed", f"stderr: {err!r}")
