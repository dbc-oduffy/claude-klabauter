"""test_schema_drift_gate.py — self-contained test suite for schema-drift-gate.

Exercises the CLI's exit-code contract across all four `schema.drift_gate`
verdicts (DRIFT / MATCH / INDETERMINATE / UNRESOLVED) plus the transport-
failure and malformed-result error paths, by faking `cc_invoke.route()` —
mirrors test_sweep_terminal_plans.py's fake-route harness shape.

Runs bash-free: `python3 test_schema_drift_gate.py` (or via the coordinator
test runner). Exit 0 = all tests pass; non-zero = at least one failure.

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from importlib.machinery import SourceFileLoader

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.dirname(SCRIPT_DIR)


def _pass(label: str) -> None:
    print(f"  PASS: {label}")


def _fail(label: str, detail: str = "") -> None:
    msg = label if not detail else f"{label}: {detail}"
    raise AssertionError(msg)


def _load_module():
    """Import coordinator/bin/schema-drift-gate.py as a fresh module object.

    spec_from_file_location can't infer a loader from a suffix-less path, so the
    SourceFileLoader is supplied explicitly (mirrors how extensionless polyglot
    entrypoints elsewhere in this tree are imported for testing).
    """
    path = os.path.join(BIN_DIR, "schema-drift-gate.py")
    loader = SourceFileLoader("schema_drift_gate_cli_under_test", path)
    spec = importlib.util.spec_from_file_location("schema_drift_gate_cli_under_test", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv=None, fake_route=None):
    """Run mod.main(argv or []) with stdout/stderr captured; optionally fake cc_invoke.route.

    schema.drift_gate is scope "none" (D4, docs/plans/2026-08-20-a-refusal-
    cannot-exit-zero.md § C16): the CLI no longer resolves (or spawns git
    for) a repo root before dispatching — there is nothing left to fake here."""
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
# DRIFT (ok=False) -> exit 1, message names the drifted schema + direction.
# ===========================================================================
def test_drift_exits_1_and_names_schema_and_direction():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        assert op == "schema.drift_gate"
        assert params == {}
        return {
            "ok": False,
            "status": "DRIFT",
            "drifted": [{"schema": "cockpit-contract.json", "direction": "we-behind"}],
            "message": "1 vendored schema(s) diverge from DoE HEAD: cockpit-contract.json [we-behind].",
        }

    rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

    if rc == 1:
        _pass("DRIFT: exit 1")
    else:
        _fail("DRIFT: exit 1", f"got rc={rc}")

    if "cockpit-contract.json" in out and "we-behind" in out:
        _pass("DRIFT: stdout names schema and direction")
    else:
        _fail("DRIFT: stdout names schema and direction", f"stdout: {out!r}")


# ===========================================================================
# MATCH (ok=True) -> exit 0, plain pass, no "unverified" hedge language.
# ===========================================================================
def test_match_exits_0_plain_pass():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        return {"ok": True, "status": "MATCH", "drifted": [], "message": None}

    rc, out, err = _run_main_capturing(mod, fake_route=fake_route)

    if rc == 0:
        _pass("MATCH: exit 0")
    else:
        _fail("MATCH: exit 0", f"got rc={rc}")

    if "PASS" in out and "unverified" not in out.lower() and "unverified" not in err.lower():
        _pass("MATCH: plain pass, no unverifiable hedge")
    else:
        _fail("MATCH: plain pass, no unverifiable hedge", f"stdout: {out!r} stderr: {err!r}")


# ===========================================================================
# INDETERMINATE (ok=True) -> exit 0, but explicitly says "could not verify".
# ===========================================================================
def test_indeterminate_exits_0_but_says_unverifiable():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        return {
            "ok": True,
            "status": "INDETERMINATE",
            "drifted": [],
            "message": "vendored-schema drift check could not run",
        }

    rc, out, err = _run_main_capturing(mod, fake_route=fake_route)

    if rc == 0:
        _pass("INDETERMINATE: exit 0")
    else:
        _fail("INDETERMINATE: exit 0", f"got rc={rc}")

    if "INDETERMINATE" in err and "could not run" in err:
        _pass("INDETERMINATE: stderr names the unverifiable status + reason")
    else:
        _fail("INDETERMINATE: stderr names the unverifiable status + reason", f"stderr: {err!r}")

    if "PASS" not in out:
        _pass("INDETERMINATE: no bare PASS on stdout that would read as verified-clean")
    else:
        _fail("INDETERMINATE: no bare PASS on stdout", f"stdout: {out!r}")


# ===========================================================================
# UNRESOLVED (ok=True) -> exit 0, explicitly says unresolved/no clone.
# ===========================================================================
def test_unresolved_exits_0_but_says_unverifiable():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        return {
            "ok": True,
            "status": "UNRESOLVED",
            "drifted": [],
            "message": "no DoE clone resolved; drift not determinable",
        }

    rc, out, err = _run_main_capturing(mod, fake_route=fake_route)

    if rc == 0:
        _pass("UNRESOLVED: exit 0")
    else:
        _fail("UNRESOLVED: exit 0", f"got rc={rc}")

    if "UNRESOLVED" in err and "no DoE clone" in err:
        _pass("UNRESOLVED: stderr names the unverifiable status + reason")
    else:
        _fail("UNRESOLVED: stderr names the unverifiable status + reason", f"stderr: {err!r}")


# ===========================================================================
# Transport failure (route() raises) -> exit 2, distinct from a DRIFT block.
# ===========================================================================
def test_transport_failure_exits_2():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure")

    rc, _out, err = _run_main_capturing(mod, fake_route=fake_route)

    if rc == 2:
        _pass("transport failure: exit 2")
    else:
        _fail("transport failure: exit 2", f"got rc={rc}")

    if "engine could not compute a verdict" in err:
        _pass("transport failure: stderr names the engine failure")
    else:
        _fail("transport failure: stderr names the engine failure", f"stderr: {err!r}")


# ===========================================================================
# Malformed result (not a dict) -> exit 2.
# ===========================================================================
def test_malformed_result_exits_2():
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        return "not-a-dict"

    rc, _out, err = _run_main_capturing(mod, fake_route=fake_route)

    if rc == 2:
        _pass("malformed result: exit 2")
    else:
        _fail("malformed result: exit 2", f"got rc={rc}")

    if "malformed result" in err:
        _pass("malformed result: stderr names the malformed-result condition")
    else:
        _fail("malformed result: stderr names the malformed-result condition", f"stderr: {err!r}")


