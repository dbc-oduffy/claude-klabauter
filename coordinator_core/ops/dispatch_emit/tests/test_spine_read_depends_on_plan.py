"""`depends_on_plan` (a row's sibling-plan predecessor, same repo): a dependent
row is withheld until the named row carries `disposition: coded`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    DanglingPlanDependencyError,
    read_spine,
    resolve_plan_edge,
    unlanded_plan_edges,
)


def _plan(path, rows_yaml: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows_yaml + "\n```\n", encoding="utf-8"
    )
    return path


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _predecessor(repo, disposition: str):
    extra = "" if disposition == "open" else f"  disposition: {disposition}\n"
    return _plan(
        repo / "docs" / "plans" / "pred.md",
        "- id: P1\n  title: pred\n  surface: a.py\n  writes: [a.py]\n" + extra,
    )


_DEPENDENT = """\
- id: D1
  title: dependent
  surface: b.py
  writes: [b.py]
  depends_on_plan:
    - plan: docs/plans/pred.md
      chunk: P1
      gate_kind: output-consumption-runtime
- id: D2
  title: downstream of D1
  surface: c.py
  writes: [c.py]
  depends_on:
    - chunk: D1
      gate_kind: output-consumption-runtime
- id: D3
  title: free
  surface: d.py
  writes: [d.py]
