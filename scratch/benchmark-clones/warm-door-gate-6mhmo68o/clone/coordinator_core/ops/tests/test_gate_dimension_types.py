"""
coordinator_core.ops.tests.test_gate_dimension_types

Op-level tests for the "types" dimension check (C2,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C2), registered
via `gate_dimension_types.register_dimension("types", ...)`.

Coverage:
  - self-registration: importing the module plugs a real check into
    `gate_validate_invocable`'s "types" slot (no longer the stub).
  - scope filter: non-`.py` changed files never reach mypy; an empty
    `.py`-filtered set is UNAVAILABLE, never a vacuous PASS.
  - tool resolution: `resolve_tool("mypy")` unavailable degrades to
    UNAVAILABLE with the tool's own reason string, never a crash.
  - mypy exit-code contract: rc=0 -> PASS, rc=1 -> FAIL, any other rc (or a
    subprocess-layer failure) -> UNAVAILABLE, never FAIL (a broken tool run
    must never masquerade as "found type errors").
  - live smoke: the real dimension check never raises regardless of what is
    actually on PATH on the machine running the suite.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C2
"""

from __future__ import annotations

import subprocess

import pytest

# ---------------------------------------------------------------------------
# Import guards -- MUST precede any test so both modules' registration side
# effects fire (gate_validate_invocable's @register_op, then this module's
# register_dimension("types", ...) call).
# ---------------------------------------------------------------------------
import coordinator_core.ops.gate_validate_invocable  # noqa: F401
import coordinator_core.ops.gate_dimension_types as types_dim  # noqa: E402

from coordinator_core.ops.gate_validate_invocable import (
    Verdict,
    _DIMENSION_REGISTRY,
    _run_dimension,
)
from coordinator_core.ops.tests._dod_gate_test_helpers import available, unavailable


@pytest.fixture(autouse=True)
def _restore_dimension_registry():
    """Isolate registry mutations across tests, mirroring
    test_gate_dimension_latency.py's own fixture."""
    original = dict(_DIMENSION_REGISTRY)
    yield
    _DIMENSION_REGISTRY.clear()
    _DIMENSION_REGISTRY.update(original)


def test_self_registration_replaces_stub() -> None:
    assert _DIMENSION_REGISTRY["types"] is types_dim._check_types


def test_no_python_files_is_unavailable(tmp_path) -> None:
    result = types_dim._check_types(["docs/reference/foo.md"], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "nothing" in result.detail.lower()


def test_empty_changed_files_is_unavailable(tmp_path) -> None:
    result = types_dim._check_types([], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE


def test_tool_unavailable_degrades_to_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        types_dim,
        "resolve_tool",
        lambda tool: unavailable("mypy", "mypy not found; install it"),
    )
    result = types_dim._check_types(["coordinator_core/ops/gate_dimension_types.py"], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "mypy not found" in result.detail


def test_rc0_is_pass(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        types_dim,
        "resolve_tool",
        lambda tool: available("mypy", "/fake/mypy"),
    )
    monkeypatch.setattr(types_dim, "_run_mypy", lambda mypy_path, py_files, repo_root: (0, "Success", ""))
    result = types_dim._check_types(["coordinator_core/ops/gate_dimension_types.py"], None, tmp_path)
    assert result.verdict is Verdict.PASS


def test_rc1_is_fail(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        types_dim,
        "resolve_tool",
        lambda tool: available("mypy", "/fake/mypy"),
    )
    monkeypatch.setattr(
        types_dim,
        "_run_mypy",
        lambda mypy_path, py_files, repo_root: (1, "foo.py:1: error: bad type", ""),
    )
    result = types_dim._check_types(["coordinator_core/ops/gate_dimension_types.py"], None, tmp_path)
    assert result.verdict is Verdict.FAIL
    assert "bad type" in result.detail


def test_rc2_fatal_error_is_unavailable_not_fail(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        types_dim,
        "resolve_tool",
        lambda tool: available("mypy", "/fake/mypy"),
    )
    monkeypatch.setattr(
        types_dim, "_run_mypy", lambda mypy_path, py_files, repo_root: (2, "", "fatal: bad config")
    )
    result = types_dim._check_types(["coordinator_core/ops/gate_dimension_types.py"], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "fatal: bad config" in result.detail


def test_timeout_is_unavailable_not_fail(monkeypatch, tmp_path) -> None:
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="mypy", timeout=120)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    rc, out, err = types_dim._run_mypy("mypy", ["a.py"], str(tmp_path))
    assert rc != 0 and rc != 1
    assert "timed out" in err


def test_non_py_files_filtered_out() -> None:
    assert types_dim._python_changed_files(["a.py", "b.md", "c.py", "d.txt"]) == ["a.py", "c.py"]


def test_run_dimension_wraps_types_check(monkeypatch, tmp_path) -> None:
    """Sanity check the seam-level wrapper (`_run_dimension`) round-trips
    this dimension's real check the same as any other registered dimension."""
    monkeypatch.setattr(
        types_dim,
        "resolve_tool",
        lambda tool: unavailable(tool, f"{tool} absent"),
    )
    result = _run_dimension("types", ["coordinator_core/ops/gate_dimension_types.py"], None, tmp_path)
    assert result.dimension == "types"
    assert result.verdict is Verdict.UNAVAILABLE


def test_live_smoke_never_raises(tmp_path) -> None:
    """Real machine, no monkeypatching -- mypy is expected UNAVAILABLE here
    (not resolved on the authoring machine), but the check must never raise
    regardless of what is actually installed."""
    result = types_dim._check_types(["coordinator_core/ops/gate_dimension_types.py"], None, tmp_path)
    assert result.dimension == "types"
    assert result.verdict in (Verdict.PASS, Verdict.FAIL, Verdict.UNAVAILABLE)
