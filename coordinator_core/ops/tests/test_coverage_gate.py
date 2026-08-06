"""
coordinator_core.ops.tests.test_coverage_gate

Tests for the coverage.gate op — specifically the C6 disk-artifact write
introduced by docs/plans/2026-07-05-coordinator-core-execution-model-retirem.md § C6.

Coverage:
    (a) registry  — "coverage.gate" present in _REGISTRY after import
    (b) artifact-written — artifact file exists after op call
    (c) artifact-schema  — artifact contains all required fields with correct types
    (d) artifact-verdict — verdict field extracted correctly from verdict_line
    (e) artifact-atomic  — artifact is valid JSON (not partially written)
    (f) artifact-uncovered-shas — uncovered_shas propagated correctly
    (g) no-repo-root-returns-error — repo_root=None returns exit_code 1, no artifact written
    (h) artifact-covered-verdict — COVERED verdict written correctly

Spec backlink: coordinator_core/ops/coverage_gate.py § Disk artifact
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.coverage_gate  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.coverage_gate import (
    ARTIFACT_RELPATH,
    _coverage_gate,
    _extract_verdict,
)

# Positive floor assertion: op must be registered before any test runs.
_OP_NAME = "coverage.gate"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.coverage_gate @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_result(
    verdict: str = "COVERED",
    chain_commits: int = 3,
    covered: int = 3,
    uncovered: int = 0,
    exit_code: int = 0,
    notes: list[str] | None = None,
    uncovered_shas: list[str] | None = None,
    bookkeeping_shas: list[str] | None = None,
    planning_shas: list[str] | None = None,
    dag_node_attribution: dict | None = None,
    coverage_ratio: float = 1.0,
) -> MagicMock:
    """Return a MagicMock that mimics coordinator_core.coverage.CoverageResult.

    dag_node_attribution defaults to {} (flat-mode / no-attribution shape) —
    a bare MagicMock() attribute is truthy, which would silently exercise the
    grouped-notes branch in every pre-existing caller of this helper that
    never mentions DAG mode. Explicit default keeps those callers on the flat
    "uncovered: <sha>" fallback path, matching real CoverageResult's own
    default_factory=dict.
    """
    r = MagicMock()
    r.verdict_line = (
        f"range=main..HEAD chain_commits={chain_commits} "
        f"covered={covered} uncovered={uncovered} VERDICT={verdict}"
    )
    r.chain_commits = chain_commits
    r.covered = covered
    r.uncovered = uncovered
    r.exit_code = exit_code
    r.notes = notes if notes is not None else []
    r.uncovered_shas = uncovered_shas if uncovered_shas is not None else []
    r.bookkeeping_shas = bookkeeping_shas if bookkeeping_shas is not None else []
    r.planning_shas = planning_shas if planning_shas is not None else []
    r.dag_node_attribution = (
        dag_node_attribution if dag_node_attribution is not None else {}
    )
    r.coverage_ratio = coverage_ratio
    return r


def _run_op(
    tmp_path: Path,
    fake_result: MagicMock,
    params: dict | None = None,
) -> dict:
    """Invoke _coverage_gate with a mocked run_coverage_gate; return the RPC result dict."""
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake_result,
    ):
        return asyncio.run(
            _coverage_gate(params or {}, repo_root=tmp_path)
        )


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------

def test_registry_coverage_gate_registered() -> None:
    """coverage.gate must be in _REGISTRY after import."""
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) Artifact written
# ---------------------------------------------------------------------------

def test_artifact_file_written(tmp_path: Path) -> None:
    """op call writes the artifact at the documented path."""
    fake = _make_fake_result()
    _run_op(tmp_path, fake)

    artifact_path = tmp_path / ARTIFACT_RELPATH
    assert artifact_path.exists(), (
        f"expected artifact at {artifact_path} — not found after op call"
    )


# ---------------------------------------------------------------------------
# (c) Artifact schema — all required fields present with correct types
# ---------------------------------------------------------------------------

def test_artifact_schema(tmp_path: Path) -> None:
    """Artifact contains all documented schema_version-1 fields."""
    fake = _make_fake_result(
        verdict="COVERED",
        chain_commits=5,
        covered=5,
        uncovered=0,
        exit_code=0,
    )
    _run_op(tmp_path, fake)

    artifact_path = tmp_path / ARTIFACT_RELPATH
    data = json.loads(artifact_path.read_text(encoding="utf-8"))

    # Required fields and their expected types
    required: dict[str, type] = {
        "schema_version": str,
        "verdict": str,
        "verdict_line": str,
        "coverage_ratio": float,
        "exit_code": int,
        "chain_commits": int,
        "covered": int,
        "uncovered": int,
        "uncovered_shas": list,
        "bookkeeping_shas": list,
        "planning_shas": list,
        "notes": list,
    }
    for field, expected_type in required.items():
        assert field in data, f"required field {field!r} missing from artifact"
        assert isinstance(data[field], expected_type), (
            f"field {field!r}: expected {expected_type.__name__}, "
            f"got {type(data[field]).__name__}"
        )

    assert data["schema_version"] == "1", (
        f"expected schema_version='1', got {data['schema_version']!r}"
    )
    assert data["chain_commits"] == 5
    assert data["covered"] == 5
    assert data["uncovered"] == 0
    assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# (d) Verdict extracted correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict_token,expected", [
    ("COVERED", "COVERED"),
    ("UNCOVERED", "UNCOVERED"),
    ("INDETERMINATE", "INDETERMINATE"),
])
def test_extract_verdict_helper(verdict_token: str, expected: str) -> None:
    """_extract_verdict returns the token after VERDICT= in verdict_line."""
    verdict_line = (
        f"range=main..HEAD chain_commits=2 covered=1 uncovered=1 VERDICT={verdict_token}"
    )
    assert _extract_verdict(verdict_line) == expected


def test_artifact_verdict_field(tmp_path: Path) -> None:
    """Artifact verdict field matches the token extracted from verdict_line."""
    fake = _make_fake_result(verdict="UNCOVERED", covered=2, uncovered=1, exit_code=0)
    _run_op(tmp_path, fake)

    data = json.loads((tmp_path / ARTIFACT_RELPATH).read_text(encoding="utf-8"))
    assert data["verdict"] == "UNCOVERED"


# ---------------------------------------------------------------------------
# (e) Atomic write — artifact is valid JSON
# ---------------------------------------------------------------------------

def test_artifact_valid_json(tmp_path: Path) -> None:
    """Artifact file is valid, complete JSON (not a partial write)."""
    fake = _make_fake_result()
    _run_op(tmp_path, fake)

    raw = (tmp_path / ARTIFACT_RELPATH).read_text(encoding="utf-8")
    # This raises json.JSONDecodeError on invalid/partial JSON.
    data = json.loads(raw)
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# (f) uncovered_shas propagated
# ---------------------------------------------------------------------------

def test_artifact_uncovered_shas(tmp_path: Path) -> None:
    """uncovered_shas from CoverageResult propagate into the artifact."""
    shas = ["aabbcc00", "deadbeef"]
    fake = _make_fake_result(
        verdict="UNCOVERED",
        chain_commits=3,
        covered=1,
        uncovered=2,
        exit_code=0,
        uncovered_shas=shas,
    )
    _run_op(tmp_path, fake)

    data = json.loads((tmp_path / ARTIFACT_RELPATH).read_text(encoding="utf-8"))
    assert data["uncovered_shas"] == shas, (
        f"expected uncovered_shas={shas!r}, got {data['uncovered_shas']!r}"
    )
    # uncovered SHA notes must also appear in the notes list
    notes = data["notes"]
    for sha in shas:
        assert any(sha in n for n in notes), (
            f"uncovered SHA {sha!r} expected in notes list, got {notes!r}"
        )


def test_artifact_bookkeeping_shas_propagated(tmp_path: Path) -> None:
    """bookkeeping_shas from CoverageResult propagates into both the artifact
    and the RPC reply dict — additive field (F3/schema_version not bumped).
    """
    shas = ["c0ffee01"]
    fake = _make_fake_result(
        verdict="COVERED",
        chain_commits=3,
        covered=3,
        uncovered=0,
        exit_code=0,
        bookkeeping_shas=shas,
    )
    result = _run_op(tmp_path, fake)

    assert result["bookkeeping_shas"] == shas, (
        f"expected bookkeeping_shas={shas!r} in RPC reply, got {result.get('bookkeeping_shas')!r}"
    )

    data = json.loads((tmp_path / ARTIFACT_RELPATH).read_text(encoding="utf-8"))
    assert data["bookkeeping_shas"] == shas, (
        f"expected bookkeeping_shas={shas!r} in artifact, got {data.get('bookkeeping_shas')!r}"
    )


def test_artifact_planning_shas_propagated_and_stays_in_uncovered(tmp_path: Path) -> None:
    """planning_shas from CoverageResult propagates into both the artifact
    and the RPC reply dict — additive field, mirroring bookkeeping_shas'
    propagation test above. Unlike bookkeeping_shas, a planning sha is a
    SUBSET of uncovered_shas (docs/plans/2026-08-05-coverage-gate-planning-
    artifact-class.md § C4 AC4/AC9): PLANNING is not exempt from review, so
    it must still be present in uncovered_shas after propagation, not
    subtracted the way the (disjoint) bookkeeping class is.
    """
    planning = ["p1a2n3n4"]
    uncovered = ["p1a2n3n4", "c0de0001"]
    fake = _make_fake_result(
        verdict="UNCOVERED",
        chain_commits=3,
        covered=1,
        uncovered=2,
        exit_code=0,
        uncovered_shas=uncovered,
        planning_shas=planning,
    )
    result = _run_op(tmp_path, fake)

    assert result["planning_shas"] == planning, (
        f"expected planning_shas={planning!r} in RPC reply, got {result.get('planning_shas')!r}"
    )

    data = json.loads((tmp_path / ARTIFACT_RELPATH).read_text(encoding="utf-8"))
    assert data["planning_shas"] == planning, (
        f"expected planning_shas={planning!r} in artifact, got {data.get('planning_shas')!r}"
    )
    # The dangerous outcome named by the plan's verification bar: a planning
    # sha vanishing from uncovered_shas at the envelope, which would silently
    # exempt it from the verdict exactly like the bookkeeping class.
    assert set(planning) <= set(data["uncovered_shas"]), (
        f"planning_shas {planning!r} must remain a subset of uncovered_shas "
        f"{data['uncovered_shas']!r} — PLANNING is not exempt from review (AC9)"
    )


# ---------------------------------------------------------------------------
# (g) No repo_root → error reply, no artifact
# ---------------------------------------------------------------------------

def test_no_repo_root_returns_error(tmp_path: Path) -> None:
    """repo_root=None returns exit_code 1 without writing any artifact."""
    result = asyncio.run(_coverage_gate({}, repo_root=None))

    assert result["exit_code"] == 1, (
        f"expected exit_code=1 when repo_root=None, got {result['exit_code']}"
    )
    # No artifact should be written
    artifact_path = tmp_path / ARTIFACT_RELPATH
    assert not artifact_path.exists(), (
        f"artifact must not be written when repo_root=None"
    )


# ---------------------------------------------------------------------------
# (h) COVERED verdict written correctly
# ---------------------------------------------------------------------------

def test_artifact_covered_verdict_full_shape(tmp_path: Path) -> None:
    """End-to-end shape check for a COVERED verdict artifact."""
    fake = _make_fake_result(
        verdict="COVERED",
        chain_commits=2,
        covered=2,
        uncovered=0,
        exit_code=0,
        notes=["D3: session-id attribution used"],
    )
    result = _run_op(tmp_path, fake, params={"range": "main..HEAD"})

    # RPC reply unchanged
    assert result["exit_code"] == 0
    assert "VERDICT=COVERED" in result["verdict_line"]

    # Artifact shape
    data = json.loads((tmp_path / ARTIFACT_RELPATH).read_text(encoding="utf-8"))
    assert data["schema_version"] == "1"
    assert data["verdict"] == "COVERED"
    assert data["exit_code"] == 0
    assert data["chain_commits"] == 2
    assert data["covered"] == 2
    assert data["uncovered"] == 0
    assert data["uncovered_shas"] == []
    # notes from CoverageResult propagated
    assert "D3: session-id attribution used" in data["notes"]


# ---------------------------------------------------------------------------
# D1 — dedup: DAG-mode grouped attribution suppresses the flat "uncovered:
# <sha>" duplicate; non-DAG / empty-attribution keeps the flat fallback.
# ---------------------------------------------------------------------------

def test_dag_mode_attribution_present_no_flat_duplicate(tmp_path: Path) -> None:
    """When dag_node_attribution is non-empty, the flat 'uncovered: <sha>'
    lines must NOT be appended on top of the grouped ancestry block already
    present in result.notes — each uncovered SHA appears exactly once.
    """
    shas = ["aaaaaaaaaa", "bbbbbbbbbb"]
    ancestry_notes = [
        "These 2 commit(s) are YOUR CHAIN'S inheritance",
        "Baton ancestry (oldest to newest):",
        "  [A] some-handoff    deliverable-x",
        "uncovered, by originating baton:",
        f"  [A]  {shas[0][:9]}",
        f"  [A]  {shas[1][:9]}",
    ]
    fake = _make_fake_result(
        verdict="UNCOVERED",
        chain_commits=2,
        covered=0,
        uncovered=2,
        exit_code=0,
        notes=ancestry_notes,
        uncovered_shas=shas,
        dag_node_attribution={"some-handoff": MagicMock()},
    )
    result = _run_op(tmp_path, fake)

    notes = result["notes"]
    for sha in shas:
        occurrences = sum(1 for n in notes if sha[:9] in n)
        assert occurrences == 1, (
            f"expected exactly one note mentioning {sha!r}, found {occurrences}: {notes!r}"
        )
    # No bare flat-form line ("uncovered: <sha>") duplicated alongside the
    # grouped baton-tagged form.
    assert not any(n.startswith("uncovered: ") for n in notes), (
        f"flat 'uncovered: <sha>' lines must not duplicate the grouped render: {notes!r}"
    )


def test_flat_mode_no_attribution_keeps_flat_fallback(tmp_path: Path) -> None:
    """Non-DAG mode (empty dag_node_attribution): the flat 'uncovered: <sha>'
    lines must still print unchanged — the fallback every non-DAG caller relies on.
    """
    shas = ["cccccccccc", "dddddddddd"]
    fake = _make_fake_result(
        verdict="UNCOVERED",
        chain_commits=2,
        covered=0,
        uncovered=2,
        exit_code=0,
        uncovered_shas=shas,
        dag_node_attribution={},
    )
    result = _run_op(tmp_path, fake)

    notes = result["notes"]
    for sha in shas:
        assert f"uncovered: {sha}" in notes, (
            f"expected flat fallback line for {sha!r} in {notes!r}"
        )


def test_indeterminate_empty_attribution_no_flat_lines_needed(tmp_path: Path) -> None:
    """INDETERMINATE: uncovered_shas is empty and dag_node_attribution is empty
    — no flat lines are synthesized (nothing to append), matching pre-existing
    behaviour.
    """
    fake = _make_fake_result(
        verdict="INDETERMINATE",
        chain_commits=0,
        covered=0,
        uncovered=0,
        exit_code=2,
        notes=["cannot resolve origin/main"],
        uncovered_shas=[],
        dag_node_attribution={},
    )
    result = _run_op(tmp_path, fake)
    assert result["notes"] == ["cannot resolve origin/main"]


# ---------------------------------------------------------------------------
# D3 — verbose kwarg reachable end-to-end from JSON-RPC params through to
# coverage.run_coverage_gate.
# ---------------------------------------------------------------------------

def test_verbose_param_forwarded_to_run_coverage_gate(tmp_path: Path) -> None:
    """params['verbose']=True reaches run_coverage_gate(verbose=True)."""
    fake = _make_fake_result()
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake,
    ) as mock_run:
        asyncio.run(
            _coverage_gate({"verbose": True}, repo_root=tmp_path)
        )
    assert mock_run.call_args.kwargs.get("verbose") is True, (
        f"expected verbose=True forwarded to run_coverage_gate, "
        f"got kwargs={mock_run.call_args.kwargs!r}"
    )


def test_verbose_defaults_false_when_absent(tmp_path: Path) -> None:
    """Omitting 'verbose' from params forwards verbose=False (unchanged default)."""
    fake = _make_fake_result()
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake,
    ) as mock_run:
        asyncio.run(_coverage_gate({}, repo_root=tmp_path))
    assert mock_run.call_args.kwargs.get("verbose") is False, (
        f"expected verbose=False by default, got kwargs={mock_run.call_args.kwargs!r}"
    )
