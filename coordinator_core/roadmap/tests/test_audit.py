"""
coordinator_core.roadmap.tests.test_audit — regression net for
coordinator_core.roadmap.audit, ported from the bash oracle's two test files:
  - coordinator/bin/tests/test-audit-roadmap-dependency-order.sh (Scenarios A/B/C)
  - coordinator/bin/tests/test-audit-roadmap-verdict-regex.sh (2nd-cell false-positive)

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T3a-g3e
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.roadmap.audit import (
    _count_verdict,
    _claude_klabauter_root,
    _machine_local_get,
    _parse_pending_stubs,
    _same_path,
    _state_root,
    check_dependency_order,
    main,
    resolve_data_root,
    resolve_repo_root,
    run_audit,
    validate_run_id,
)

# Declared, not excused: `test_resolve_repo_root_git_dir_returns_toplevel` and
# `test_resolve_data_root_derives_from_cwd_repo_root` spawn a real `git init` because
# the property under test is `resolve_repo_root`'s real `git rev-parse --show-toplevel`
# resolution against an actual repo (including a nested-cwd case) -- the exact
# P1 silent-cwd-fallback bug this file's own docstring names required a genuine git
# repo to catch, not a mock returning a canned toplevel. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the route
# for this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_stub(
    path: Path,
    roadmap_id: str,
    stub_id: str,
    sprint: Optional[int],
    wave: Optional[int],
    blocked_by: Optional[List[str]] = None,
    deployment_state: str = "active",
    gate_dependency: Optional[str] = None,
    number: Optional[int] = None,
    kind: str = "spinoff-roadmap",
) -> None:
    # Review: code-reviewer (P1, Finding 1) — `kind` defaults to the retired
    # spelling for byte-parity with every pre-existing caller, but callers
    # below now also pass `kind="roadmap-baton"` (the canonical D1 spelling)
    # to prove `_ROADMAP_BATON_KIND_WHERE`'s `kind in (...)` term actually
    # finds already-migrated stubs — the live defect this diff fixed.
    num = number if number is not None else int(stub_id.rsplit("-", 1)[-1])
    lines = [
        "---",
        f'title: "Test stub {stub_id}"',
        "created: 2026-07-02",
        "status: active",
        f"deployment_state: {deployment_state}",
        f"kind: {kind}",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
        f"number: {num}",
    ]
    if sprint is not None:
        lines.append(f"sprint: {sprint}")
    if wave is not None:
        lines.append(f"wave: {wave}")
    if gate_dependency is not None:
        lines.append(f'gate_dependency: "{gate_dependency}"')
    if blocked_by:
        lines.append("blocked_by:")
        for dep in blocked_by:
            lines.append(f"  - {dep}")
    else:
        lines.append("blocked_by: []")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_reconciliation(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Verdict: KEEP\n" * count, encoding="utf-8")


# ---------------------------------------------------------------------------
# validate_run_id
# ---------------------------------------------------------------------------


def test_validate_run_id_accepts_lowercase_alnum_hyphen() -> None:
    assert validate_run_id("claude-klabauter-strangler-2026-07-04") is None


@pytest.mark.parametrize("bad", ["", "Bad-Id", "-leading-hyphen", "under_score", "UP"])
def test_validate_run_id_rejects_bad_shapes(bad: str) -> None:
    assert validate_run_id(bad) is not None


# ---------------------------------------------------------------------------
# _count_verdict — port of test-audit-roadmap-verdict-regex.sh
# ---------------------------------------------------------------------------


def test_count_verdict_does_not_double_count_2nd_cell_notes_column() -> None:
    text = (
        "| # | notes | verdict |\n"
        "|---|-------|---------|\n"
        "| 1 | **KEEP** this cluster because it is load-bearing | **KEEP** |\n"
    )
    assert _count_verdict(text, "KEEP") == 1


def test_count_verdict_matches_2nd_cell_anchored_shape() -> None:
    text = "| cluster | **KEEP** | notes |\n"
    assert _count_verdict(text, "KEEP") == 1


def test_count_verdict_prose_fallback() -> None:
    text = "Some free text.\nVerdict: KEEP\nMore text.\n"
    assert _count_verdict(text, "KEEP") == 1


def test_count_verdict_cell_value_with_trailing_note() -> None:
    text = "| cluster | **KEEP** (cross-repo relay) | notes |\n"
    assert _count_verdict(text, "KEEP") == 1


# ---------------------------------------------------------------------------
# check_dependency_order — port of test-audit-roadmap-dependency-order.sh
# Scenarios A (good), B (inverted), C (cyclic).
# ---------------------------------------------------------------------------


def test_dependency_order_scenario_a_good() -> None:
    stubs = [
        {"stub_id": "good-1", "number": 1, "sprint": 1, "wave": 1, "blocked_by": []},
        {"stub_id": "good-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": ["good-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is True
    assert result["violations"] == []
    assert result["unresolved"] == []
    assert result["cycle"] is None


def test_dependency_order_scenario_b_inverted() -> None:
    stubs = [
        {"stub_id": "inv-1", "number": 1, "sprint": 1, "wave": 2, "blocked_by": ["inv-2"]},
        {"stub_id": "inv-2", "number": 2, "sprint": 1, "wave": 3, "blocked_by": []},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    reasons = {v["reason"] for v in result["violations"]}
    assert "number-order" in reasons
    assert "same-or-inverted-slot" in reasons
    assert any(v["from"] == "inv-1" for v in result["violations"])


def test_dependency_order_scenario_c_cyclic() -> None:
    stubs = [
        {"stub_id": "cyc-1", "number": 1, "sprint": 1, "wave": 1, "blocked_by": ["cyc-2"]},
        {"stub_id": "cyc-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": ["cyc-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    assert result["cycle"] is not None
    assert set(result["cycle"]) == {"cyc-1", "cyc-2"}


def test_dependency_order_missing_sprint() -> None:
    stubs = [
        {"stub_id": "a-1", "number": 1, "sprint": None, "wave": None, "blocked_by": ["a-2"]},
        {"stub_id": "a-2", "number": 2, "sprint": 1, "wave": 1, "blocked_by": []},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    assert result["violations"][0]["reason"] == "missing-sprint"


def test_dependency_order_unresolved_edge() -> None:
    stubs = [
        {"stub_id": "a-1", "number": 1, "sprint": 1, "wave": 1, "blocked_by": ["missing-9"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    assert result["unresolved"][0]["to"] == "missing-9"


def test_dependency_order_number_derived_from_stub_id_when_absent() -> None:
    # number omitted entirely — derived from trailing -<N> in stub_id.
    stubs = [
        {"stub_id": "x-1", "sprint": 1, "wave": 1, "blocked_by": []},
        {"stub_id": "x-2", "sprint": 1, "wave": 2, "blocked_by": ["x-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# _parse_pending_stubs — Audit 4 awk -F'|' port
# ---------------------------------------------------------------------------


def test_parse_pending_stubs_extracts_2nd_visible_column() -> None:
    # Real table shape (roadmap-planning SKILL.md): | sprint | stub_id | ... | resolved? |
    # awk -F'|' $3 == the 2nd visible column (fields[0]="" before the leading pipe).
    pmg = "| sprint | stub_id | gate question | resolved? |\n| 2 | foo-3 | ok? | pending |\n"
    assert _parse_pending_stubs(pmg) == ["foo-3"]


def test_parse_pending_stubs_ignores_non_matching_cell() -> None:
    pmg = "| 1 | not-a-stub-id-shape | pending |\n"
    # 2nd visible field here is "not-a-stub-id-shape" — doesn't match the stub-id shape.
    assert _parse_pending_stubs(pmg) == []


def test_parse_pending_stubs_ignores_non_pending_rows() -> None:
    pmg = "| id | foo-3 | resolved |\n"
    assert _parse_pending_stubs(pmg) == []


# ---------------------------------------------------------------------------
# End-to-end run_audit — real-tree fixtures under tmp_path (mirrors bash
# real-tree strategy, without needing PID-scoped cleanup since tmp_path is
# already isolated per test).
# ---------------------------------------------------------------------------


def _init_tree(tmp_path: Path) -> Path:
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    return tmp_path


def test_run_audit_end_to_end_good_roadmap(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-good"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1)
    _write_stub(handoffs / f"{run_id}-2.md", run_id, f"{run_id}-2", 1, 2, blocked_by=[f"{run_id}-1"])
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0
    assert any("Audit 5:" in line and line.startswith("PASS:") for line in stdout_lines)
    assert stderr_lines == []


def test_run_audit_end_to_end_inverted_roadmap(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-inv"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 2, blocked_by=[f"{run_id}-2"])
    _write_stub(handoffs / f"{run_id}-2.md", run_id, f"{run_id}-2", 1, 3)
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    joined = "\n".join(stderr_lines)
    assert "Audit 5: dependency-order violation" in joined
    assert f"{run_id}-1" in joined


def test_run_audit_end_to_end_cyclic_roadmap(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-cyc"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1, blocked_by=[f"{run_id}-2"])
    _write_stub(handoffs / f"{run_id}-2.md", run_id, f"{run_id}-2", 1, 2, blocked_by=[f"{run_id}-1"])
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("dependency cycle detected" in line for line in stderr_lines)


def test_run_audit_dead_gate_zero_zero_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-deadgate"
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 0)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("dead-gate signature" in line for line in stderr_lines)


def test_run_audit_missing_reconciliation_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-norecon"

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("reconciliation.md not found" in line for line in stderr_lines)


def test_run_audit_ready_to_fire_uniqueness_violation(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-ready"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1,
        deployment_state="ready_to_fire",
    )
    _write_stub(
        handoffs / f"{run_id}-2.md", run_id, f"{run_id}-2", 1, 1,
        deployment_state="ready_to_fire",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("Multiple ready_to_fire stubs" in line for line in stderr_lines)


def test_run_audit_pm_gates_cross_reference_missing(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-pmgate"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1,
        deployment_state="awaiting_gate",
        gate_dependency="PM approve scope",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("pm-gates.md missing" in line for line in stderr_lines)


def test_run_audit_pm_gates_cross_reference_present(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-pmgate-ok"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1,
        deployment_state="awaiting_gate",
        gate_dependency="PM approve scope",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)
    pmg_dir = root / "state" / "roadmap" / run_id
    pmg_dir.mkdir(parents=True, exist_ok=True)
    (pmg_dir / "pm-gates.md").write_text(
        f"| stub_id | resolved? |\n| {run_id}-1 | pending |\n", encoding="utf-8"
    )

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("pm-gates.md cross-references" in line for line in stdout_lines)


def test_run_audit_pending_row_without_matching_stub_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-pending"
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 0)
    pmg_dir = root / "state" / "roadmap" / run_id
    pmg_dir.mkdir(parents=True, exist_ok=True)
    # stub_id must match the narrow ^[a-z]+-[0-9]+$ shape (awk-port regex, preserved
    # exactly from the bash oracle) to be extracted as a pending-row candidate at all —
    # a multi-hyphen id (e.g. embedding the run_id) would not match, so use a bare
    # single-word-then-digit id here, decoupled from run_id.
    (pmg_dir / "pm-gates.md").write_text(
        "| sprint | stub_id | resolved? |\n| 1 | ghost-1 | pending |\n",
        encoding="utf-8",
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("no stub with that stub_id exists" in line for line in stderr_lines)


# ---------------------------------------------------------------------------
# Canonical `kind: roadmap-baton` spelling — Review: code-reviewer (P1,
# Finding 1). Every fixture above seeds the RETIRED `kind: spinoff-roadmap`
# spelling, which already matched the pre-fix hardcoded `kind=spinoff-roadmap`
# literal — so none of them exercise `_ROADMAP_BATON_KIND_WHERE`'s `kind in
# (...)` term against the canonical spelling the live defect was about. These
# tests seed `kind: roadmap-baton` (and one dual-spelling mix) and assert the
# audit paths find them — the exact regression the live defect would
# reintroduce if the `where=` fix were ever reverted.
# ---------------------------------------------------------------------------


def test_run_audit_end_to_end_canonical_kind_roadmap_baton(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-canonical"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1, kind="roadmap-baton"
    )
    _write_stub(
        handoffs / f"{run_id}-2.md",
        run_id,
        f"{run_id}-2",
        1,
        2,
        blocked_by=[f"{run_id}-1"],
        kind="roadmap-baton",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("Stub-coverage: 2 stubs" in line for line in stdout_lines)
    assert any("Audit 5:" in line and line.startswith("PASS:") for line in stdout_lines)


def test_run_audit_canonical_kind_ready_to_fire_uniqueness_violation(
    tmp_path: Path,
) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-canonical-ready"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1,
        deployment_state="ready_to_fire", kind="roadmap-baton",
    )
    _write_stub(
        handoffs / f"{run_id}-2.md", run_id, f"{run_id}-2", 1, 1,
        deployment_state="ready_to_fire", kind="roadmap-baton",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("Multiple ready_to_fire stubs" in line for line in stderr_lines)


def test_run_audit_dual_spelling_both_legacy_and_canonical_kind_found(
    tmp_path: Path,
) -> None:
    # Dual-acceptance is the contract: a mid-migration roadmap can carry BOTH
    # spellings across its stub set, and the audit must count both.
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-dual-spelling"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1, kind="spinoff-roadmap"
    )
    _write_stub(
        handoffs / f"{run_id}-2.md",
        run_id,
        f"{run_id}-2",
        1,
        2,
        blocked_by=[f"{run_id}-1"],
        kind="roadmap-baton",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("Stub-coverage: 2 stubs" in line for line in stdout_lines)


# ---------------------------------------------------------------------------
# resolve_repo_root / _state_root / _claude_klabauter_root / resolve_data_root —
# Review: code-reviewer (P2) — the DATA_ROOT/state-root resolution chain had
# zero direct unit tests despite ~50 lines of module docstring justifying it
# as a genuine correctness fix over the oracle. A test exercising
# resolve_repo_root against a non-git tmp_path would have caught the P1
# silent-cwd-fallback bug this review found and the first pass fixed.
# ---------------------------------------------------------------------------


def test_resolve_repo_root_non_git_dir_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Not inside a git repository"):
        resolve_repo_root(tmp_path)


def test_resolve_repo_root_git_dir_returns_toplevel(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    nested = tmp_path / "sub" / "dir"
    nested.mkdir(parents=True)

    result = resolve_repo_root(nested)

    assert Path(os.path.realpath(result)) == Path(os.path.realpath(tmp_path))


def test_same_path_true_for_equivalent_paths(tmp_path: Path) -> None:
    assert _same_path(str(tmp_path), str(tmp_path) + os.sep) is True


def test_same_path_false_for_distinct_paths(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert _same_path(str(tmp_path), str(other)) is False


def test_state_root_sibling_repo_is_repo_root_slash_state(tmp_path: Path) -> None:
    repo_root = tmp_path / "sibling-repo"
    repo_root.mkdir()
    assert _state_root(repo_root) == repo_root / "state"


def test_state_root_meta_repo_resolves_via_engine_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude_home = tmp_path / "fake-claude-home"
    fake_claude_home.mkdir()
    fake_claude_klabauter = tmp_path / "fake-claude-klabauter-repo"
    fake_claude_klabauter.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_claude_home))
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(fake_claude_klabauter))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    assert _state_root(fake_claude_home) == fake_claude_klabauter / "state"


def test_state_root_meta_repo_unresolvable_claude_klabauter_root_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude_home = tmp_path / "fake-claude-home"
    fake_claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_claude_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(tmp_path / "does-not-exist.py"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-home"))

    with pytest.raises(RuntimeError, match="CLAUDE_KLABAUTER_ROOT is"):
        _state_root(fake_claude_home)


def test_engine_root_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "/explicit/override")
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    assert _claude_klabauter_root() == "/explicit/override"


def test_retired_claude_klabauter_root_env_no_longer_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C14 closed the dual-read window: `coordinator_engine_root_env` still
    READS `CLAUDE_KLABAUTER_ROOT` (to emit the retired advisory and a census row) but
    never RETURNS it. A set-but-retired old name must not resolve, or the
    rename it completes is cosmetic."""
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "/retired/name")
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(tmp_path / "does-not-exist.py"))

    assert _claude_klabauter_root() is None


