#!/usr/bin/env python3
"""test_sweep_boot.py — self-contained test suite for sweep-boot.py.

Port of: test-sweep-boot-wrapper.sh (example-doctrine-repo a920e2de, 2026-07-19). Retargets the bash oracle's
contract assertions onto the Python trampoline: integer-count stdout contract on every
path (success / op-level refusal / transport failure), exit-0-always ceremony contract,
WARN-to-stderr on non-success paths, params-json state_common_dir forwarding, and the
big-bang no-legacy-fallback invariant (_legacy_fn raises — no per-class bash sweep is
re-implemented). The bash oracle's State-1 legacy-routing tests (AC-a State 1) are NOT
reproduced — there is no legacy leg left to strangle to; _legacy_fn raising IS the
State-1 behavior now, exercised by test_legacy_fn_raises below and folded into the
generic transport-error path (test_route_mutation_runtime_error).

Runs bash-free: `python3 test_sweep_boot.py` (or via the coordinator test runner).
Exit 0 = all tests pass; non-zero = at least one failure.

Spec backlink: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C2 / AC7
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md § Pinned pattern, Wave 1b
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import json
import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(SCRIPT_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from cc_invoke import RouteMutationError, _resolve_claude_klabauter_root  # noqa: E402

_CLAUDE_KLABAUTER_ROOT = _resolve_claude_klabauter_root()
if _CLAUDE_KLABAUTER_ROOT not in sys.path:
    sys.path.insert(0, _CLAUDE_KLABAUTER_ROOT)

from coordinator_core.ops.ceremony.detached_spawn import housekeeping_failures_log_path  # noqa: E402
from coordinator_core.ops.ceremony.housekeeping_liveness import (  # noqa: E402
    ARCHIVE_SWEEPS,
    liveness_path,
)

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


def _load_sweep_boot():
    """Import sweep-boot.py as a fresh module object (hyphenated filename -> importlib)."""
    path = os.path.join(SCRIPT_DIR, "sweep-boot.py")
    spec = importlib.util.spec_from_file_location("sweep_boot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv, monkey_route_mutation=None):
    """Run mod.main(argv) with stdout/stderr captured; optionally monkeypatch route_mutation."""
    orig = mod.route_mutation
    if monkey_route_mutation is not None:
        mod.route_mutation = monkey_route_mutation
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv)
    finally:
        mod.route_mutation = orig
    return rc, out.getvalue(), err.getvalue()


_FULL_JSON_RESULT = {
    "exit_code": 0,
    "consumed_handoffs": {"archived": ["a", "b", "c"], "skipped": [], "failed": []},
    "plans": {"archived": ["p1"], "skipped": [], "failed": []},
    "shipped_handoffs": {"archived": [], "skipped": [], "failed": []},
    "memos": {"archived": ["m1", "m2"], "skipped": [], "failed": []},
}


# ===========================================================================
# [AC-a / State 2] Native OK -> integer sum across four buckets
# ===========================================================================
def test_native_ok_sums_four_buckets(tmp_path):
    mod = _load_sweep_boot()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        assert op == "session.boot_sweep"
        return dict(_FULL_JSON_RESULT)

    rc, out, _err = _run_main_capturing(mod, [str(tmp_path)], fake_route_mutation)
    count = int(out.strip())
    if rc == 0:
        _pass("native ok: exit 0")
    else:
        _fail("native ok: exit 0", f"got rc={rc}")
    if count == 6:
        _pass("native ok: count = 6 (3 consumed + 1 plan + 0 shipped + 2 memos)")
    else:
        _fail("native ok: count = 6", f"got {count}")


# ===========================================================================
# [AC-c] Integer-count stdout contract across varied bucket sizes
# ===========================================================================
def test_integer_count_contract(tmp_path):
    mod = _load_sweep_boot()
    cases = [
        ({"consumed_handoffs": {"archived": []}, "plans": {"archived": []}, "shipped_handoffs": {"archived": []}, "memos": {"archived": []}}, 0),
        ({"consumed_handoffs": {"archived": ["x"]}, "plans": {"archived": []}, "shipped_handoffs": {"archived": []}, "memos": {"archived": []}}, 1),
        ({"consumed_handoffs": {"archived": ["a", "b"]}, "plans": {"archived": ["c"]}, "shipped_handoffs": {"archived": ["d"]}, "memos": {"archived": ["e"]}}, 5),
    ]
    for result, expected in cases:

        def fake_route_mutation(op, params, repo_root, legacy_fn, _result=result):
            return dict(_result)

        _rc, out, _err = _run_main_capturing(mod, [str(tmp_path)], fake_route_mutation)
        got = int(out.strip())
        if got == expected:
            _pass(f"int-count: expected={expected}, got={got}")
        else:
            _fail(f"int-count: expected={expected}", f"got={got}")


# ===========================================================================
# [AC-d] RouteMutationError (op-level refusal) -> partial count + WARN + exit 0
# ===========================================================================
def test_route_mutation_error_partial(tmp_path):
    mod = _load_sweep_boot()
    partial = {
        "exit_code": 2,
        "consumed_handoffs": {"archived": ["x"], "skipped": [], "failed": ["y"]},
        "plans": {"archived": [], "skipped": [], "failed": []},
        "shipped_handoffs": {"archived": [], "skipped": [], "failed": []},
        "memos": {"archived": [], "skipped": [], "failed": []},
    }

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        raise RouteMutationError("op refused: exit_code=2", partial)

    rc, out, err = _run_main_capturing(mod, [str(tmp_path)], fake_route_mutation)
    count = int(out.strip())
    if rc == 0:
        _pass("route_mutation error: exit 0 (best-effort ceremony)")
    else:
        _fail("route_mutation error: exit 0", f"got rc={rc}")
    if count == 1:
        _pass("route_mutation error: partial count printed (1)")
    else:
        _fail("route_mutation error: partial count == 1", f"got {count}")
    if "WARN" in err:
        _pass("route_mutation error: WARN on stderr")
    else:
        _fail("route_mutation error: WARN on stderr", f"stderr was: {err!r}")


# ===========================================================================
# [AC-a / State 3] Generic RuntimeError (transport failure) -> print 0, exit 0
# ===========================================================================
def test_route_mutation_runtime_error(tmp_path):
    mod = _load_sweep_boot()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure")

    rc, out, err = _run_main_capturing(mod, [str(tmp_path)], fake_route_mutation)
    count_str = out.strip()
    if rc == 0:
        _pass("transport error: exit 0 (log-and-continue)")
    else:
        _fail("transport error: exit 0", f"got rc={rc}")
    if count_str == "0":
        _pass("transport error: stdout == 0")
    else:
        _fail("transport error: stdout == 0", f"got '{count_str}'")
    if "WARN" in err:
        _pass("transport error: WARN on stderr")
    else:
        _fail("transport error: WARN on stderr", f"stderr was: {err!r}")


# ===========================================================================
# Big-bang invariant: _legacy_fn raises (no bash fallback reimplemented)
# ===========================================================================
def test_legacy_fn_raises():
    mod = _load_sweep_boot()
    try:
        mod._legacy_fn()
        _fail("_legacy_fn raises RuntimeError", "no exception raised")
    except RuntimeError as exc:
        if "no bash fallback" in str(exc) or "big-bang" in str(exc):
            _pass("_legacy_fn raises RuntimeError with no-fallback context")
        else:
            _fail("_legacy_fn raises RuntimeError with no-fallback context", f"message: {exc}")
    except Exception as exc:  # noqa: BLE001
        _fail("_legacy_fn raises RuntimeError (not another type)", f"got {type(exc).__name__}: {exc}")


# ===========================================================================
# state_common_dir forwarding — params-json contract (symmetric with repo_root)
# ===========================================================================
def test_state_common_dir_forwarded(tmp_path):
    mod = _load_sweep_boot()
    seen = {}
    repo_root = tmp_path / "fake-repo"
    repo_root.mkdir()
    state_common_dir = tmp_path / "state-common-dir"
    state_common_dir.mkdir()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        seen["params"] = params
        return dict(_FULL_JSON_RESULT)

    _run_main_capturing(mod, [str(repo_root), str(state_common_dir)], fake_route_mutation)
    if seen.get("params") == {"state_common_dir": str(state_common_dir)}:
        _pass("state_common_dir: forwarded verbatim into params")
    else:
        _fail("state_common_dir: forwarded verbatim into params", f"got {seen.get('params')!r}")

    seen.clear()
    _run_main_capturing(mod, [str(repo_root)], fake_route_mutation)
    if seen.get("params") == {}:
        _pass("state_common_dir absent: params == {} (op-side unified-state fallback)")
    else:
        _fail("state_common_dir absent: params == {}", f"got {seen.get('params')!r}")


def _read_failures_log(repo_root: str) -> str:
    path = housekeeping_failures_log_path(repo_root)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _read_liveness(repo_root: str) -> dict:
    path = liveness_path(repo_root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# C20 [fail-loud]: op refusal writes a CHILD FAILED housekeeping-failures record
# ===========================================================================
def test_refused_op_writes_failure_record():
    mod = _load_sweep_boot()
    partial = {
        "exit_code": 2,
        "consumed_handoffs": {"archived": [], "skipped": [], "failed": ["y"]},
        "plans": {"archived": [], "skipped": [], "failed": []},
        "shipped_handoffs": {"archived": [], "skipped": [], "failed": []},
        "memos": {"archived": [], "skipped": [], "failed": []},
    }

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        raise RouteMutationError("op refused: exit_code=2", partial)

    with tempfile.TemporaryDirectory() as repo_root:
        os.mkdir(os.path.join(repo_root, ".git"))
        rc, _out, _err = _run_main_capturing(mod, [repo_root], fake_route_mutation)
        log = _read_failures_log(repo_root)
        if rc == 0:
            _pass("refused op: exit 0 (fail-open preserved)")
        else:
            _fail("refused op: exit 0", f"got rc={rc}")
        if "CHILD FAILED" in log and "sweep-boot.py" in log:
            _pass("refused op: CHILD FAILED record written to housekeeping-failures log")
        else:
            _fail("refused op: CHILD FAILED record written", f"log contents: {log!r}")


# ===========================================================================
# C20 [fail-loud]: transport error writes a CHILD FAILED housekeeping-failures record
# ===========================================================================
def test_transport_error_writes_failure_record():
    mod = _load_sweep_boot()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure")

    with tempfile.TemporaryDirectory() as repo_root:
        os.mkdir(os.path.join(repo_root, ".git"))
        rc, _out, _err = _run_main_capturing(mod, [repo_root], fake_route_mutation)
        log = _read_failures_log(repo_root)
        if rc == 0:
            _pass("transport error: exit 0 (fail-open preserved)")
        else:
            _fail("transport error: exit 0", f"got rc={rc}")
        if "CHILD FAILED" in log and "sweep-boot.py" in log:
            _pass("transport error: CHILD FAILED record written to housekeeping-failures log")
        else:
            _fail("transport error: CHILD FAILED record written", f"log contents: {log!r}")


# ===========================================================================
# C20 [fail-loud]: a malformed (non-dict) op result also writes a failure record
# ===========================================================================
def test_non_object_result_writes_failure_record():
    mod = _load_sweep_boot()

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        return ["not", "a", "dict"]

    with tempfile.TemporaryDirectory() as repo_root:
        os.mkdir(os.path.join(repo_root, ".git"))
        rc, out, _err = _run_main_capturing(mod, [repo_root], fake_route_mutation)
        log = _read_failures_log(repo_root)
        if rc == 0 and out.strip() == "0":
            _pass("non-object result: exit 0, stdout '0' (fail-open preserved)")
        else:
            _fail("non-object result: exit 0, stdout '0'", f"rc={rc} out={out!r}")
        if "CHILD FAILED" in log:
            _pass("non-object result: CHILD FAILED record written to housekeeping-failures log")
        else:
            _fail("non-object result: CHILD FAILED record written", f"log contents: {log!r}")


# ===========================================================================
# C20 [fail-loud]: a genuine zero-item sweep writes NO failure record and DOES stamp
# the archive_sweeps liveness key (the negative case + the liveness-producer decision)
# ===========================================================================
def test_genuine_zero_item_sweep_writes_no_failure_and_stamps_liveness():
    mod = _load_sweep_boot()
    empty_result = {
        "consumed_handoffs": {"archived": []},
        "plans": {"archived": []},
        "shipped_handoffs": {"archived": []},
        "memos": {"archived": []},
    }

    def fake_route_mutation(op, params, repo_root, legacy_fn):
        return dict(empty_result)

    with tempfile.TemporaryDirectory() as repo_root:
        os.mkdir(os.path.join(repo_root, ".git"))
        rc, out, _err = _run_main_capturing(mod, [repo_root], fake_route_mutation)
        log = _read_failures_log(repo_root)
        liveness = _read_liveness(repo_root)
        if rc == 0 and out.strip() == "0":
            _pass("genuine zero-item: exit 0, stdout '0'")
        else:
            _fail("genuine zero-item: exit 0, stdout '0'", f"rc={rc} out={out!r}")
        if log == "":
            _pass("genuine zero-item: no housekeeping-failures record written")
        else:
            _fail("genuine zero-item: no housekeeping-failures record written", f"log contents: {log!r}")
        if ARCHIVE_SWEEPS in liveness:
            _pass("genuine zero-item: archive_sweeps liveness key stamped")
        else:
            _fail("genuine zero-item: archive_sweeps liveness key stamped", f"liveness contents: {liveness!r}")


# ===========================================================================
# No-stage/no-commit invariant (AC-b) — structural: source never shells to git add/commit
# ===========================================================================
def test_no_stage_commit_in_source():
    path = os.path.join(SCRIPT_DIR, "sweep-boot.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    forbidden = ("git add", "git commit", "\"add\", \"-A\"", "'add', '-A'")
    hits = [tok for tok in forbidden if tok in src]
    if not hits:
        _pass("AC-b: no git add/commit invocation in sweep-boot.py (op self-commits)")
    else:
        _fail("AC-b: no git add/commit invocation in sweep-boot.py", f"found: {hits}")


