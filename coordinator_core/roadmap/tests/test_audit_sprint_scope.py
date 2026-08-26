"""
coordinator_core.roadmap.tests.test_audit_sprint_scope — regression net for
the C4 sprint-scoped audit-roadmap mode (Audits 1, 3, 5).

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md § C4

Test each audit's sprint-scoped and whole-roadmap arms SEPARATELY (per the
chunk's own body) — a shared green would hide which one moved.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.roadmap.audit import (
    check_cross_sprint_edge_order,
    main,
    read_spine,
    run_audit,
)

pytestmark = [pytest.mark.cadence]

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_stub(
    path: Path,
    roadmap_id: str,
    stub_id: str,
    sprint: Optional[int] = None,
    wave: Optional[int] = None,
    deployment_state: str = "active",
    gate_dependency: Optional[str] = None,
    number: Optional[int] = None,
) -> None:
    num = number if number is not None else int(stub_id.rsplit("-", 1)[-1])
    lines = [
        "---",
        f'title: "Test stub {stub_id}"',
        "created: 2026-07-02",
        "status: active",
        f"deployment_state: {deployment_state}",
        "kind: roadmap-baton",
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
    lines.append("blocked_by: []")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_reconciliation(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Verdict: KEEP\n" * count, encoding="utf-8")


def _write_spine(
    path: Path,
    roadmap_id: str,
    sprints: List[dict],
    cross_sprint_edges: Optional[List[dict]] = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        'title: "Test spine"',
        "created: 2026-08-21",
        "kind: roadmap-spine",
        f"roadmap_id: {roadmap_id}",
        "synthesis: docs/research/synthesis.md",
        "sprints:",
    ]
    for s in sprints:
        lines.append(f"  - id: {s['id']}")
        lines.append(f"    ordinal: {s['ordinal']}")
        lines.append(f'    jtbd: "{s.get("jtbd", "do the thing")}"')
        lines.append(f'    exit_condition: "{s.get("exit_condition", "done")}"')
        if "stubs" in s:
            if s["stubs"]:
                lines.append("    stubs:")
                for stub_id in s["stubs"]:
                    lines.append(f"      - {stub_id}")
            else:
                lines.append("    stubs: []")
    edges = cross_sprint_edges or []
    if edges:
        lines.append("cross_sprint_edges:")
        for e in edges:
            lines.append(f"  - from: {e['from']}")
            lines.append(f"    to: {e['to']}")
    else:
        lines.append("cross_sprint_edges: []")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _init_tree(tmp_path: Path) -> Path:
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# read_spine / check_cross_sprint_edge_order — unit level
# ---------------------------------------------------------------------------


def test_read_spine_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert read_spine(tmp_path / "SPINE.md") is None


def test_read_spine_returns_none_for_wrong_kind(tmp_path: Path) -> None:
    p = tmp_path / "SPINE.md"
    p.write_text("---\nkind: something-else\n---\n", encoding="utf-8")
    assert read_spine(p) is None


def test_read_spine_parses_nested_sprints_and_edges(tmp_path: Path) -> None:
    p = tmp_path / "SPINE.md"
    _write_spine(
        p,
        "rm-1",
        [
            {"id": "sprint-a", "ordinal": 1, "stubs": ["rm-1-1"]},
            {"id": "sprint-b", "ordinal": 2, "stubs": []},
        ],
        cross_sprint_edges=[{"from": "sprint-a", "to": "sprint-b"}],
    )
    spine = read_spine(p)
    assert spine is not None
    assert spine["roadmap_id"] == "rm-1"
    assert len(spine["sprints"]) == 2
    assert spine["sprints"][0]["stubs"] == ["rm-1-1"]
    assert spine["sprints"][1]["stubs"] == []
    assert spine["cross_sprint_edges"] == [{"from": "sprint-a", "to": "sprint-b"}]


def test_check_cross_sprint_edge_order_good() -> None:
    spine = {
        "sprints": [{"id": "sprint-a", "ordinal": 1}, {"id": "sprint-b", "ordinal": 2}],
        "cross_sprint_edges": [{"from": "sprint-a", "to": "sprint-b"}],
    }
    result = check_cross_sprint_edge_order(spine)
    assert result["ok"] is True
    assert result["violations"] == []
    assert result["unresolved"] == []
    assert result["cycle"] is None


def test_check_cross_sprint_edge_order_inverted() -> None:
    spine = {
        "sprints": [{"id": "sprint-a", "ordinal": 1}, {"id": "sprint-b", "ordinal": 2}],
        "cross_sprint_edges": [{"from": "sprint-b", "to": "sprint-a"}],
    }
    result = check_cross_sprint_edge_order(spine)
    assert result["ok"] is False
    assert len(result["violations"]) == 1
    assert result["violations"][0]["from"] == "sprint-b"
    assert result["violations"][0]["to"] == "sprint-a"


def test_check_cross_sprint_edge_order_unresolved_sprint() -> None:
    spine = {
        "sprints": [{"id": "sprint-a", "ordinal": 1}],
        "cross_sprint_edges": [{"from": "sprint-a", "to": "sprint-ghost"}],
    }
    result = check_cross_sprint_edge_order(spine)
    assert result["ok"] is False
    assert result["unresolved"] == [{"from": "sprint-a", "to": "sprint-ghost", "which": "to"}]


def test_check_cross_sprint_edge_order_cycle() -> None:
    spine = {
        "sprints": [
            {"id": "sprint-a", "ordinal": 1},
            {"id": "sprint-b", "ordinal": 2},
            {"id": "sprint-c", "ordinal": 3},
        ],
        "cross_sprint_edges": [
            {"from": "sprint-a", "to": "sprint-b"},
            {"from": "sprint-b", "to": "sprint-c"},
            {"from": "sprint-c", "to": "sprint-a"},
        ],
    }
    result = check_cross_sprint_edge_order(spine)
    assert result["ok"] is False
    assert result["cycle"] is not None


# ---------------------------------------------------------------------------
# run_audit(sprint_id=...) end-to-end — sprint-scoped arm
# ---------------------------------------------------------------------------


def test_sprint_scoped_good_roadmap_passes(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-good"
    handoffs = root / "state" / "handoffs"
    # sprint-1's own cluster: one stub
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", sprint=1, wave=1)
    # a stub belonging to a LATER, not-yet-planned sprint — must not count
    # against sprint-1's coverage.
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [
            {"id": "sprint-a", "ordinal": 1, "stubs": [f"{run_id}-1"]},
            {"id": "sprint-b", "ordinal": 2},  # ABSENT stubs — not planned yet
        ],
        cross_sprint_edges=[{"from": "sprint-a", "to": "sprint-b"}],
    )
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 1
    )

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state", sprint_id="sprint-a")

    assert exit_code == 0, "\n".join(stderr_lines)
    assert any("Audit 5 (sprint-scoped" in line and line.startswith("PASS:") for line in stdout_lines)
    assert stderr_lines == []


def test_sprint_scoped_whole_roadmap_would_have_false_failed(tmp_path: Path) -> None:
    """The exact false-violation this mode exists to avoid: a whole-roadmap
    run before the last sprint lands sees sprint-b's absent stubs and a
    reconciliation.md that only covers sprint-a, and reports a mismatch —
    while the sprint-scoped run on sprint-a alone is clean."""
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-contrast"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", sprint=1, wave=1)
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [
            {"id": "sprint-a", "ordinal": 1, "stubs": [f"{run_id}-1"]},
            {"id": "sprint-b", "ordinal": 2},
        ],
        cross_sprint_edges=[{"from": "sprint-a", "to": "sprint-b"}],
    )
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 1
    )
    # No roadmap-root reconciliation.md at all — the whole-roadmap arm fails loud.
    exit_code_whole, _stdout_whole, stderr_whole = run_audit(run_id, root, root / "state")
    assert exit_code_whole == 1
    assert any("reconciliation.md not found" in line for line in stderr_whole)

    exit_code_sprint, _stdout_sprint, stderr_sprint = run_audit(
        run_id, root, root / "state", sprint_id="sprint-a"
    )
    assert exit_code_sprint == 0, "\n".join(stderr_sprint)


def test_sprint_scoped_coverage_mismatch_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-mismatch"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", sprint=1, wave=1)
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [{"id": "sprint-a", "ordinal": 1, "stubs": [f"{run_id}-1"]}],
    )
    # Reconciliation claims 2 KEEP verdicts but only 1 stub belongs to the sprint.
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 2
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(
        run_id, root, root / "state", sprint_id="sprint-a"
    )

    assert exit_code == 1
    assert any("Stub-coverage sprint=sprint-a mismatch" in line for line in stderr_lines)


def test_sprint_scoped_missing_spine_fails_loud(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-nospine"

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state", sprint_id="sprint-a")

    assert exit_code == 1
    assert any("SPINE.md not found" in line for line in stderr_lines)


def test_sprint_scoped_unknown_sprint_id_fails_loud(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-unknown"
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(spine_path, run_id, [{"id": "sprint-a", "ordinal": 1, "stubs": []}])

    exit_code, _stdout_lines, stderr_lines = run_audit(
        run_id, root, root / "state", sprint_id="sprint-ghost"
    )

    assert exit_code == 1
    assert any("sprint_id='sprint-ghost' not found" in line for line in stderr_lines)


def test_sprint_scoped_pm_gates_cross_reference_missing(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-pmgmissing"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md",
        run_id,
        f"{run_id}-1",
        sprint=1,
        wave=1,
        deployment_state="awaiting_gate",
        gate_dependency="PM approve budget",
    )
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(spine_path, run_id, [{"id": "sprint-a", "ordinal": 1, "stubs": [f"{run_id}-1"]}])
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 1
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state", sprint_id="sprint-a")

    assert exit_code == 1
    assert any("pm-gates.md missing" in line for line in stderr_lines)


def test_sprint_scoped_pm_gates_cross_reference_present(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-pmgok"
    handoffs = root / "state" / "handoffs"
    _write_stub(
        handoffs / f"{run_id}-1.md",
        run_id,
        f"{run_id}-1",
        sprint=1,
        wave=1,
        deployment_state="awaiting_gate",
        gate_dependency="PM approve budget",
    )
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(spine_path, run_id, [{"id": "sprint-a", "ordinal": 1, "stubs": [f"{run_id}-1"]}])
    sprint_dir = root / "state" / "roadmap" / run_id / "sprint-1"
    _write_reconciliation(sprint_dir / "reconciliation.md", 1)
    sprint_dir.mkdir(parents=True, exist_ok=True)
    (sprint_dir / "pm-gates.md").write_text(
        f"| {run_id}-1 | pending |\n", encoding="utf-8"
    )

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state", sprint_id="sprint-a")

    assert exit_code == 0, "\n".join(stderr_lines)
    assert any("pm-gates.md cross-references (sprint=sprint-a)" in line for line in stdout_lines)


def test_sprint_scoped_pm_gates_scoped_out_of_other_sprint(tmp_path: Path) -> None:
    """A stub belonging to a DIFFERENT sprint's cluster must not force this
    sprint's audit to require it in this sprint's own pm-gates.md."""
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-pmgscope"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", sprint=1, wave=1)
    _write_stub(
        handoffs / f"{run_id}-2.md",
        run_id,
        f"{run_id}-2",
        sprint=2,
        wave=1,
        deployment_state="awaiting_gate",
        gate_dependency="PM approve budget",
    )
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [
            {"id": "sprint-a", "ordinal": 1, "stubs": [f"{run_id}-1"]},
            {"id": "sprint-b", "ordinal": 2, "stubs": [f"{run_id}-2"]},
        ],
    )
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 1
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state", sprint_id="sprint-a")

    assert exit_code == 0, "\n".join(stderr_lines)


def test_sprint_scoped_cross_sprint_edge_violation_fails(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-sprint-edgebad"
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [
            {"id": "sprint-a", "ordinal": 1, "stubs": []},
            {"id": "sprint-b", "ordinal": 2, "stubs": []},
        ],
        cross_sprint_edges=[{"from": "sprint-b", "to": "sprint-a"}],
    )
    _write_reconciliation(
        root / "state" / "roadmap" / run_id / "sprint-1" / "reconciliation.md", 0
    )

    exit_code, _stdout_lines, stderr_lines = run_audit(run_id, root, root / "state", sprint_id="sprint-a")

    assert exit_code == 1
    assert any("Audit 5 (sprint-scoped" in line and "cross-sprint edge violation" in line for line in stderr_lines)


# ---------------------------------------------------------------------------
# Whole-roadmap arm — unaffected by the sprint_id=None default (byte-parity)
# ---------------------------------------------------------------------------


def test_whole_roadmap_arm_unaffected_by_sprint_scoped_addition(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "zzz-whole-unaffected"
    handoffs = root / "state" / "handoffs"
    _write_stub(handoffs / f"{run_id}-1.md", run_id, f"{run_id}-1", sprint=1, wave=1)
    _write_reconciliation(root / "state" / "roadmap" / run_id / "reconciliation.md", 1)

    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")

    assert exit_code == 0
    assert stderr_lines == []
    assert any(line == f"Stub-coverage: 1 stubs across 1 record(s) (1 live + 0 archived) match 1 verdicts (KEEP=1, MERGE=0)." or "Stub-coverage:" in line for line in stdout_lines)


# ---------------------------------------------------------------------------
# CLI main() — --sprint flag parsing
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
def test_main_sprint_flag_requires_value() -> None:
    exit_code = main(["some-run-id", "--sprint"])
    assert exit_code == 2


@pytest.mark.spawns_process
def test_main_rejects_unexpected_extra_argument() -> None:
    exit_code = main(["some-run-id", "--bogus"])
    assert exit_code == 2


def test_absent_stubs_skips_coverage_rather_than_false_failing(tmp_path: Path) -> None:
    """ABSENT `sprints[].stubs` means sprint-planning has not run: reconciliation.md
    legitimately does not exist, so Audit 1 must SKIP rather than land the
    both-sides-zero dead-gate fail. Regression pin — the pre-fix code read
    `sprint.get("stubs") or []`, which collapsed ABSENT into `[]` and failed here.
    """
    root = _init_tree(tmp_path)
    run_id = "zzz-stubs-absent"
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [{"id": "sprint-a", "ordinal": 1}],  # no `stubs` key at all
    )

    exit_code, stdout_lines, stderr_lines = run_audit(
        run_id, root, root / "state", sprint_id="sprint-a"
    )

    assert exit_code == 0, "\n".join(stderr_lines)
    assert any("ABSENT" in line and line.startswith("PASS:") for line in stdout_lines), stdout_lines
    assert not any("dead-gate signature" in line for line in stdout_lines + stderr_lines)


def test_authored_empty_stubs_is_a_finding_not_a_skip(tmp_path: Path) -> None:
    """`stubs: []` is the opposite fact: sprint-planning RAN and authored none.
    That must still reach Audit 1 and fail, or the audit reports clean on exactly
    the case worth catching.
    """
    root = _init_tree(tmp_path)
    run_id = "zzz-stubs-empty"
    spine_path = root / "state" / "roadmap" / run_id / "SPINE.md"
    _write_spine(
        spine_path,
        run_id,
        [{"id": "sprint-a", "ordinal": 1, "stubs": []}],
    )

    exit_code, stdout_lines, stderr_lines = run_audit(
        run_id, root, root / "state", sprint_id="sprint-a"
    )

    assert exit_code != 0, "authored-empty stubs must not pass as a skip"
    assert not any("ABSENT" in line for line in stdout_lines)
