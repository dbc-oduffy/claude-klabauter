
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core._claude_klabauter_root import clear_machine_local_cache
from coordinator_core.roadmap.audit import (
    _count_verdict,
    _claude_klabauter_root,
    _machine_local_get,
    _parse_pending_stubs,
    parse_keep_cluster_ids,
    parse_post_reconciliation_stubs,
    _same_path,
    _state_root,
    check_dependency_order,
    main,
    resolve_data_root,
    resolve_repo_root,
    run_audit,
    validate_run_id,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs


@pytest.fixture(autouse=True)
def _reset_shared_machine_local_cache() -> None:
    """The shared ``_machine_local_get`` (coordinator_core._claude_klabauter_root, R4) is
    memoized per (key, resolved impl path) at MODULE scope. Without this reset,
    a test that resolves ``repos.claude_klabauter`` under one MACHINE_LOCAL_IMPL/
    COORDINATOR_SETTINGS_HOME combination leaks its cached result into a later
    test using the same (now-stale) impl path but a different env, producing a
    false pass or false failure depending on test order."""
    clear_machine_local_cache()
    yield
    clear_machine_local_cache()

# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the route
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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
    covers: Optional[List[str]] = None,
    blocks: Optional[List[str]] = None,
    loe: Optional[str] = None,
) -> None:
    # to prove `_ROADMAP_BATON_KIND_WHERE`'s `kind in (...)` term actually
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
    if loe is not None:
        lines.append(f"loe: {loe}")
    if covers is not None:
        lines.append("covers: [" + ", ".join(covers) + "]")
    if blocked_by:
        lines.append("blocked_by:")
        for dep in blocked_by:
            lines.append(f"  - {dep}")
    else:
        lines.append("blocked_by: []")
    if blocks:
        lines.append("blocks:")
        for dep in blocks:
            lines.append(f"  - {dep}")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_reconciliation(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Verdict: KEEP\n" * count, encoding="utf-8")


def test_validate_run_id_accepts_lowercase_alnum_hyphen() -> None:
    assert validate_run_id("claude-klabauter-strangler-2026-07-04") is None


@pytest.mark.parametrize("bad", ["", "Bad-Id", "-leading-hyphen", "under_score", "UP"])
def test_validate_run_id_rejects_bad_shapes(bad: str) -> None:
    assert validate_run_id(bad) is not None


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


def test_dependency_order_blocks_dangling_edge_is_unresolved() -> None:
    stubs = [
        {"stub_id": "a-1", "number": 1, "sprint": 1, "wave": 1, "blocked_by": [], "blocks": ["zz-ghost"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is False
    assert len(result["unresolved"]) == 1
    u = result["unresolved"][0]
    assert u["to"] == "zz-ghost"
    assert u["edge"] == "blocks"


def test_dependency_order_blocks_resolved_edge_not_ordering_checked() -> None:
    stubs = [
        {"stub_id": "a-1", "number": 1, "sprint": 1, "wave": 1, "blocked_by": [], "blocks": ["a-2"]},
        {"stub_id": "a-2", "number": 2, "sprint": 1, "wave": 2, "blocked_by": []},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is True
    assert result["unresolved"] == []
    assert result["violations"] == []


def test_dependency_order_number_derived_from_stub_id_when_absent() -> None:
    stubs = [
        {"stub_id": "x-1", "sprint": 1, "wave": 1, "blocked_by": []},
        {"stub_id": "x-2", "sprint": 1, "wave": 2, "blocked_by": ["x-1"]},
    ]
    result = check_dependency_order(stubs)
    assert result["ok"] is True


def test_parse_pending_stubs_extracts_2nd_visible_column() -> None:
    pmg = "| sprint | stub_id | gate question | resolved? |\n| 2 | foo-3 | ok? | pending |\n"
    assert _parse_pending_stubs(pmg) == ["foo-3"]


def test_parse_pending_stubs_ignores_non_matching_cell() -> None:
    pmg = "| 1 | not-a-stub-id-shape | pending |\n"
    assert _parse_pending_stubs(pmg) == []


def test_parse_pending_stubs_ignores_non_pending_rows() -> None:
    pmg = "| id | foo-3 | resolved |\n"
    assert _parse_pending_stubs(pmg) == []


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


def test_run_audit_loe_band_in_band_passes(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-loe-good"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1, loe="M")
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0
    assert any("Audit 7:" in line and line.startswith("PASS:") for line in stdout_lines)
    assert stderr_lines == []


def test_run_audit_loe_band_out_of_band_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-loe-bad"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1, loe="S")
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    joined = "\n".join(stderr_lines)
    assert "Audit 7:" in joined
    assert f"{run_id}-1" in joined
    assert "loe='S'" in joined


def test_run_audit_loe_band_absent_is_pass_with_count(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-loe-absent"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1)
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0
    assert stderr_lines == []
    joined = "\n".join(stdout_lines)
    assert "Audit 7:" in joined
    assert "1 of 1 stub(s)" in joined
    assert "carry no loe:" in joined


def _write_spine_for_blocks_test(
    path: Path, roadmap_id: str, stub_ids: List[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        'title: "Test spine"',
        "created: 2026-09-23",
        "kind: roadmap-spine",
        f"roadmap_id: {roadmap_id}",
        "synthesis: docs/research/synthesis.md",
        "sprints:",
        "  - id: sprint-a",
        "    ordinal: 1",
        '    jtbd: "do the thing"',
        '    exit_condition: "done"',
        "    stubs:",
    ]
    for sid in stub_ids:
        lines.append(f"      - {sid}")
    lines.append("cross_sprint_edges: []")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_run_audit_end_to_end_dangling_blocks_edge_fails_sprint_scoped(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-blocks-sprint"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1,
        blocks=["zz-does-not-exist"], kind="roadmap-baton",
    )
    _write_spine_for_blocks_test(
        root / "state" / "roadmap" / run_id / "SPINE.md", run_id, [f"{run_id}-1"]
    )
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 1
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(
        run_id, root, root / "state", sprint_id="sprint-a"
    )

    assert exit_code == 1
    joined = "\n".join(stderr_lines)
    assert "unresolved blocks edge" in joined
    assert "zz-does-not-exist" in joined


def test_run_audit_end_to_end_dangling_blocks_edge_fails_whole_roadmap(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-e2e-blocks-dangling"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1,
        blocks=["zz-does-not-exist"], kind="roadmap-baton",
    )
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    joined = "\n".join(stderr_lines)
    assert "Audit 5: unresolved blocks edge" in joined
    assert "zz-does-not-exist" in joined


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
    (pmg_dir / "pm-gates.md").write_text(
        "| sprint | stub_id | resolved? |\n| 1 | ghost-1 | pending |\n",
        encoding="utf-8",
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("no stub with that stub_id exists" in line for line in stderr_lines)


# literal — so none of them exercise `_ROADMAP_BATON_KIND_WHERE`'s `kind in


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


# The DATA_ROOT/state-root resolution chain had


def test_resolve_repo_root_non_git_dir_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Not inside a git repository"):
        resolve_repo_root(tmp_path)


def test_resolve_repo_root_git_dir_returns_toplevel(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
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
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True, **no_console_passthrough_kwargs())

    result = resolve_data_root(None, cwd=repo_root)

    assert result == repo_root


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
    # CLAUDE_KLABAUTER_ROOT) is a usage/config error, exit 1 — not this module's own
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


def test_audit1_succession_pair_counts_as_one_stub(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-succession"
    handoffs = root / "state" / "handoffs"
    archived = root / "archive" / "handoffs"

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
    assert any("3 record(s)" in line for line in stdout_lines), stdout_lines


def test_parse_post_reconciliation_stubs_reads_id_and_provenance() -> None:
    text = (
        "| stub_id | minted | why |\n"
        "|---------|--------|-----|\n"
        "| foo-04 | 2026-07-04 | expansion cohort, clusters.md Cluster 7 |\n"
        "| foo-10-C | 2026-07-06 | sub-split of foo-10 |\n"
    )
    assert parse_post_reconciliation_stubs(text) == {
        "foo-04": "expansion cohort, clusters.md Cluster 7",
        "foo-10-C": "sub-split of foo-10",
    }


def test_parse_post_reconciliation_stubs_ignores_row_with_empty_provenance() -> None:
    text = "| stub_id | minted | why |\n| foo-04 | 2026-07-04 |  |\n"
    assert parse_post_reconciliation_stubs(text) == {}


def test_merge_verdicts_do_not_inflate_expected_stub_count(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-merge-arith"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1)
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(
        "| 1 — a | **KEEP** | → stub |\n| 2 — b | **MERGE** | folds into 1, no stub |\n",
        encoding="utf-8",
    )

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any(
        "Stub-coverage: 1 stubs" in line and "MERGE=1" in line for line in stdout_lines
    )


def test_declared_post_reconciliation_stub_is_excluded_from_coverage(
    tmp_path: Path,
) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-post-recon"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1)
    _write_stub(handoffs / f"{run_id}-2.md", run_id, f"{run_id}-2", 1, 2)
    roadmap_dir = root / "state" / "roadmap" / run_id
    _write_reconciliation(roadmap_dir / "reconciliation.md", 1)

    exit_code, _stdout, stderr_lines = run_audit(run_id, root, root / "state")
    assert exit_code == 1
    assert any("Stub-coverage mismatch: 2 stubs" in line for line in stderr_lines)

    (roadmap_dir / "post-reconciliation-stubs.md").write_text(
        "| stub_id | minted | why |\n"
        "|---------|--------|-----|\n"
        f"| {run_id}-2 | 2026-08-01 | minted after the Phase-1 pass |\n",
        encoding="utf-8",
    )

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")
    assert exit_code == 0, stderr_lines
    assert any(
        "1 post-reconciliation stub(s) declared" in line for line in stdout_lines
    )


def test_declaration_naming_a_stub_not_on_disk_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-post-recon-orphan"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", 1, 1)
    roadmap_dir = root / "state" / "roadmap" / run_id
    _write_reconciliation(roadmap_dir / "reconciliation.md", 1)
    (roadmap_dir / "post-reconciliation-stubs.md").write_text(
        "| stub_id | minted | why |\n"
        "|---------|--------|-----|\n"
        "| ghost-99 | 2026-08-01 | never minted |\n",
        encoding="utf-8",
    )

    exit_code, _stdout, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert any("ghost-99" in line and "not on disk" in line for line in stderr_lines)

# roadmap-planning skill MANDATES folding several clusters into one baton, so
# `stub_count == keep_count` fails a conforming roadmap BY CONSTRUCTION and

_NL = chr(10)


def test_parse_keep_cluster_ids_reads_first_column_of_bolded_keep_rows() -> None:
    text = _NL.join([
        "| id | Verdict | Rationale |",
        "|---|---|---|",
        "| `cl-01` | **KEEP** | engine work |",
        "| `cl-02` | **KEEP** | mentions cl-01 in prose, must not double-count |",
        "| `cl-03` | **MOVE** | peer plane |",
        "| `cl-04` | KEEP | unbolded is not a verdict |",
    ]) + _NL
    assert parse_keep_cluster_ids(text) == ["cl-01", "cl-02"]


def test_parse_keep_cluster_ids_honours_column_3_verdict_shape() -> None:
    """Regression for overengineering-reviewer (major): `_KEEP_ROW_RE` used to
    match column 2 ONLY while `_VERDICT_TABLE_RE["KEEP"]` (the count this
    result feeds in `_audit1_stub_coverage`) accepted column 2 OR column 3 --
    a reconciliation table putting its verdict in column 3 produced a
    non-empty `keep_count` alongside an EMPTY `keep_ids`, which read as
    vacuously satisfied coverage. Both counters must now agree on the same
    table."""
    text = _NL.join([
        "| id | Cluster name | Verdict |",
        "|---|---|---|",
        "| `cl-05` | foo | **KEEP** |",
        "| `cl-06` | bar | **KEEP** |",
        "| `cl-07` | baz | **MOVE** |",
    ]) + _NL
    keep_ids = parse_keep_cluster_ids(text)
    assert keep_ids == ["cl-05", "cl-06"]
    assert len(keep_ids) == _count_verdict(text, "KEEP")


def test_one_stub_covering_several_clusters_passes_coverage(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-fold"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1,
                covers=["cl-01", "cl-02"])
    _write_stub(handoffs / (run_id + "-2.md"), run_id, run_id + "-2", 1, 2,
                covers=["cl-03"])
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(_NL.join([
        "| `cl-01` | **KEEP** | a |",
        "| `cl-02` | **KEEP** | b |",
        "| `cl-03` | **KEEP** | c |",
    ]) + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("all 3 KEEP cluster(s) named exactly once" in ln for ln in stdout_lines)


def test_uncovered_keep_cluster_fails_and_is_named(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-gap"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1,
                covers=["cl-01"])
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text(_NL.join([
        "| `cl-01` | **KEEP** | a |",
        "| `cl-02` | **KEEP** | orphaned |",
    ]) + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    joined = _NL.join(stdout_lines + stderr_lines)
    assert "cl-02" in joined and "NOT covered" in joined


def test_cluster_covered_twice_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-dupe"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1, covers=["cl-01"])
    _write_stub(handoffs / (run_id + "-2.md"), run_id, run_id + "-2", 1, 2, covers=["cl-01"])
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text("| `cl-01` | **KEEP** | a |" + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    assert "covered more than once" in _NL.join(stdout_lines + stderr_lines)


def test_repeated_id_within_one_stubs_own_covers_passes(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-self-dupe"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1,
        covers=["cl-01", "cl-01"],
    )
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text("| `cl-01` | **KEEP** | a |" + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("all 1 KEEP cluster(s) named exactly once" in ln for ln in stdout_lines)


def test_covers_entry_naming_no_keep_cluster_fails_as_unknown(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-unknown"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1,
        covers=["cl-01", "cl-99"],
    )
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text("| `cl-01` | **KEEP** | a |" + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 1
    joined = _NL.join(stdout_lines + stderr_lines)
    assert "cl-99" in joined and "carrying no KEEP verdict" in joined


def test_backtick_wrapped_covers_entry_matches_plain_table_id(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-backtick-covers"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1,
        covers=["`cl-01`"],
    )
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text("| `cl-01` | **KEEP** | a |" + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("all 1 KEEP cluster(s) named exactly once" in ln for ln in stdout_lines)


def test_roadmap_declaring_no_covers_keeps_the_legacy_count_bar(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-legacy"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / (run_id + "-1.md"), run_id, run_id + "-1", 1, 1)
    recon = root / "state" / "roadmap" / run_id / "reconciliation.md"
    recon.parent.mkdir(parents=True, exist_ok=True)
    recon.write_text("| 1 - a | **KEEP** | free-form first column |" + _NL, encoding="utf-8")

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0, stderr_lines
    assert any("Counted, not covered" in ln for ln in stdout_lines)
