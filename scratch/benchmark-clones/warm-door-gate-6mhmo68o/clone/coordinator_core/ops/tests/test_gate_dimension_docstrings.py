"""
coordinator_core.ops.tests.test_gate_dimension_docstrings

Op-level tests for the "docstrings" dimension check (C3,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C3), registered
via `gate_dimension_docstrings.register_dimension("docstrings", ...)`.

Coverage:
  - self-registration: importing the module plugs a real check into
    `gate_validate_invocable`'s "docstrings" slot (no longer the stub).
  - tool resolution: either `ruff` or `interrogate` unavailable degrades to
    UNAVAILABLE with the missing tool(s) named, never a crash.
  - fragment/floor loading: a missing/empty `.github/ported-ops-paths.txt`
    or a missing/malformed `.github/docstring-coverage-floor.json` degrades
    to UNAVAILABLE, never a vacuous PASS.
  - stale-fragment tolerance: a fragment entry absent on disk is filtered
    out rather than raising; zero survivors is UNAVAILABLE.
  - exit-code contract for both tools: rc=0 -> that tool's leg passes,
    rc=1 -> that tool's leg fails (FAIL), any other rc -> UNAVAILABLE (a
    broken tool run must never masquerade as a docstring FAIL).
  - live smoke: the real dimension check never raises regardless of what is
    actually on PATH on the machine running the suite.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C3
"""

from __future__ import annotations

import subprocess

import pytest

# ---------------------------------------------------------------------------
# Import guards -- MUST precede any test so both modules' registration side
# effects fire (gate_validate_invocable's @register_op, then this module's
# register_dimension("docstrings", ...) call). Mirrors test_gate_dimension_
# types.py's own import-order discipline -- see gate_validate_invocable.py's
# bottom-of-module import block comment for why order matters here.
# ---------------------------------------------------------------------------
import coordinator_core.ops.gate_validate_invocable  # noqa: F401
import coordinator_core.ops.gate_dimension_docstrings as doc_dim  # noqa: E402

from coordinator_core.ops.gate_validate_invocable import (
    Verdict,
    _DIMENSION_REGISTRY,
    _run_dimension,
)
from coordinator_core.ops.tests._dod_gate_test_helpers import (
    available as _available,
    unavailable as _unavailable,
)


@pytest.fixture(autouse=True)
def _restore_dimension_registry():
    """Isolate registry mutations across tests, mirroring
    test_gate_dimension_types.py's own fixture."""
    original = dict(_DIMENSION_REGISTRY)
    yield
    _DIMENSION_REGISTRY.clear()
    _DIMENSION_REGISTRY.update(original)


def test_self_registration_replaces_stub() -> None:
    assert _DIMENSION_REGISTRY["docstrings"] is doc_dim._check_docstrings


def test_ruff_unavailable_degrades_to_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        doc_dim,
        "resolve_tool",
        lambda tool: _unavailable("ruff", "ruff not found; install it")
        if tool == "ruff"
        else _available(tool),
    )
    result = doc_dim._check_docstrings([], None, None)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "ruff" in result.detail


def test_interrogate_unavailable_degrades_to_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        doc_dim,
        "resolve_tool",
        lambda tool: _unavailable("interrogate", "interrogate not found; install it")
        if tool == "interrogate"
        else _available(tool),
    )
    result = doc_dim._check_docstrings([], None, None)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "interrogate" in result.detail


def test_missing_fragment_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "ported-ops-paths.txt" in result.detail


def test_stale_fragment_all_missing_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    (github_dir / "ported-ops-paths.txt").write_text(
        "# comment\ncoordinator_core/ops/does_not_exist.py\n", encoding="utf-8"
    )
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "stale" in result.detail.lower()


def test_missing_floor_file_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    real_file = tmp_path / "real.py"
    real_file.write_text('"""Docstring."""\n', encoding="utf-8")
    (github_dir / "ported-ops-paths.txt").write_text("real.py\n", encoding="utf-8")
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "docstring-coverage-floor.json" in result.detail


