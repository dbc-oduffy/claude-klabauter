"""test_reap_sessions.py — self-contained test suite for reap-sessions.py.

Native-Python successor to the retired coordinator/bin/tests/test-reap-sessions-wrapper.sh
and test-coordinator-reap-sessions.sh (example-doctrine-repo f703efad, 2026-07-21; de-bash-coordinator
campaign, Wave F1, facade collapse). Retargets the bash oracles' contract assertions onto the Python trampoline: session.reap
dispatch shape (params == {}, never `force`), the negative-spec no-stdout-on-success
invariant, and the best-effort exit-0-always ceremony even when the transport seam raises.

Runs bash-free: `python3 test_reap_sessions.py` (or via the coordinator test runner).
Exit 0 = all tests pass; non-zero = at least one failure.

Spec backlink: docs/plans/2026-07-06-session-init-op-absorption-repoint.md § C1
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
    """Import reap-sessions.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "reap-sessions.py")
    spec = importlib.util.spec_from_file_location("reap_sessions_under_test", path)
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
# Dispatch shape: session.reap with params == {}, via cc_invoke.route(),
# never `force` in the payload.
# ===========================================================================
def test_dispatches_session_reap_empty_params_no_force(tmp_path):
    mod = _load_module()
    seen = {}
    fake_repo_root = str(tmp_path)

    def fake_route(op, params, repo_root, legacy_fn):
        seen["op"] = op
        seen["params"] = params
        seen["repo_root"] = repo_root
        return {"exit_code": 0}

    rc, out, _err = _run_main_capturing(mod, argv=[fake_repo_root], fake_route=fake_route)

    if rc == 0:
        _pass("dispatch shape: exit 0")
    else:
        _fail("dispatch shape: exit 0", f"got rc={rc}")

    if seen.get("op") == "session.reap":
        _pass("dispatch shape: op == session.reap")
    else:
        _fail("dispatch shape: op == session.reap", f"got {seen.get('op')!r}")

    if seen.get("params") == {}:
        _pass("dispatch shape: params == {} exactly")
    else:
        _fail("dispatch shape: params == {} exactly", f"got {seen.get('params')!r}")

    if "force" not in seen.get("params", {}):
        _pass("dispatch shape: 'force' never passed")
    else:
        _fail("dispatch shape: 'force' never passed", f"params: {seen.get('params')!r}")

    if seen.get("repo_root") == fake_repo_root:
        _pass("dispatch shape: repo_root forwarded from argv")
    else:
        _fail("dispatch shape: repo_root forwarded from argv", f"got {seen.get('repo_root')!r}")


# ===========================================================================
# Negative-spec: no integer count printed to stdout on success (unlike
# sweep-terminal-plans.py) — session-init's reaper block does not consume one.
# ===========================================================================
def test_no_stdout_on_success(tmp_path):
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        return {"exit_code": 0}

    rc, out, err = _run_main_capturing(mod, argv=[str(tmp_path)], fake_route=fake_route)

    if rc == 0:
        _pass("no-stdout-on-success: exit 0")
    else:
        _fail("no-stdout-on-success: exit 0", f"got rc={rc}")

    if out == "":
        _pass("no-stdout-on-success: stdout is empty (negative-spec)")
    else:
        _fail("no-stdout-on-success: stdout is empty", f"got {out!r}")

    if err == "":
        _pass("no-stdout-on-success: stderr is empty (no WARN on success)")
    else:
        _fail("no-stdout-on-success: stderr is empty", f"got {err!r}")


# ===========================================================================
# Transport failure (route() raises RuntimeError) -> still exit 0, WARN to
# stderr, no stdout. Reaper must never block session start.
# ===========================================================================
def test_transport_failure_exits_zero_warns_no_stdout(tmp_path):
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated session.reap transport failure")

    rc, out, err = _run_main_capturing(mod, argv=[str(tmp_path)], fake_route=fake_route)

    if rc == 0:
        _pass("transport failure: exit 0 (best-effort, never blocks session start)")
    else:
        _fail("transport failure: exit 0", f"got rc={rc}")

    if out == "":
        _pass("transport failure: no stdout on failure path")
    else:
        _fail("transport failure: no stdout on failure path", f"got {out!r}")

    if "session.reap failed" in err and "best-effort" in err:
        _pass("transport failure: stderr mentions session.reap failed (best-effort)")
    else:
        _fail("transport failure: stderr mentions session.reap failed (best-effort)", f"got {err!r}")


# ===========================================================================
# repo_root resolution: cannot resolve git repo root -> exit 0, no route() call.
# ===========================================================================
def test_unresolvable_repo_root_exits_zero_no_dispatch():
    mod = _load_module()
    called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        called["n"] += 1
        return {"exit_code": 0}

    mod._resolve_repo_root = lambda argv: None

    rc, out, err = _run_main_capturing(mod, argv=[], fake_route=fake_route)

    if rc == 0:
        _pass("unresolvable repo_root: exit 0 (never blocks session start)")
    else:
        _fail("unresolvable repo_root: exit 0", f"got rc={rc}")

    if called["n"] == 0:
        _pass("unresolvable repo_root: route() never dispatched")
    else:
        _fail("unresolvable repo_root: route() never dispatched", f"called {called['n']} times")

    if "cannot resolve git repo root" in err:
        _pass("unresolvable repo_root: stderr explains the failure")
    else:
        _fail("unresolvable repo_root: stderr explains the failure", f"got {err!r}")


