"""coordinator/bin/tests/test_entry_point_shim_warm_route.py -- C2's own
unit coverage for `entry_point_shim.py :: _merge_assemble_entry` and its
routing helpers (`_merge_assemble_dispatch`, `_merge_assemble_cold_call`,
`_merge_assemble_is_method_not_found`).

Spec backlink: docs/plans/2026-08-26-merge-assembles-entry-point-reaches-
the-warm-engine.md, chunk C2.

Scope: the ROUTING code this chunk owns, isolated from the real warm
transport (`cc_invoke.route` is monkeypatched per-scenario) and from the
real CLAUDE_KLABAUTER_ROOT registry (`entry_point_shim._import_engine_module` is
monkeypatched to a bare `importlib.import_module` against this repo's own
tree, so a test's outcome does not depend on which root this box's
machine-local registry happens to resolve today -- see
`docs/plans/.../C2.md`'s own EM addendum, obligation 2, for a case where
that resolution legitimately points elsewhere).

Negative-spec:
    - Does NOT exercise the real `coordinator_core.invoke` warm transport
      (UDS socket, JSON-RPC serialization) -- that is `cc_invoke`'s own
      test surface, not this chunk's.
    - Does NOT re-test `route()`'s State-1 seam-absent gate itself (AC6's
      own text: "not a branch this chunk writes"). Where a scenario needs
      route()'s real seam-absent behaviour, `cc_invoke.route` is stubbed
      to mirror that contract (call `legacy_fn()`), never re-derived.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _REPO_ROOT / "coordinator" / "bin" / "lib"

for _p in (str(_LIB_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_point_shim  # noqa: E402
import cc_invoke  # noqa: E402

_TRANSPORT_FAIL = entry_point_shim._TRANSPORT_FAIL
_USAGE_FAIL = entry_point_shim._USAGE_FAIL
_APPLY_EXIT_PARTIAL_MUTATION = entry_point_shim._APPLY_EXIT_PARTIAL_MUTATION


@pytest.fixture(autouse=True)
def _decouple_from_real_engine_root(monkeypatch):
    """Every test in this module reaches `coordinator_core.merge_assemble.cli`
    directly against THIS repo's own tree, never via the machine-local
    registry's resolved root -- that root can legitimately point at a
    sibling publish tree lacking C1's `cli.py` split (the exact condition
    diagnosed for obligation 2's regression), which would make these tests'
    outcomes depend on which box/registry state they happen to run under.
    """
    monkeypatch.setattr(
        entry_point_shim,
        "_import_engine_module",
        lambda dotted: importlib.import_module(dotted),
    )


@pytest.fixture()
def cli_mod():
    return importlib.import_module("coordinator_core.merge_assemble.cli")


def _stub_route(result=None, exc=None):
    """Builds a `cc_invoke.route`-shaped stub: returns `result` on success,
    or raises `exc` when given (never both)."""

    def _route(op, params, repo_root, legacy_fn, **kwargs):
        if exc is not None:
            raise exc
        return result

    return _route


# ---------------------------------------------------------------------------
# AC3 -- exit-code table, one row per code, each a real invocation of the
# entry (`entry_point_shim._merge_assemble_entry`).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv, route_result, route_exc, expected_code",
    [
        pytest.param(
            ["brief"],
            {"exit_code": 0, "decision_object": {"ok": True}},
            None,
            0,
            id="0-ok",
        ),
        pytest.param(
            ["apply"],
            {"exit_code": 1, "report": {"halted_at": "ship_verdict"}},
            None,
            1,
            id="1-halted-at-judgment",
        ),
        pytest.param(
            ["brief", "--bogus"],
            None,
            None,
            _USAGE_FAIL,
            id="2-usage-error",
        ),
        pytest.param(
            ["brief"],
            None,
            RuntimeError("transport exploded"),
            _TRANSPORT_FAIL,
            id="3-transport-failure",
        ),
        pytest.param(
            ["apply"],
            None,
            RuntimeError("transport exploded post-dispatch"),
            _APPLY_EXIT_PARTIAL_MUTATION,
            id="4-partial-mutation",
        ),
    ],
)
def test_exit_code_table(monkeypatch, argv, route_result, route_exc, expected_code):
    monkeypatch.setattr(cc_invoke, "route", _stub_route(result=route_result, exc=route_exc))
    code = entry_point_shim._merge_assemble_entry(list(argv))
    assert code == expected_code


# ---------------------------------------------------------------------------
# AC6 -- cold-fallback branch assertions, per verb. Assert the BRANCH taken
# (via a spy on `_merge_assemble_cold_call`), not just the return value.
# ---------------------------------------------------------------------------

def _spy_cold_call(monkeypatch, return_value):
    calls = []

    def _cold(op, params):
        calls.append((op, params))
        return return_value

    monkeypatch.setattr(entry_point_shim, "_merge_assemble_cold_call", _cold)
    return calls


@pytest.mark.parametrize("verb, argv", [("brief", ["brief"]), ("apply", ["apply"])])
def test_method_not_found_falls_back_cold_and_does_not_surface_as_exit_3(monkeypatch, verb, argv):
    cold_result = (
        {"exit_code": 0, "decision_object": {"cold": True}}
        if verb == "brief"
        else {"exit_code": 0, "report": {"cold": True}}
    )
    calls = _spy_cold_call(monkeypatch, cold_result)
    monkeypatch.setattr(
        cc_invoke,
        "route",
        _stub_route(exc=RuntimeError("cc_invoke: op failed, code=-32601 Method not found")),
    )
    code = entry_point_shim._merge_assemble_entry(list(argv))
    assert len(calls) == 1, "cold fallback branch was not taken on method-not-found"
    assert calls[0][0] == f"merge_assemble.{verb}"
    assert code == 0
    assert code != _TRANSPORT_FAIL


@pytest.mark.parametrize("verb, argv", [("brief", ["brief"]), ("apply", ["apply"])])
def test_seam_absent_routes_through_legacy_fn_not_a_reimplemented_check(monkeypatch, verb, argv):
    """AC6: seam-absent is `route()`'s own State-1 gate (it owns calling
    `legacy_fn()`), not a branch this chunk re-derives. Stub `route` to
    mirror that real contract and assert OUR `legacy_fn` (built from
    `_merge_assemble_cold_call`) is what gets reached."""
    cold_result = (
        {"exit_code": 0, "decision_object": {"cold": True}}
        if verb == "brief"
        else {"exit_code": 0, "report": {"cold": True}}
    )
    calls = _spy_cold_call(monkeypatch, cold_result)

    def _seam_absent_route(op, params, repo_root, legacy_fn, **kwargs):
        return legacy_fn()

    monkeypatch.setattr(cc_invoke, "route", _seam_absent_route)
    code = entry_point_shim._merge_assemble_entry(list(argv))
    assert len(calls) == 1
    assert code == 0


@pytest.mark.parametrize(
    "verb, argv, expected_code",
    [
        ("brief", ["brief"], _TRANSPORT_FAIL),
        ("apply", ["apply"], _APPLY_EXIT_PARTIAL_MUTATION),
    ],
)
def test_post_dispatch_transport_failure_does_not_fall_back(monkeypatch, verb, argv, expected_code):
    calls = _spy_cold_call(monkeypatch, {"exit_code": 0})
    monkeypatch.setattr(
        cc_invoke,
        "route",
        _stub_route(exc=RuntimeError("connection reset after send")),
    )
    code = entry_point_shim._merge_assemble_entry(list(argv))
    assert calls == [], "post-dispatch failure must NOT retry cold"
    assert code == expected_code


def test_apply_post_dispatch_failure_names_partial_mutation_state(monkeypatch, capsys):
    monkeypatch.setattr(entry_point_shim, "_merge_assemble_cold_call", lambda op, params: {})
    monkeypatch.setattr(
        cc_invoke, "route", _stub_route(exc=RuntimeError("connection reset after send"))
    )
    code = entry_point_shim._merge_assemble_entry(["apply"])
    assert code == _APPLY_EXIT_PARTIAL_MUTATION
    err = capsys.readouterr().err
    assert "APPLY_EXIT_PARTIAL_MUTATION" in err or "partial-mutation" in err


# ---------------------------------------------------------------------------
# `_merge_assemble_is_method_not_found` -- direct predicate test.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message, expected",
    [
        ("cc_invoke: op failed, code=-32601 Method not found", True),
        ("Method not found for op x", True),
        ("connection reset after send", False),
        ("timeout waiting for engine response", False),
    ],
)
def test_method_not_found_predicate(message, expected):
    assert entry_point_shim._merge_assemble_is_method_not_found(RuntimeError(message)) is expected


# ---------------------------------------------------------------------------
# AC4 -- refusal-shape table, exercised directly against
# `_merge_assemble_dispatch` (the discrimination-rule call site).
# ---------------------------------------------------------------------------

def _run_dispatch(monkeypatch, route_result, *, is_apply=False):
    monkeypatch.setattr(cc_invoke, "route", _stub_route(result=route_result))
    printed = []
    code = entry_point_shim._merge_assemble_dispatch(
        "merge_assemble.apply" if is_apply else "merge_assemble.brief",
        {},
        printed.append,
        "report" if is_apply else "decision_object",
        is_apply=is_apply,
    )
    return code, printed


def test_refusal_shape_a_error_with_no_exit_code_exits_nonzero(monkeypatch):
    code, printed = _run_dispatch(monkeypatch, {"error": "boom, no exit_code here"})
    assert code != 0
    assert printed == []


def test_refusal_shape_b_noncastable_exit_code_exits_nonzero(monkeypatch):
    code, printed = _run_dispatch(monkeypatch, {"exit_code": "banana"})
    assert code != 0
    assert printed == []


def test_refusal_shape_c_castable_nonzero_exit_code_exits_and_prints_report(monkeypatch):
    """The row that fails if `mutation_refusal_message`'s non-None return is
    naively treated as 'refuse': `exit_code=1` is halted-at-judgment, a
    normal ceremony outcome whose report MUST reach the caller."""
    report = {"halted_at": "ship_verdict", "reason": "judgment required"}
    code, printed = _run_dispatch(monkeypatch, {"exit_code": 1, "report": report}, is_apply=True)
    assert code == 1
    assert printed == [report]


# ---------------------------------------------------------------------------
# Observability -- path-served indicator (warm vs cold), present and correct
# on both branches.
# ---------------------------------------------------------------------------

def test_path_served_indicator_warm(monkeypatch, capsys):
    monkeypatch.setattr(
        cc_invoke, "route", _stub_route(result={"exit_code": 0, "decision_object": {}})
    )
    entry_point_shim._merge_assemble_entry(["brief"])
    err = capsys.readouterr().err
    assert "path=warm" in err


def test_path_served_indicator_cold(monkeypatch, capsys):
    _spy_cold_call(monkeypatch, {"exit_code": 0, "decision_object": {}})
    monkeypatch.setattr(
        cc_invoke,
        "route",
        _stub_route(exc=RuntimeError("code=-32601 Method not found")),
    )
    entry_point_shim._merge_assemble_entry(["brief"])
    err = capsys.readouterr().err
    assert "path=cold" in err


# ---------------------------------------------------------------------------
# Obligation 3 (EM addendum) -- runtime import-closure GATE for
# `coordinator_core.merge_assemble.cli`. AC10's own AST check on `cli.py`
# cannot see this: `import coordinator_core.merge_assemble.cli` runs
# `merge_assemble/__init__.py` first as ordinary Python package machinery,
# and a heavy module-scope import living THERE (not in `cli.py`) is
# invisible to an AST walk over `cli.py` alone. This test snapshots
# `sys.modules` in a FRESH interpreter (subprocess, not this test process,
# whose `sys.modules` is already polluted by every other test module's own
# imports) and fails if any forbidden heavy module was pulled in.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "contract.apply_base",
    "contract.decision_object",
    "merge_assemble.apply",
    "cli_dispatch",
    "composition_record",
)

_IMPORT_CLOSURE_PROBE = textwrap.dedent(
    """
    import json
    import sys
    import coordinator_core.merge_assemble.cli  # noqa: F401
    print(json.dumps(sorted(sys.modules.keys())))
    """
)


def _run_import_closure_probe() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_CLOSURE_PROBE],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        f"import-closure probe failed: rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runtime_import_closure_excludes_heavy_modules():
    modules = _run_import_closure_probe()
    offenders = [
        m for m in modules if any(bad in m for bad in _FORBIDDEN_IMPORT_SUBSTRINGS)
    ]
    assert offenders == [], (
        f"importing coordinator_core.merge_assemble.cli pulled in {offenders!r} -- "
        "AC1's warm-path import-cost promise is broken (this is the runtime "
        "gate AC10's AST check on cli.py alone cannot see, since these modules "
        "load via merge_assemble/__init__.py's own module-scope imports, not "
        "cli.py's)"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runtime_import_closure_gate_can_actually_fail(tmp_path):
    """Proves the gate above is not vacuous: temporarily restores a heavy
    module-scope import into `merge_assemble/__init__.py`, re-runs the SAME
    probe against that mutated file (via a throwaway copy of the package
    directory so the real tree is never touched), and asserts the probe's
    own `sys.modules` snapshot now contains the offending module -- i.e.
    the assertion above would have gone red had the mutation landed in the
    real tree.

    Mutate-and-check happens against a COPY, never the real
    `merge_assemble/__init__.py` (out of this chunk's `writes:` scope) --
    no revert-of-real-file step is needed because nothing real was ever
    changed.
    """
    import shutil

    real_pkg_dir = _REPO_ROOT / "coordinator_core" / "merge_assemble"
    real_core_dir = _REPO_ROOT / "coordinator_core"

    mutant_root = tmp_path / "mutant_root"
    mutant_core = mutant_root / "coordinator_core"
    shutil.copytree(real_core_dir, mutant_core, ignore=shutil.ignore_patterns("__pycache__"))

    init_path = mutant_core / "merge_assemble" / "__init__.py"
    original_text = init_path.read_text(encoding="utf-8")
    anchor = "from __future__ import annotations\n"
    assert anchor in original_text, "expected __init__.py to open with a __future__ import"
    mutated_text = original_text.replace(
        anchor,
        anchor
        + "from coordinator_core.contract.decision_object.judgment import "
        "build_judgment_point  # mutation-test-only\n",
        1,
    )
    init_path.write_text(mutated_text, encoding="utf-8")

    probe = textwrap.dedent(
        """
        import json
        import sys
        import coordinator_core.merge_assemble.cli  # noqa: F401
        print(json.dumps(sorted(sys.modules.keys())))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(mutant_root),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, f"mutant probe failed to even run: {proc.stderr}"
    modules = json.loads(proc.stdout)
    offenders = [m for m in modules if any(bad in m for bad in _FORBIDDEN_IMPORT_SUBSTRINGS)]
    assert offenders, (
        "mutation did not reproduce a forbidden import -- the gate's fail "
        "path cannot be trusted until this reproduces red"
    )