"""


def test_unlanded_predecessor_withholds_row_and_its_dependents(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")
    dep = _plan(repo / "docs" / "plans" / "dep.md", _DEPENDENT)
    exclusions: list = []

    rows = read_spine(dep, exclusions=exclusions)

    assert [r.id for r in rows] == ["D3"]
    by_id = {e["id"]: e for e in exclusions}
    assert by_id["D1"]["reason"] == "depends_on_plan"
    assert "docs/plans/pred.md" in by_id["D1"]["detail"] and "P1" in by_id["D1"]["detail"]
    assert by_id["D2"]["reason"] == "transitive_gate_closure"


def test_coded_predecessor_releases_the_row(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "coded")
    dep = _plan(repo / "docs" / "plans" / "dep.md", _DEPENDENT)

    assert [r.id for r in read_spine(dep)] == ["D1", "D2", "D3"]


@pytest.mark.parametrize("disposition", ["wont_do", "backlogged", "spun_off"])
def test_terminal_non_coded_predecessor_is_dangling(tmp_path, disposition):
    repo = _repo(tmp_path)
    _predecessor(repo, disposition)
    dep = _plan(repo / "docs" / "plans" / "dep.md", _DEPENDENT)

    with pytest.raises(DanglingPlanDependencyError, match="P1"):
        read_spine(dep)


def test_missing_predecessor_plan_is_dangling(tmp_path):
    repo = _repo(tmp_path)
    dep = _plan(repo / "docs" / "plans" / "dep.md", _DEPENDENT)

    with pytest.raises(DanglingPlanDependencyError, match="docs/plans/pred.md"):
        read_spine(dep)


def test_unknown_predecessor_chunk_is_dangling(tmp_path):
    repo = _repo(tmp_path)
    _plan(repo / "docs" / "plans" / "pred.md", "- id: OTHER\n  title: x\n  surface: a.py\n  writes: [a.py]\n")
    dep = _plan(repo / "docs" / "plans" / "dep.md", _DEPENDENT)

    with pytest.raises(DanglingPlanDependencyError, match="P1"):
        read_spine(dep)


def test_parent_traversal_is_refused(tmp_path):
    repo = _repo(tmp_path)
    dep = _plan(
        repo / "docs" / "plans" / "dep.md",
        "- id: D1\n  title: t\n  surface: b.py\n  writes: [b.py]\n"
        "  depends_on_plan:\n    - plan: ../outside.md\n      chunk: P1\n"
        "      gate_kind: output-consumption-runtime\n",
    )

    with pytest.raises(DanglingPlanDependencyError, match="outside"):
        read_spine(dep)


def test_no_git_ancestor_withholds_rather_than_dispatches(tmp_path):
    dep = _plan(tmp_path / "docs" / "plans" / "dep.md", _DEPENDENT)
    exclusions: list = []

    rows = read_spine(dep, exclusions=exclusions)

    assert [r.id for r in rows] == ["D3"]
    assert {e["id"]: e["reason"] for e in exclusions}["D1"] == "depends_on_plan"


def test_done_spelling_reads_as_coded_for_the_row_and_for_a_predecessor(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "done")
    dep = _plan(
        repo / "docs" / "plans" / "dep.md",
        _DEPENDENT + "- id: D4\n  title: shipped under the old spelling\n  surface: e.py\n"
        "  writes: [e.py]\n  disposition: done\n",
    )
    exclusions: list = []

    rows = read_spine(dep, exclusions=exclusions)

    assert [r.id for r in rows] == ["D1", "D2", "D3"]
    assert {e["id"]: e["detail"] for e in exclusions}["D4"] == "disposition: coded"


_EDGE = {"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}


def test_resolve_chunk_edge_to_open_row_returns_row_and_hold(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")

    res = resolve_plan_edge("row 'D1'", _EDGE, repo, {})

    assert res.named_row["id"] == "P1"
    assert res.hold is not None and "not yet coded" in res.hold


def test_resolve_chunk_edge_to_done_row_is_coded_and_unheld(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "done")

    res = resolve_plan_edge("row 'D1'", _EDGE, repo, {})

    assert res.named_row["disposition"] == "coded"
    assert res.hold is None


def test_resolve_status_edge_has_no_named_row(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")
    edge = {"plan": "docs/plans/pred.md", "status": "approved", "gate_kind": "epistemic-premise"}

    res = resolve_plan_edge("row 'D1'", edge, repo, {})

    assert res.named_row is None


def test_resolve_backslash_plan_reads_as_slash(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")

    res = resolve_plan_edge("row 'D1'", {**_EDGE, "plan": "docs\\plans\\pred.md"}, repo, {})

    assert res.named_row["id"] == "P1"


def test_resolve_absent_plan_is_dangling(tmp_path):
    repo = _repo(tmp_path)

    with pytest.raises(DanglingPlanDependencyError):
        resolve_plan_edge("row 'D1'", _EDGE, repo, {})


def test_held_first_edge_does_not_stop_a_later_dangling_edge_raising(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")
    absent = {**_EDGE, "plan": "docs/plans/absent.md"}

    with pytest.raises(DanglingPlanDependencyError):
        unlanded_plan_edges("row 'D1'", [_EDGE, absent], repo, {})


def _archive(repo, name="pred.md", status="implemented"):
    src = repo / "docs" / "plans" / name
    dest = repo / "archive" / "specs" / "2026-10" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    dest.write_text(f"---\nstatus: {status}\n---\n" + dest.read_text(encoding="utf-8"), encoding="utf-8")


def test_chunk_edge_to_archived_plan_is_satisfied(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")
    _archive(repo)

    res = resolve_plan_edge("row 'D1'", _EDGE, repo, {})

    assert res.hold is None and res.named_row["id"] == "P1"
    dep = _plan(repo / "docs" / "plans" / "dep.md", _DEPENDENT)
    assert [r.id for r in read_spine(dep)] == ["D1", "D2", "D3"]


def test_status_edge_to_archived_plan_is_satisfied(tmp_path):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")
    _archive(repo)
    edge = {"plan": "docs/plans/pred.md", "status": "implemented", "gate_kind": "epistemic-premise"}

    assert resolve_plan_edge("row 'D1'", edge, repo, {}).hold is None


@pytest.mark.parametrize("status", ["abandoned", "superseded"])
def test_archived_dead_plan_is_still_dangling(tmp_path, status):
    repo = _repo(tmp_path)
    _predecessor(repo, "open")
    _archive(repo, status=status)

    with pytest.raises(DanglingPlanDependencyError):
        resolve_plan_edge("row 'D1'", _EDGE, repo, {})


def test_plan_absent_everywhere_stays_dangling(tmp_path):
    repo = _repo(tmp_path)
    (repo / "archive" / "specs" / "2026-10").mkdir(parents=True)

    with pytest.raises(DanglingPlanDependencyError):
        resolve_plan_edge("row 'D1'", _EDGE, repo, {})
