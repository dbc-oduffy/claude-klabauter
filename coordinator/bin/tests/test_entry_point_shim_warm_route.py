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
    - Does NOT exercise `_merge_assemble_entry`'s own `except (RuntimeError,
      ImportError): return _merge_assemble_legacy_entry(argv)` branch (the
      seam-absent-because-`cli.py`-doesn't-exist path) -- this module's
      `_decouple_from_real_engine_root` fixture monkeypatches
      `_import_engine_module` unconditionally, so that branch cannot be
      reached from here. Deliberately left to C3's launcher/e2e suite,
      which exercises the real (non-monkeypatched) import path. (Review:
      coordinator-code-reviewer, Finding 3.)
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
# `_merge_assemble_checked_repo_root` -- DR-277 MISMATCH-warns-and-proceeds
# convention. (Review: coordinator-code-reviewer, Finding 2.)
# ---------------------------------------------------------------------------

def test_checked_repo_root_mismatch_warns_to_stderr_and_still_dispatches(monkeypatch, capsys):
    def _fake_resolve_checked_repo_root(explicit_root=None):
        return "root", {"verdict": "MISMATCH", "message": "MISMATCH: repo root drifted"}

    import repo_identity  # noqa: E402

    monkeypatch.setattr(repo_identity, "resolve_checked_repo_root", _fake_resolve_checked_repo_root)
    monkeypatch.setattr(
        cc_invoke, "route", _stub_route(result={"exit_code": 0, "decision_object": {}})
    )

    code = entry_point_shim._merge_assemble_entry(["brief"])

    err = capsys.readouterr().err
    assert "MISMATCH" in err
    assert code == 0


# ---------------------------------------------------------------------------
# `_APPLY_EXIT_PARTIAL_MUTATION` pin -- this module duplicates the value by
# design (comment at its definition site: avoiding a cold-path import of
# `apply_base`), so nothing else catches a drift between the two if
# `apply_base.APPLY_EXIT_PARTIAL_MUTATION` is ever changed.
# (Review: coordinator-code-reviewer, Finding 4.)
# ---------------------------------------------------------------------------

def test_apply_exit_partial_mutation_matches_apply_base_constant():
    from coordinator_core.contract import apply_base

    assert entry_point_shim._APPLY_EXIT_PARTIAL_MUTATION == apply_base.APPLY_EXIT_PARTIAL_MUTATION


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


def _probe_import_closure(engine_root: Path) -> list[str]:
    """Run `_IMPORT_CLOSURE_PROBE` in a FRESH interpreter rooted at `engine_root`
    and return the resulting `sys.modules` keys.

    Subprocess, never this process: the test interpreter's own `sys.modules` is
    already polluted by every other test module's imports, so an in-process
    snapshot would report modules this probe did not cause and could not
    distinguish a leak from a neighbour's import.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_CLOSURE_PROBE],
        capture_output=True,
        text=True,
        cwd=str(engine_root),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _forbidden_hits(modules: list[str]) -> list[str]:
    return [m for m in modules if any(s in m for s in _FORBIDDEN_IMPORT_SUBSTRINGS)]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runtime_import_closure_excludes_heavy_modules() -> None:
    """AC11: importing the leaf must not drag in the heavy graph AT RUNTIME.

    AC10's AST walk over `cli.py` structurally cannot discharge this: Python
    executes `merge_assemble/__init__.py` before the submodule, so a heavy
    module-scope import living THERE is invisible to a scan of `cli.py` alone.
    That was not hypothetical -- it was the real state of the tree until the
    three imports in `__init__.py` were made lazy.
    """
    hits = _forbidden_hits(_probe_import_closure(_REPO_ROOT))
    assert hits == [], (
        "importing coordinator_core.merge_assemble.cli pulled in forbidden heavy "
        f"modules: {hits!r}. A module-scope import was probably restored to "
        "coordinator_core/merge_assemble/__init__.py -- move it back inside its "
        "call site."
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runtime_import_closure_gate_can_actually_fail(tmp_path: Path) -> None:
    """The gate above must be shown to FAIL when the property is violated.

    Without this, a probe that silently stopped importing anything -- a renamed
    module, a swallowed error, a typo in the forbidden list -- would keep
    reporting green forever. Mutates a COPY in tmp_path; the real tree is never
    written to.
    """
    import shutil

    engine_copy = tmp_path / "engine"
    shutil.copytree(
        _REPO_ROOT / "coordinator_core",
        engine_copy / "coordinator_core",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    init_py = engine_copy / "coordinator_core" / "merge_assemble" / "__init__.py"
    lines = init_py.read_text(encoding="utf-8").splitlines(keepends=True)
    # AFTER the `from __future__` line, never before it: a __future__ import must
    # be the first statement in the file, so prepending raises SyntaxError and the
    # probe would fail for the wrong reason -- a red that proves nothing about
    # import closure, which is exactly the vacuity this test exists to rule out.
    insert_at = next(
        (i + 1 for i, line in enumerate(lines) if line.startswith("from __future__")),
        0,
    )
    lines.insert(
        insert_at,
        "from coordinator_core.contract.decision_object.envelope import "
        "build_envelope  # noqa: F401,E402\n",
    )
    init_py.write_text("".join(lines), encoding="utf-8")

    hits = _forbidden_hits(_probe_import_closure(engine_copy))
    assert hits, (
        "the mutated copy restored a heavy module-scope import to "
        "merge_assemble/__init__.py, so the closure probe MUST report a "
        "forbidden module. It reported none, which means the gate above proves "
        "nothing."
    )
