"""
coordinator_core.roadmap.tests.test_audit_write_set_disjointness — regression
net for Audit 6 (`derive_write_set` / `_audit6_write_set_disjointness`).

Spec backlink: docs/plans/2026-09-12-audit-roadmap-derives-its-write-set-
from.md § Tasks C2. Each case pins a behaviour the plan's Anti-scope says
would otherwise break: reusing `plan_gate`'s resolver and its refusals,
counting every row whose work has not run (including one blocked only by a
gated predecessor, which `read_spine`'s `exclusions` list never names),
filtering superseded plans and per-plan malformed spines, and the mandatory
coverage line (§ The three questions).

Zero spawns; every case builds its corpus under `tmp_path` with an explicit
`--root`-equivalent (`run_audit`/`derive_write_set` both take `data_root`
directly, no CLI/rooting involved).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.roadmap.audit import (
    _audit6_write_set_disjointness,
    _Reporter,
    derive_write_set,
    run_audit,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _init_tree(tmp_path: Path) -> Path:
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    return tmp_path


def _baton(
    root: Path,
    stub_id: str,
    roadmap_id: str,
    *,
    governing_plan: Optional[str] = None,
    sizing_object: Optional[str] = None,
    archived: bool = False,
) -> None:
    lines = [
        "---",
        f'title: "Test baton {stub_id}"',
        "created: 2026-09-12",
        "status: active",
        "deployment_state: ready_to_fire",
        "kind: roadmap-baton",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
        "baton_role: work",
    ]
    if governing_plan is not None:
        lines.append(f"governing_plan: {governing_plan}")
    if sizing_object is not None:
        lines.append(f"sizing_object: {sizing_object}")
    lines.append("---")
    lines.append("")
    subdir = "archive/handoffs" if archived else "state/handoffs"
    (root / subdir / f"{stub_id}.md").write_text("\n".join(lines), encoding="utf-8")


def _plan_row(
    row_id: str,
    writes: Optional[List[str]] = None,
    *,
    writes_under: Optional[List[str]] = None,
    disposition: Optional[str] = None,
    deferred: Optional[bool] = None,
    execution_mode: Optional[str] = None,
    external_gate: Optional[List[Dict[str, Any]]] = None,
    depends_on: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": row_id,
        "title": f"Row {row_id}",
        "change_kind": "script-edit",
        "surface": f"src/{row_id}.py",
        "queue_scope": "project",
        "body": "Do the thing.",
    }
    if writes is not None:
        row["writes"] = writes
    if writes_under is not None:
        row["writes_under"] = writes_under
    if disposition is not None:
        row["disposition"] = disposition
    if deferred is not None:
        row["deferred"] = deferred
    if execution_mode is not None:
        row["execution_mode"] = execution_mode
    if external_gate is not None:
        row["external_gate"] = external_gate
    if depends_on is not None:
        row["depends_on"] = depends_on
    return row


def _plan(
    root: Path,
    slug: str,
    status: str,
    rows: List[Dict[str, Any]],
    *,
    sizing_object: Optional[str] = None,
) -> str:
    fm_lines = [
        "---",
        f'title: "{slug}"',
        f"status: {status}",
    ]
    if sizing_object is not None:
        fm_lines.append(f"sizing_object: {sizing_object}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append("# " + slug)
    fm_lines.append("")
    fm_lines.append("## Tasks")
    fm_lines.append("")
    fm_lines.append("```yaml plan-tasks")
    fm_lines.append(yaml.safe_dump(rows, sort_keys=False).rstrip("\n"))
    fm_lines.append("```")
    fm_lines.append("")
    rel = f"docs/plans/{slug}.md"
    (root / rel).write_text("\n".join(fm_lines), encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# Two live plans declaring one path -> collision
# ---------------------------------------------------------------------------


def test_two_live_plans_declaring_one_path_collide(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-collide"
    plan_a = _plan(root, "plan-a", "approved", [_plan_row("C1", ["shared/thing.py"])])
    plan_b = _plan(root, "plan-b", "approved", [_plan_row("C1", ["shared/thing.py"])])
    _baton(root, "baton-a", run_id, governing_plan=plan_a)
    _baton(root, "baton-b", run_id, governing_plan=plan_b)

    result = derive_write_set(run_id, root, root)
    assert result["paths"]["shared/thing.py"] == sorted([plan_a, plan_b])

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 1
    assert any(
        "shared/thing.py" in line and plan_a in line and plan_b in line
        for line in r.stderr_lines
    )


# ---------------------------------------------------------------------------
# The superseded false positive
# ---------------------------------------------------------------------------


def test_sharing_only_with_a_superseded_plan_passes(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-superseded"
    plan_live = _plan(root, "plan-live", "approved", [_plan_row("C1", ["shared/thing.py"])])
    plan_dead = _plan(root, "plan-dead", "superseded", [_plan_row("C1", ["shared/thing.py"])])
    _baton(root, "baton-live", run_id, governing_plan=plan_live)
    _baton(root, "baton-dead", run_id, governing_plan=plan_dead)

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0
    assert r.stderr_lines == []


# ---------------------------------------------------------------------------
# Within-plan overlap is not a collision
# ---------------------------------------------------------------------------


def test_within_plan_overlap_passes(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-within-plan"
    plan = _plan(
        root,
        "plan-solo",
        "approved",
        [
            _plan_row("C1", ["shared/thing.py"]),
            _plan_row("C2", ["shared/thing.py"]),
        ],
    )
    _baton(root, "baton-solo", run_id, governing_plan=plan)

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0
    assert r.stderr_lines == []


# ---------------------------------------------------------------------------
# The weak-basis decline: no manufactured collision between siblings
# ---------------------------------------------------------------------------


def test_weak_sizing_object_basis_does_not_manufacture_a_collision(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-weak-basis"
    plan_x = _plan(
        root, "plan-x", "approved", [_plan_row("C1", ["only/in/x.py"])],
        sizing_object="shared-sizing",
    )
    plan_y = _plan(
        root, "plan-y", "approved", [_plan_row("C1", ["only/in/y.py"])],
        sizing_object="shared-sizing",
    )
    # Neither baton links via governing_plan -- both resolve ONLY via the
    # shared sizing_object, a _WEAK_PLAN_LINK_BASES member. Each baton's
    # `link_plans` hit set is therefore [plan_x, plan_y] (both cite the same
    # sizing object), which `_best_plan` must decline rather than guess.
    _baton(root, "baton-x", run_id, sizing_object="shared-sizing")
    _baton(root, "baton-y", run_id, sizing_object="shared-sizing")

    result = derive_write_set(run_id, root, root)
    assert result["paths"] == {}
    assert sorted(result["unresolved_stub_ids"]) == ["baton-x", "baton-y"]

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0
    assert r.stderr_lines == []


# ---------------------------------------------------------------------------
# Path normalization: backslash vs slash collide
# ---------------------------------------------------------------------------


def test_backslash_and_slash_spelled_paths_collide(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-normalize"
    plan_a = _plan(root, "plan-win", "approved", [_plan_row("C1", ["shared\\thing.py"])])
    plan_b = _plan(root, "plan-mac", "approved", [_plan_row("C1", ["shared/thing.py"])])
    _baton(root, "baton-win", run_id, governing_plan=plan_a)
    _baton(root, "baton-mac", run_id, governing_plan=plan_b)

    result = derive_write_set(run_id, root, root)
    assert "shared/thing.py" in result["paths"]
    assert sorted(result["paths"]["shared/thing.py"]) == sorted([plan_a, plan_b])

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 1


# ---------------------------------------------------------------------------
# Gated / operator rows still contribute their writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row_kwargs",
    [
        pytest.param({"external_gate": [{"blocks": "execution"}]}, id="uncleared-gate"),
        pytest.param({"execution_mode": "operator"}, id="operator"),
    ],
)
def test_a_row_whose_work_has_not_run_still_contributes(
    tmp_path: Path, row_kwargs: Dict[str, Any]
) -> None:
    """Both classes reach the write set through the SAME fall-through.

    Parametrized rather than written twice because the implementation reads
    neither key: the rule is "count it unless its work has landed or will
    never happen", so a gated row and an operator row are one behaviour seen
    from two doors. They are both kept as cases because they are the two row
    shapes a reader will ask about, and a rule change that special-cased
    either would have to break this test to do it.
    """
    root = _init_tree(tmp_path)
    run_id = "roadmap-not-run"
    plan_a = _plan(
        root, "plan-not-run", "approved", [_plan_row("C1", ["shared/thing.py"], **row_kwargs)]
    )
    plan_b = _plan(root, "plan-plain", "approved", [_plan_row("C1", ["shared/thing.py"])])
    _baton(root, "baton-not-run", run_id, governing_plan=plan_a)
    _baton(root, "baton-plain", run_id, governing_plan=plan_b)

    result = derive_write_set(run_id, root, root)
    assert sorted(result["paths"]["shared/thing.py"]) == sorted([plan_a, plan_b])


def test_writes_under_prefix_collides_and_stays_a_directory_claim(tmp_path: Path) -> None:
    """`writes_under:` joins the write set, in its own string space.

    Two plans declaring one directory prefix collide. A plan creating `gen/`
    and a plan writing a FILE named `gen` do not — collapsing the trailing
    separator would fuse two claims that are not the same claim.
    """
    root = _init_tree(tmp_path)
    run_id = "roadmap-under"
    plan_a = _plan(
        root, "plan-under-a", "approved", [_plan_row("C1", writes_under=["gen/"])]
    )
    plan_b = _plan(
        root, "plan-under-b", "approved", [_plan_row("C1", writes_under=["gen\\"])]
    )
    plan_c = _plan(root, "plan-file-named-gen", "approved", [_plan_row("C1", ["gen"])])
    _baton(root, "baton-under-a", run_id, governing_plan=plan_a)
    _baton(root, "baton-under-b", run_id, governing_plan=plan_b)
    _baton(root, "baton-file-gen", run_id, governing_plan=plan_c)

    result = derive_write_set(run_id, root, root)
    assert sorted(result["paths"]["gen/"]) == sorted([plan_a, plan_b])
    assert result["paths"]["gen"] == [plan_c]


def test_row_blocked_only_by_a_gated_predecessor_still_contributes(tmp_path: Path) -> None:
    """The transitive-closure leg, which an `exclusions`-keyed re-admit misses.

    `spine_read.read_spine` appends an `exclusions` entry only for a row
    excluded on its OWN properties. A row dropped by the transitive closure —
    blocked solely because something it `depends_on` carries an uncleared
    execution-blocking gate — is removed from the return value with NO entry
    appended. So a write set that re-admits by reading that list drops exactly
    the rows nobody has executed, and the list's apparent completeness hides
    the loss. C2 here has run no more than C1 has; its writes are still a live
    collision.
    """
    root = _init_tree(tmp_path)
    run_id = "roadmap-transitive"
    plan_a = _plan(
        root,
        "plan-transitive",
        "approved",
        [
            _plan_row("C1", ["gated/thing.py"], external_gate=[{"blocks": "execution"}]),
            _plan_row(
                "C2",
                ["shared/downstream.py"],
                depends_on=[{"chunk": "C1", "gate_kind": "output-consumption-runtime"}],
            ),
        ],
    )
    plan_b = _plan(root, "plan-plain2", "approved", [_plan_row("C1", ["shared/downstream.py"])])
    _baton(root, "baton-transitive", run_id, governing_plan=plan_a)
    _baton(root, "baton-plain2", run_id, governing_plan=plan_b)

    result = derive_write_set(run_id, root, root)
    assert sorted(result["paths"]["shared/downstream.py"]) == sorted([plan_a, plan_b])


def test_operator_row_still_contributes(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-operator"
    plan_a = _plan(
        root,
        "plan-operator",
        "approved",
        [_plan_row("C1", ["shared/thing.py"], execution_mode="operator")],
    )
    plan_b = _plan(root, "plan-plain2", "approved", [_plan_row("C1", ["shared/thing.py"])])
    _baton(root, "baton-operator", run_id, governing_plan=plan_a)
    _baton(root, "baton-plain2", run_id, governing_plan=plan_b)

    result = derive_write_set(run_id, root, root)
    assert sorted(result["paths"]["shared/thing.py"]) == sorted([plan_a, plan_b])


# ---------------------------------------------------------------------------
# Closed / deferred rows do NOT contribute
# ---------------------------------------------------------------------------


def test_closed_disposition_row_does_not_contribute(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-closed"
    plan_a = _plan(
        root,
        "plan-closed",
        "approved",
        [_plan_row("C1", ["shared/thing.py"], disposition="coded")],
    )
    plan_b = _plan(root, "plan-plain3", "approved", [_plan_row("C1", ["shared/thing.py"])])
    _baton(root, "baton-closed", run_id, governing_plan=plan_a)
    _baton(root, "baton-plain3", run_id, governing_plan=plan_b)

    result = derive_write_set(run_id, root, root)
    # plan_a's only row is closed (landed) -- its writes do not contribute,
    # so `shared/thing.py` is declared by plan_b alone: no collision.
    assert result["paths"].get("shared/thing.py") == [plan_b]

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0


def test_deferred_row_does_not_contribute(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-deferred"
    plan_a = _plan(
        root,
        "plan-deferred-row",
        "approved",
        [_plan_row("C1", ["shared/thing.py"], deferred=True)],
    )
    _baton(root, "baton-deferred", run_id, governing_plan=plan_a)

    result = derive_write_set(run_id, root, root)
    assert "shared/thing.py" not in result["paths"]
    # fully_resolved, NOT undeclared: the row said what it writes and that work
    # is not happening. See test_a_landed_plan_is_not_reported_as_declaring_nothing.
    assert result["undeclared_plans"] == []
    assert plan_a in result["fully_resolved_plans"]


# ---------------------------------------------------------------------------
# Coverage line
# ---------------------------------------------------------------------------


def test_coverage_line_names_unplanned_batons_on_a_clean_roadmap(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-coverage"
    plan_a = _plan(root, "plan-cov", "approved", [_plan_row("C1", ["only/one.py"])])
    _baton(root, "baton-planned", run_id, governing_plan=plan_a)
    _baton(root, "baton-unplanned", run_id)

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0
    coverage_lines = [line for line in r.stdout_lines if "Write-set disjointness" in line]
    assert any(
        "2 baton(s)" in line and "1 resolved" in line and "baton-unplanned" in line
        for line in coverage_lines
    )


# ---------------------------------------------------------------------------
# A malformed spine fails THAT plan by name; the run still reports (exit 1,
# not exit 3) and does not destroy the other audits.
# ---------------------------------------------------------------------------


def test_malformed_spine_fails_that_plan_and_run_audit_reports_exit_1(tmp_path: Path) -> None:
    root = _init_tree(tmp_path)
    run_id = "roadmap-malformed"
    plan_ok = _plan(root, "plan-ok", "approved", [_plan_row("C1", ["fine/thing.py"])])
    # A plan record with NO task-spine block at all -- read_spine raises
    # SpineReadError (ABSENT, not LOCATED).
    plan_bad_rel = "docs/plans/plan-bad.md"
    (root / plan_bad_rel).write_text(
        "---\ntitle: \"plan-bad\"\nstatus: approved\n---\n\nNo tasks here.\n",
        encoding="utf-8",
    )
    _baton(root, "baton-ok", run_id, governing_plan=plan_ok)
    _baton(root, "baton-bad", run_id, governing_plan=plan_bad_rel)

    result = derive_write_set(run_id, root, root)
    assert [plan_bad_rel, "SpineReadError"] in result["parse_failures"]
    assert result["paths"].get("fine/thing.py") == [plan_ok]

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 1
    assert any("plan-bad" in line for line in r.stderr_lines)

    # And end-to-end through run_audit: the malformed spine does not blow
    # the whole run up to exit 3 -- it is caught per-plan inside Audit 6 and
    # r.fail'd by name, same as every other audit failure.
    _write_reconciliation = root / "state" / "roadmap" / run_id / "reconciliation.md"
    _write_reconciliation.parent.mkdir(parents=True, exist_ok=True)
    _write_reconciliation.write_text("Verdict: KEEP\nVerdict: KEEP\n", encoding="utf-8")
    exit_code, stdout_lines, stderr_lines = run_audit(run_id, root, root / "state")
    assert exit_code == 1
    assert any("plan-bad" in line for line in stderr_lines)


def test_a_plan_file_that_vanished_is_named_not_silently_dropped(tmp_path: Path) -> None:
    """A baton whose `governing_plan` file is gone must still be VISIBLE.

    It does not reach the per-plan read at all: `build_plan_index` scans disk,
    so a deleted plan is simply not in the index and `link_plans` resolves
    nothing. The baton therefore lands in the unresolved bucket and is NAMED
    on the coverage line — which is the property that matters, because the
    alternative is a PASS that quietly covered one baton fewer than the reader
    believes.

    The broad `except` in `_plan_declared_write_set` is NOT what saves this
    case and is not pinned by it: it covers the narrow race where a plan is
    indexed and then removed or rewritten before it is read, plus an indexed
    file that will not decode. Both would otherwise escape to `main()` as a
    whole-run exit 3.
    """
    root = _init_tree(tmp_path)
    run_id = "roadmap-vanished"
    plan_ok = _plan(root, "plan-present", "approved", [_plan_row("C1", ["fine/thing.py"])])
    plan_gone = _plan(root, "plan-vanished", "approved", [_plan_row("C1", ["gone/thing.py"])])
    _baton(root, "baton-present", run_id, governing_plan=plan_ok)
    _baton(root, "baton-vanished", run_id, governing_plan=plan_gone)
    (root / plan_gone).unlink()

    result = derive_write_set(run_id, root, root)
    assert result["unresolved_stub_ids"] == ["baton-vanished"]
    assert "gone/thing.py" not in result["paths"]

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0
    assert any(
        "Write-set disjointness" in line and "baton-vanished" in line
        for line in r.stdout_lines
    )


def test_a_landed_plan_is_not_reported_as_declaring_nothing(tmp_path: Path) -> None:
    """Every row landed is not the same fact as no row declaring anything.

    Both leave the write set empty, so a single "declared nothing" bucket
    conflates them — and the conflation is exactly what the third report line
    exists to prevent, reappearing in the REPORTING rather than in the check.
    Found by running the shipped audit against a live roadmap
    (`fifa-substrate-coverage-2026-09-12`, example-retrieval-repo-ue-addon-em,
    2026-09-12): a `status: landed` plan carrying five rows and nine paths was
    announced as declaring no writes at all. Excluding its rows is right;
    calling it silent sends a reader to look for a missing declaration that
    was never missing.
    """
    root = _init_tree(tmp_path)
    run_id = "roadmap-landed"
    plan_landed = _plan(
        root,
        "plan-all-landed",
        "landed",
        [
            _plan_row("C1", ["done/one.py"], disposition="coded"),
            _plan_row("C2", ["done/two.py"], disposition="coded"),
        ],
    )
    plan_silent = _plan(root, "plan-silent", "approved", [_plan_row("C1")])
    _baton(root, "baton-landed", run_id, governing_plan=plan_landed)
    _baton(root, "baton-silent", run_id, governing_plan=plan_silent)

    result = derive_write_set(run_id, root, root)
    assert result["fully_resolved_plans"] == [plan_landed]
    assert result["undeclared_plans"] == [plan_silent]
    assert result["paths"] == {}

    r = _Reporter()
    _audit6_write_set_disjointness(r, run_id, root, root)
    assert r.exit_code == 0
    landed_lines = [line for line in r.stdout_lines if plan_landed in line]
    silent_lines = [line for line in r.stdout_lines if plan_silent in line]
    # The landed plan is never described as declaring nothing, and the line it
    # DOES appear on says why it contributed no paths.
    assert landed_lines and all("declaring no writes" not in line for line in landed_lines)
    assert any("has landed or will not happen" in line for line in landed_lines)
    assert any("declaring no writes" in line for line in silent_lines)
