"""
coordinator_core.ops.tests.test_halt_on_coverage_gate_uncovered

Tests for the coverage.halt_on_uncovered op (C1e — the caller-side
COORDINATOR_OVERRIDE_COVERAGE_GATE=1 warn-and-continue escape hatch, the one
gap not already covered by coverage.gate + coverage.py::run_coverage_gate).

Coverage:
    (a) registry            — "coverage.halt_on_uncovered" present in _REGISTRY after import
    (b) halts-on-uncovered  — UNCOVERED verdict + no override => halted=True
    (c) no-halt-covered     — COVERED / INDETERMINATE verdicts never halt
    (d) param-override      — override=True param bypasses halt on UNCOVERED
    (e) env-override        — COORDINATOR_OVERRIDE_COVERAGE_GATE=1 bypasses halt on UNCOVERED
    (f) param-wins-over-env — explicit override=False param wins even if env is set
    (g) override-applied-only-when-it-mattered — override flag set but verdict COVERED
        => override_applied False (nothing to override)
    (h) no-repo-root        — repo_root=None returns halted=True, verdict UNKNOWN
    (i) idempotent          — two identical invocations return the same decision (AC7)

Spec backlink: coordinator_core/ops/coverage_gate.py § coverage.halt_on_uncovered
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.coverage_gate  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.coverage_gate import (
    OVERRIDE_ENV_VAR,
    _coverage_halt_on_uncovered,
)

# Positive floor assertion: op must be registered before any test runs.
_OP_NAME = "coverage.halt_on_uncovered"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.coverage_gate @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_result(verdict: str = "UNCOVERED") -> MagicMock:
    """Return a MagicMock that mimics coordinator_core.coverage.CoverageResult."""
    r = MagicMock()
    r.verdict_line = f"range=main..HEAD chain_commits=2 covered=1 uncovered=1 VERDICT={verdict}"
    r.chain_commits = 2
    r.covered = 1
    r.uncovered = 1
    r.exit_code = 0
    r.notes = []
    r.uncovered_shas = []
    return r


def _run_op(
    tmp_path: Path,
    fake_result: MagicMock,
    params: dict | None = None,
) -> dict:
    """Invoke _coverage_halt_on_uncovered with a mocked run_coverage_gate."""
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake_result,
    ):
        return asyncio.run(
            _coverage_halt_on_uncovered(params or {}, repo_root=tmp_path)
        )


@pytest.fixture(autouse=True)
def _clean_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure OVERRIDE_ENV_VAR never leaks in from the ambient test environment."""
    monkeypatch.delenv(OVERRIDE_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------

def test_registry_halt_on_uncovered_registered() -> None:
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) Halts on UNCOVERED with no override
# ---------------------------------------------------------------------------

def test_halts_on_uncovered_no_override(tmp_path: Path) -> None:
    result = _run_op(tmp_path, _make_fake_result("UNCOVERED"))
    assert result["halted"] is True
    assert result["verdict"] == "UNCOVERED"
    assert result["override_applied"] is False
    assert "halt" in result["message"].lower() or "uncovered" in result["message"].lower()


# ---------------------------------------------------------------------------
# (c) No halt on COVERED / INDETERMINATE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["COVERED", "INDETERMINATE", "UNKNOWN"])
def test_no_halt_on_non_uncovered(tmp_path: Path, verdict: str) -> None:
    result = _run_op(tmp_path, _make_fake_result(verdict))
    assert result["halted"] is False
    assert result["verdict"] == verdict
    assert result["override_applied"] is False


# ---------------------------------------------------------------------------
# (d) Explicit param override bypasses halt
# ---------------------------------------------------------------------------

def test_param_override_bypasses_halt(tmp_path: Path) -> None:
    result = _run_op(tmp_path, _make_fake_result("UNCOVERED"), params={"override": True})
    assert result["halted"] is False
    assert result["verdict"] == "UNCOVERED"
    assert result["override_applied"] is True