def test_engine_root_wins_while_both_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Precedence is load-bearing: a stale `CLAUDE_KLABAUTER_ROOT` inherited from an
    ancestor process must never override a fresh `COORDINATOR_ENGINE_ROOT`
    set by the immediate parent."""
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "/stale/ancestor")
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "/fresh/parent")
    assert _claude_klabauter_root() == "/fresh/parent"


def test_claude_klabauter_root_pointer_file_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review: code-reviewer (P2) — rung-1.5 pointer-file fast path (Windows
    # hook-latency fix, ported from coordinator-claude-klabauter-root.sh) must resolve
    # without ever invoking the subprocess-based machine-local ladder.
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    (settings_home / "machine-local" / ".claude-klabauter-live-root").write_text(
        "/pointer/resolved/claude-klabauter\n", encoding="utf-8"
    )
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(tmp_path / "does-not-exist.py"))

    assert _claude_klabauter_root() == "/pointer/resolved/claude-klabauter"


def test_claude_klabauter_root_none_when_fully_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(tmp_path / "does-not-exist.py"))

    assert _claude_klabauter_root() is None


def test_machine_local_get_missing_impl_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(tmp_path / "does-not-exist.py"))
    assert _machine_local_get("repos.claude_klabauter") is None


def test_resolve_data_root_root_flag_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit-root"
    assert resolve_data_root(str(explicit)) == explicit


def test_resolve_data_root_derives_from_cwd_repo_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "sibling-repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)

    result = resolve_data_root(None, cwd=repo_root)

    assert result == repo_root


# ---------------------------------------------------------------------------
# main() CLI/argv layer — Review: code-reviewer (P2) — no test invoked main()
# directly; the usage/exit-2 branches, --root consumption, and the exit-3
# hard-error path were all unexercised end-to-end.
# ---------------------------------------------------------------------------


def test_main_missing_run_id_prints_usage_and_exits_2(capsys: pytest.CaptureFixture) -> None:
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "Usage: audit-roadmap" in err


def test_main_invalid_run_id_shape_exits_2(capsys: pytest.CaptureFixture) -> None:
    assert main(["Bad-Id"]) == 2
    err = capsys.readouterr().err
    assert "must match" in err


def test_main_root_flag_missing_argument_exits_2(capsys: pytest.CaptureFixture) -> None:
    assert main(["good-run-id", "--root"]) == 2
    err = capsys.readouterr().err
    assert "--root requires a directory argument" in err


def test_main_unexpected_argument_exits_2(capsys: pytest.CaptureFixture) -> None:
    assert main(["good-run-id", "--bogus"]) == 2
    err = capsys.readouterr().err
    assert "unexpected argument: --bogus" in err


def test_main_root_flag_consumed_runs_audit(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-cli-root"
    _write_stub(root / "state" / "handoffs" / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1)
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    assert main([run_id, "--root", str(root)]) == 0


def test_main_config_error_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review: code-reviewer (P2) — a foreseeable config gap (unresolvable
    # CLAUDE_KLABAUTER_ROOT) is a usage/config error, exit 1 — not this module's own
    # documented exit-3 "unexpected internal error" contract.
    def _boom(root_flag: Optional[str], cwd: Optional[Path] = None) -> Path:
        raise RuntimeError("audit-roadmap: repo_root is the meta-repo but CLAUDE_KLABAUTER_ROOT is unresolvable")

    monkeypatch.setattr("coordinator_core.roadmap.audit.resolve_data_root", _boom)

    assert main(["good-run-id"]) == 1


def test_main_hard_error_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-hard-error"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated unexpected IO failure")

    monkeypatch.setattr("coordinator_core.roadmap.audit.query_records", _boom)

    assert main([run_id, "--root", str(root)]) == 3
    err = capsys.readouterr().err
    assert "hard error while auditing" in err


# ---------------------------------------------------------------------------
# DR-172 succession — a superseded baton leaves TWO records carrying ONE
# `stub_id` (archived predecessor + live successor) permanently. Audit 1 counts
# coverage per STUB, not per record, so the pair must read as one stub. Summing
# record counts made this audit fail forever after the first succession and
# blocked Phase 3 dispatch on a roadmap whose graph was sound.
# Source: cross-repo/inbox/2026-08-18-doe-claude-em-roadmap-baton-succession-refusal-comes-out.md § ask 3
# ---------------------------------------------------------------------------


def test_audit1_succession_pair_counts_as_one_stub(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-succession"
    handoffs = root / "state" / "handoffs"
    archived = root / "archive" / "handoffs"

    # Two distinct stubs, one of which has undergone a succession: its
    # predecessor sits in archive/ and its successor in state/, both carrying
    # `stub_id: <run_id>-1`. Three records, two stubs, two KEEP verdicts.
    _write_stub(
        archived / f"{run_id}-1-predecessor.md",
        run_id,
        f"{run_id}-1",
        1,
        1,
        deployment_state="continued",
        kind="roadmap-baton",
    )
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1, kind="roadmap-baton"
    )
    _write_stub(
        handoffs / f"{run_id}-2.md",
        run_id,
        f"{run_id}-2",
        1,
        2,
        blocked_by=[f"{run_id}-1"],
        kind="roadmap-baton",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 2)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("Stub-coverage: 2 stubs" in line for line in stdout_lines), stdout_lines
    # The record count is still reported — the succession is visible, not hidden.
    assert any("3 record(s)" in line for line in stdout_lines), stdout_lines
