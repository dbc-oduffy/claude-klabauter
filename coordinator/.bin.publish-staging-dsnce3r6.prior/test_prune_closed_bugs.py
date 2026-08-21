"""test_prune_closed_bugs.py — self-contained test suite for prune-closed-bugs.py.

Native-Python successor to the retired coordinator/bin/tests/test-*-prune-closed-bugs.sh
suite lineage (de-bash-coordinator campaign, Wave F1, facade collapse). Retargets the
review-flagged ACT-call fix onto a regression test: fleet.prune_closed_bugs' act response
is a DETERMINATE-PARTIAL shape (exit_code=2 with a populated acted[] for the succeeded
subset) — route_mutation() would raise on ANY non-zero exit_code before the caller can
read acted[], mischaracterizing a real partial success as "not archived (transport
error)" for every requested id. The fix dispatches the ACT call via cc_invoke.route()
(bare result, manual exit_code inspection) instead. test_act_partial_success_reports_true_count
is the regression net for exactly this bug — it would FAIL against the pre-fix
route_mutation()-based ACT call.

Also covers the two-call dry->act shape (dry-run selects candidates; act performs the
archive) and the dry-run-only early exit (Call 2 skipped when --dry-run is passed).

Runs bash-free: `python3 test_prune_closed_bugs.py` (or via the coordinator test runner).
Exit 0 = all tests pass; non-zero = at least one failure.

Spec backlink: DoE-claude:pln-wire-claude-klabauter-fleet-archive-prun-8fd552 § KD-4 / AC6
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
    """Import prune-closed-bugs.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "prune-closed-bugs.py")
    spec = importlib.util.spec_from_file_location("prune_closed_bugs_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv=None, fake_route_mutation=None, fake_route=None):
    """Run mod.main(argv or []) with stdout/stderr captured; optionally fake both seams."""
    orig_route_mutation = mod.route_mutation
    orig_route = mod.cc_invoke.route
    if fake_route_mutation is not None:
        mod.route_mutation = fake_route_mutation
    if fake_route is not None:
        mod.cc_invoke.route = fake_route
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv if argv is not None else [])
    finally:
        mod.route_mutation = orig_route_mutation
        mod.cc_invoke.route = orig_route
    return rc, out.getvalue(), err.getvalue()


# ===========================================================================
# Regression: DETERMINATE-PARTIAL ACT response (exit_code=2, populated acted[])
# must report the TRUE archived count via a WARN, not "not archived (transport
# error)" for the whole batch. Pins the route()-not-route_mutation() fix on the
# ACT call.
# ===========================================================================
def test_act_partial_success_reports_true_count():
    mod = _load_module()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        assert op == "fleet.prune_closed_bugs"
        assert params.get("dry_run") is True
        return {
            "candidates": [
                {"id": "bug-1"},
                {"id": "bug-2"},
                {"id": "bug-3"},
            ]
        }

    def fake_route(op, params, repo_root, legacy_fn):
        assert op == "fleet.prune_closed_bugs"
        assert params.get("dry_run") is False
        assert params.get("candidate_ids") == ["bug-1", "bug-2", "bug-3"]
        # Partial success: 2 archived, 1 failed — exit_code=2 (DETERMINATE-PARTIAL).
        return {"exit_code": 2, "acted": ["bug-1", "bug-2"], "failed": ["bug-3"]}

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_route_mutation=fake_route_mutation, fake_route=fake_route
    )

    if rc == 0:
        _pass("partial ACT success: exit 0 (best-effort ceremony)")
    else:
        _fail("partial ACT success: exit 0", f"got rc={rc}")

    if "2 entr(ies) archived" in out:
        _pass("partial ACT success: TRUE count (2) printed")
    else:
        _fail("partial ACT success: TRUE count (2) printed", f"stdout: {out!r}")

    if "not archived (transport error)" in out:
        _fail(
            "partial ACT success: does NOT fall to the false transport-error message",
            f"stdout: {out!r}",
        )
    else:
        _pass("partial ACT success: does NOT fall to the false transport-error message")

    if "WARN" in err and "partial" in err:
        _pass("partial ACT success: WARN emitted on stderr")
    else:
        _fail("partial ACT success: WARN emitted on stderr", f"stderr: {err!r}")