# ---------------------------------------------------------------------------
# (e) Env-var override bypasses halt
# ---------------------------------------------------------------------------

def test_env_override_bypasses_halt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "1")
    result = _run_op(tmp_path, _make_fake_result("UNCOVERED"))
    assert result["halted"] is False
    assert result["override_applied"] is True


def test_env_override_wrong_value_does_not_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "true")
    result = _run_op(tmp_path, _make_fake_result("UNCOVERED"))
    assert result["halted"] is True
    assert result["override_applied"] is False


# ---------------------------------------------------------------------------
# (f) Explicit param wins over env var
# ---------------------------------------------------------------------------

def test_param_false_wins_over_env_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "1")
    result = _run_op(tmp_path, _make_fake_result("UNCOVERED"), params={"override": False})
    assert result["halted"] is True
    assert result["override_applied"] is False


# ---------------------------------------------------------------------------
# (g) override_applied only reflects a verdict it actually changed
# ---------------------------------------------------------------------------

def test_override_applied_false_when_verdict_covered(tmp_path: Path) -> None:
    result = _run_op(tmp_path, _make_fake_result("COVERED"), params={"override": True})
    assert result["halted"] is False
    assert result["override_applied"] is False


# ---------------------------------------------------------------------------
# (h) No repo root => fail-loud halt, not a silent pass
# ---------------------------------------------------------------------------

def test_no_repo_root_returns_halted_unknown() -> None:
    result = asyncio.run(_coverage_halt_on_uncovered({}, repo_root=None))
    assert result["halted"] is True
    assert result["verdict"] == "UNKNOWN"
    assert result["override_applied"] is False


# ---------------------------------------------------------------------------
# (i) Idempotency (AC7) — two identical invocations, identical decision
# ---------------------------------------------------------------------------

def test_double_invocation_is_safe_no_op(tmp_path: Path) -> None:
    fake = _make_fake_result("UNCOVERED")
    first = _run_op(tmp_path, fake, params={"override": True})
    second = _run_op(tmp_path, fake, params={"override": True})
    assert first == second
    assert os.environ.get(OVERRIDE_ENV_VAR) is None


def test_closing_session_id_threaded_to_run_coverage_gate(tmp_path: Path) -> None:
    """Regression for improvement-queue
    2026-07-03-coverage-gate-dag-mode-misfires-on-prede.yaml: this op used to
    hardcode closing_session_id="" on every call to run_coverage_gate,
    silently discarding a caller-supplied session id and forcing DAG mode
    onto the fragile git-log --follow -M100% add-commit fallback for session
    attribution -- unlike the sibling coverage.gate op, which has always
    threaded closing_session_id through. Must fail pre-fix (hardcoded ""
    regardless of params) and pass post-fix (params value passed through).
    """
    fake = _make_fake_result("COVERED")
    sid = "55555555-5555-4555-8555-555555555555"
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake,
    ) as mock_run:
        asyncio.run(
            _coverage_halt_on_uncovered(
                {"from_handoff": "/tmp/some-handoff.md", "closing_session_id": sid},
                repo_root=tmp_path,
            )
        )
    assert mock_run.call_args.kwargs["closing_session_id"] == sid, (
        "coverage.halt_on_uncovered must thread a caller-supplied "
        "closing_session_id through to run_coverage_gate exactly as "
        f"coverage.gate does; call kwargs={mock_run.call_args.kwargs!r}"
    )


def test_closing_session_id_defaults_to_empty_when_absent(tmp_path: Path) -> None:
    """When the caller omits closing_session_id, the op must still pass an
    empty string (not None, not raise) -- preserves the existing D3
    fallback-to-git-log-follow behaviour for callers that don't have a
    session id to offer.
    """
    fake = _make_fake_result("COVERED")
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake,
    ) as mock_run:
        asyncio.run(
            _coverage_halt_on_uncovered(
                {"from_handoff": "/tmp/some-handoff.md"},
                repo_root=tmp_path,
            )
        )
    assert mock_run.call_args.kwargs["closing_session_id"] == ""