def _seed_repo(tmp_path, fail_under: float = 50.0):
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    real_file = tmp_path / "real.py"
    real_file.write_text('"""Docstring."""\n', encoding="utf-8")
    (github_dir / "ported-ops-paths.txt").write_text("real.py\n", encoding="utf-8")
    (github_dir / "docstring-coverage-floor.json").write_text(
        f'{{"fail_under": {fail_under}}}', encoding="utf-8"
    )
    return tmp_path


def test_both_tools_pass_is_pass(monkeypatch, tmp_path) -> None:
    _seed_repo(tmp_path)
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    monkeypatch.setattr(doc_dim, "_run_ruff", lambda *a, **k: (0, "All checks passed!", ""))
    monkeypatch.setattr(
        doc_dim, "_run_interrogate", lambda *a, **k: (0, "RESULT: PASSED", "")
    )
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.PASS


def test_ruff_rc1_is_fail(monkeypatch, tmp_path) -> None:
    _seed_repo(tmp_path)
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    monkeypatch.setattr(
        doc_dim, "_run_ruff", lambda *a, **k: (1, "real.py:1:1: D100 Missing docstring", "")
    )
    monkeypatch.setattr(
        doc_dim, "_run_interrogate", lambda *a, **k: (0, "RESULT: PASSED", "")
    )
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.FAIL
    assert "D100" in result.detail


def test_interrogate_rc1_is_fail(monkeypatch, tmp_path) -> None:
    _seed_repo(tmp_path)
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    monkeypatch.setattr(doc_dim, "_run_ruff", lambda *a, **k: (0, "All checks passed!", ""))
    monkeypatch.setattr(
        doc_dim,
        "_run_interrogate",
        lambda *a, **k: (1, "RESULT: FAILED (minimum: 50.0%, actual: 10.0%)", ""),
    )
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.FAIL
    assert "FAILED" in result.detail


def test_ruff_broken_rc_is_unavailable_not_fail(monkeypatch, tmp_path) -> None:
    _seed_repo(tmp_path)
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    monkeypatch.setattr(doc_dim, "_run_ruff", lambda *a, **k: (2, "", "fatal: bad config"))
    monkeypatch.setattr(
        doc_dim, "_run_interrogate", lambda *a, **k: (0, "RESULT: PASSED", "")
    )
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "fatal: bad config" in result.detail


def test_interrogate_broken_rc_is_unavailable_not_fail(monkeypatch, tmp_path) -> None:
    _seed_repo(tmp_path)
    monkeypatch.setattr(doc_dim, "resolve_tool", lambda tool: _available(tool))
    monkeypatch.setattr(doc_dim, "_run_ruff", lambda *a, **k: (0, "All checks passed!", ""))
    monkeypatch.setattr(
        doc_dim, "_run_interrogate", lambda *a, **k: (2, "", "usage error: bad path")
    )
    result = doc_dim._check_docstrings([], None, tmp_path)
    assert result.verdict is Verdict.UNAVAILABLE
    assert "usage error" in result.detail


def test_timeout_is_unavailable_not_fail(tmp_path) -> None:
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=120)

    orig_run = subprocess.run
    try:
        subprocess.run = _raise_timeout
        rc, out, err = doc_dim._run_ruff("ruff", ["a.py"], str(tmp_path))
        assert rc not in (0, 1)
        assert "timed out" in err
    finally:
        subprocess.run = orig_run


def test_run_dimension_wraps_docstrings_check(monkeypatch, tmp_path) -> None:
    """Sanity check the seam-level wrapper (`_run_dimension`) round-trips
    this dimension's real check the same as any other registered dimension."""
    monkeypatch.setattr(
        doc_dim,
        "resolve_tool",
        lambda tool: _unavailable(tool, f"{tool} absent"),
    )
    result = _run_dimension("docstrings", [], None, tmp_path)
    assert result.dimension == "docstrings"
    assert result.verdict is Verdict.UNAVAILABLE


def test_live_smoke_never_raises(tmp_path) -> None:
    """The real check, against whatever ruff/interrogate state this machine
    actually has, must never raise -- mirrors test_gate_dimension_types.py's
    own live-smoke test."""
    result = doc_dim._check_docstrings([], None, None)
    assert result.verdict in (
        Verdict.PASS,
        Verdict.FAIL,
        Verdict.UNAVAILABLE,
    )