# ===========================================================================
# Two-call dry->act shape: Call 1 dry_run:true selects candidates; Call 2
# dry_run:false performs the act. Full success (exit_code=0) prints the
# full count, no WARN.
# ===========================================================================
def test_two_call_dry_then_act_shape():
    mod = _load_module()
    calls = []

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        calls.append(("route_mutation", dict(params)))
        return {"candidates": [{"id": "bug-a"}, {"id": "bug-b"}]}

    def fake_route(op, params, repo_root, legacy_fn):
        calls.append(("route", dict(params)))
        return {"exit_code": 0, "acted": ["bug-a", "bug-b"]}

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_route_mutation=fake_route_mutation, fake_route=fake_route
    )

    if rc == 0:
        _pass("two-call shape: exit 0")
    else:
        _fail("two-call shape: exit 0", f"got rc={rc}")

    if len(calls) == 2:
        _pass("two-call shape: exactly two dispatch calls made")
    else:
        _fail("two-call shape: exactly two dispatch calls made", f"got {len(calls)}: {calls!r}")

    if calls and calls[0][0] == "route_mutation" and calls[0][1].get("dry_run") is True:
        _pass("two-call shape: Call 1 is route_mutation(dry_run=True)")
    else:
        _fail("two-call shape: Call 1 is route_mutation(dry_run=True)", f"calls: {calls!r}")

    if len(calls) > 1 and calls[1][0] == "route" and calls[1][1].get("dry_run") is False:
        _pass("two-call shape: Call 2 is route(dry_run=False)")
    else:
        _fail("two-call shape: Call 2 is route(dry_run=False)", f"calls: {calls!r}")

    if "2 entr(ies) archived" in out:
        _pass("two-call shape: full count (2) printed")
    else:
        _fail("two-call shape: full count (2) printed", f"stdout: {out!r}")

    if "WARN" in err:
        _fail("two-call shape: no WARN on full success", f"stderr: {err!r}")
    else:
        _pass("two-call shape: no WARN on full success")


# ===========================================================================
# --dry-run mode skips the ACT call entirely.
# ===========================================================================
def test_dry_run_mode_skips_act_call():
    mod = _load_module()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        assert params.get("dry_run") is True
        return {"candidates": [{"id": "bug-x"}]}

    act_called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        act_called["n"] += 1
        return {"exit_code": 0, "acted": []}

    rc, out, _err = _run_main_capturing(
        mod, argv=["--dry-run"], fake_route_mutation=fake_route_mutation, fake_route=fake_route
    )

    if rc == 0:
        _pass("dry-run mode: exit 0")
    else:
        _fail("dry-run mode: exit 0", f"got rc={rc}")

    if act_called["n"] == 0:
        _pass("dry-run mode: ACT call (route()) never invoked")
    else:
        _fail("dry-run mode: ACT call never invoked", f"called {act_called['n']} times")

    if "1 closed bug(s) selected for prune (--dry-run: no changes made)" in out:
        _pass("dry-run mode: preview message printed")
    else:
        _fail("dry-run mode: preview message printed", f"stdout: {out!r}")


# ===========================================================================
# Empty candidates from dry-run -> Call 2 skipped, "nothing to prune", exit 0.
# ===========================================================================
def test_empty_candidates_skips_act_call():
    mod = _load_module()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        return {"candidates": []}

    act_called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        act_called["n"] += 1
        return {"exit_code": 0, "acted": []}

    rc, out, _err = _run_main_capturing(
        mod, argv=[], fake_route_mutation=fake_route_mutation, fake_route=fake_route
    )

    if rc == 0:
        _pass("empty candidates: exit 0")
    else:
        _fail("empty candidates: exit 0", f"got rc={rc}")

    if act_called["n"] == 0:
        _pass("empty candidates: ACT call never invoked")
    else:
        _fail("empty candidates: ACT call never invoked", f"called {act_called['n']} times")

    if "nothing to prune" in out:
        _pass("empty candidates: 'nothing to prune' message printed")
    else:
        _fail("empty candidates: 'nothing to prune' message printed", f"stdout: {out!r}")


# ===========================================================================
# Dry-run call itself fails (transport error) -> WARN + skip, exit 0.
# ===========================================================================
def test_dry_run_call_transport_failure():
    mod = _load_module()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated dry-run transport failure")

    rc, out, err = _run_main_capturing(mod, argv=[], fake_route_mutation=fake_route_mutation)

    if rc == 0:
        _pass("dry-run transport failure: exit 0 (non-blocking)")
    else:
        _fail("dry-run transport failure: exit 0", f"got rc={rc}")

    if "skipping (non-blocking)" in out:
        _pass("dry-run transport failure: skip message printed")
    else:
        _fail("dry-run transport failure: skip message printed", f"stdout: {out!r}")

    if "WARN" in err:
        _pass("dry-run transport failure: WARN on stderr")
    else:
        _fail("dry-run transport failure: WARN on stderr", f"stderr: {err!r}")


