"""plan.overlap_edges op body: bound, determinism, exclusions, grouping, cycles, seam_fix reach.

Most cases build plans with `LeanPlan.from_fields` through the `read` seam; the files under
tmp_path are stubs the path check resolves. The last two cases run the real reader.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from coordinator_core.ops import plan_overlap_edges as op
from coordinator_core.ops import plan_seam_fix as seam_fix
from coordinator_core.ops.plan_writes_lean import LeanPlan, read_lean_plan

_DEP = "gate_kind: output-consumption-runtime, note: n"


def _stub(root: Path, name: str) -> str:
    rel = f"docs/plans/{name}.md"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("stub\n", encoding="utf-8")
    return rel


def _lean(root: Path, plans: dict, name: str, rows, *, sha="abc", edges=()):
    rel = _stub(root, name)
    plans[rel] = LeanPlan.from_fields(rel, certified_sha=sha, rows=rows, plan_edges=edges)
    return rel


def _r(rid, writes, **extra):
    return {"id": rid, "writes": writes, **extra}


def _read(plans):
    return lambda rel, root: plans[rel]


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _run(root, plans, rels=None):
    return op._overlap_edges({"plans": list(rels or plans)}, root, read=_read(plans))


def test_over_bound_is_refused_before_any_path_resolves(root):
    params = {"plans": [f"docs/plans/nope-{i}.md" for i in range(op.MAX_PLANS + 1)]}
    with pytest.raises(ValueError, match=f"bound {op.MAX_PLANS}"):
        op._overlap_edges(params, root)
    with pytest.raises(ValueError, match="no such plan"):
        op._overlap_edges({"plans": params["plans"][: op.MAX_PLANS]}, root)


@pytest.mark.parametrize("bad", [None, [], "a.md", [1], ["  "]])
def test_params_must_be_a_non_empty_list_of_paths(root, bad):
    with pytest.raises(ValueError, match="non-empty list"):
        op._overlap_edges({"plans": bad}, root)


def test_paths_are_contained_and_under_docs_plans(root):
    _stub(root, "a")
    (root / "docs" / "other.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        op._overlap_edges({"plans": ["../x.md"]}, root)
    with pytest.raises(ValueError, match="docs/plans"):
        op._overlap_edges({"plans": ["docs/other.md"]}, root)


def test_pair_reply_shape_and_direction(root):
    plans = {}
    a = _lean(root, plans, "a", [_r("A1", ["x.py", "y.py"])])
    b = _lean(root, plans, "b", [_r("B0", ["z.py"]), _r("B1", ["x.py"]), _r("B2", ["x.py"])])
    got = _run(root, plans, [b, a])
    assert got == {
        "set": [a, b],
        "edges": [{"before": a, "after": b, "paths": ["x.py"], "rows": [
            {"after_row": "B1", "before_row": "A1", "path": "x.py"},
            {"after_row": "B2", "before_row": "A1", "path": "x.py"}]}],
        "cycles": [], "uncertified": [], "unreadable": [],
    }


def test_permuted_inputs_and_duplicates_give_equal_replies(root):
    plans = {}
    rels = [
        _lean(root, plans, "a", [_r("A1", ["x.py"])]),
        _lean(root, plans, "b", [_r("B1", ["x.py", "q.py"])]),
        _lean(root, plans, "c", [_r("C1", ["q.py"])]),
        _lean(root, plans, "d", [_r("D1", ["x.py"])]),
    ]
    first = _run(root, plans, rels)
    assert first["edges"]
    for perm in itertools.permutations(rels):
        assert _run(root, plans, list(perm) + [rels[0]]) == first


def test_uncertified_plan_is_listed_and_in_no_edge(root):
    plans = {}
    a = _lean(root, plans, "a", [_r("A1", ["x.py"])])
    u = _lean(root, plans, "u", [_r("U1", ["x.py"])], sha="")
    b = _lean(root, plans, "b", [_r("B1", ["x.py"])])
    got = _run(root, plans)
    assert got["uncertified"] == [u] and u not in got["set"]
    assert [(e["before"], e["after"]) for e in got["edges"]] == [(a, b)]


def test_unreadable_plan_is_listed_with_detail_and_excluded(root):
    plans = {}
    a = _lean(root, plans, "a", [_r("A1", "x.py")])
    b = _lean(root, plans, "b", [_r("B1", ["x.py"])])
    got = _run(root, plans)
    assert [u["plan"] for u in got["unreadable"]] == [a]
    assert got["unreadable"][0]["detail"] and got["set"] == [b] and got["edges"] == []


def test_cycle_is_reported_and_not_edged(root):
    plans = {}
    c3 = _lean(root, plans, "cyc-3", [_r("C3", ["p.py"])])
    c2 = _lean(root, plans, "cyc-2", [_r("C2", ["p.py", "q.py"])])
    c1 = _lean(root, plans, "cyc-1", [_r("C1", ["q.py"])], edges=[c3])
    got = _run(root, plans)
    assert len(got["cycles"]) == 1
    cyc = got["cycles"][0]
    assert cyc["plans"] == [c3, c2] and cyc["path"] == "p.py" and cyc["detail"]
    assert [(e["before"], e["after"]) for e in got["edges"]] == [(c1, c2)]


def test_closed_rows_and_declared_edges_add_nothing(root):
    plans = {}
    a = _lean(root, plans, "a", [_r("A1", ["x.py"], disposition="coded")])
    b = _lean(root, plans, "b", [_r("B1", ["x.py"], disposition="done")])
    c = _lean(root, plans, "c", [_r("C1", ["y.py"])])
    d = _lean(root, plans, "d", [_r("D1", ["y.py"])], edges=[c])
    got = _run(root, plans)
    assert got["edges"] == [] and sorted(got["set"]) == sorted([a, b, c, d])


def test_handler_requires_a_resolved_root():
    with pytest.raises(ValueError, match="repo_root"):
        op._handler({"plans": ["docs/plans/a.md"]})


_DROPPED = {
    "dfr": "  deferred: true\n",
    "em": "  performer: em\n",
    "gate": "  external_gate:\n    - id: g\n",
}


def _write_plan(root: Path, name: str, rows: str, fm: str = "") -> str:
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntitle: t\nstatus: approved\nmise_prepped_sha: abc123\n{fm}---\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )
    return rel


def _real_row(rid, writes, extra=""):
    return f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n  writes: {writes}\n{extra}"


def _real_fixture(root):
    rels = [
        _write_plan(root, "a-live", _real_row("A1", "[x.py]")),
        _write_plan(root, "b-live", _real_row("B0", "[o.py]") + _real_row(
            "B1", "[x.py]", f"  depends_on:\n    - {{chunk: B0, {_DEP}}}\n")),
        _write_plan(root, "c-dropped", _real_row("C1", "[d.py]", _DROPPED["dfr"])),
        _write_plan(root, "d-dropped", _real_row("D1", "[d.py]")),
        _write_plan(root, "e-em", _real_row("E1", "[e.py]", _DROPPED["em"])),
        _write_plan(root, "f-em", _real_row("F1", "[e.py]")),
        _write_plan(root, "g-gate", _real_row("G1", "[g.py]", _DROPPED["gate"])),
        _write_plan(root, "h-gate", _real_row("H1", "[g.py]")),
        _write_plan(root, "i-declared", _real_row("I1", "[i.py]")),
        _write_plan(root, "j-declared", _real_row("J1", "[i.py]"),
                    fm="depends_on_plan:\n  - {plan: docs/plans/i-declared.md, status: approved, "
                       + _DEP + "}\n"),
    ]
    return rels


def _graph(reply, read_plans):
    graph = {}
    for rel, plan in read_plans.items():
        graph.setdefault(rel, set()).update(d for d in plan.edge_plans() if d in read_plans)
    for e in reply["edges"]:
        graph.setdefault(e["after"], set()).add(e["before"])
    return graph


def _reaches(graph, a, b):
    seen, todo = set(), [a]
    while todo:
        for nxt in graph.get(todo.pop(), ()):
            if nxt == b:
                return True
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return False


def test_every_seam_fix_dry_run_pair_is_reached_and_the_op_adds_more(root):
    rels = _real_fixture(root)
    reply = op._overlap_edges({"plans": rels}, root)
    dry = seam_fix._handler({"plans": rels, "dry_run": True}, repo_root=root)
    assert dry["edges_added"] and dry["files"] == []
    graph = _graph(reply, {rel: read_lean_plan(rel, root) for rel in rels})
    for added in dry["edges_added"]:
        assert _reaches(graph, added["plan"], added["depends_on"]["plan"]), added
    ordered = {(e["before"], e["after"]) for e in reply["edges"]}
    dry_pairs = {(a["depends_on"]["plan"], a["plan"]) for a in dry["edges_added"]}
    assert dry_pairs < ordered
    for early, late in (("c-dropped", "d-dropped"), ("e-em", "f-em"), ("g-gate", "h-gate")):
        assert (f"docs/plans/{early}.md", f"docs/plans/{late}.md") in ordered - dry_pairs
    assert ("docs/plans/i-declared.md", "docs/plans/j-declared.md") not in ordered


def test_real_reader_leaves_every_input_plan_bytes_and_mtime_unchanged(root):
    rels = _real_fixture(root)
    snap = {rel: ((root / rel).read_bytes(), (root / rel).stat().st_mtime_ns) for rel in rels}
    reply = op._overlap_edges({"plans": rels}, root)
    assert reply["edges"]
    assert snap == {rel: ((root / rel).read_bytes(), (root / rel).stat().st_mtime_ns) for rel in rels}
