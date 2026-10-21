"""`read_spine(landed_plan_chunks=...)`: a checkpoint-landed open row is done, and a
landed `depends_on_plan` target does not hold its dependent."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.spine_read import read_spine

_OWN = """\
- id: C1
  title: first
  surface: a.py
  writes: [a.py]
- id: C2
  title: second
  surface: b.py
  writes: [b.py]
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
- id: C3
  title: free
  surface: c.py
  writes: [c.py]
"""

_EDGE = """\
- id: D1
  title: dependent
  surface: d.py
  writes: [d.py]
  depends_on_plan:
    - plan: docs/plans/pred.md
      chunk: P1
      gate_kind: output-consumption-runtime
"""

_PRED = "- id: P1\n  title: pred\n  surface: p.py\n  writes: [p.py]\n"


def _plan(path, rows_yaml: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows_yaml + "\n```\n", encoding="utf-8"
    )
    return path


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_landed_own_row_is_excluded_and_does_not_hold_dependent(tmp_path):
    repo = _repo(tmp_path)
    plan = _plan(repo / "docs" / "plans" / "own.md", _OWN)
    ex: list = []

    rows = read_spine(plan, exclusions=ex, landed_plan_chunks={("docs/plans/own.md", "C1"): "a" * 40})

    assert [r.id for r in rows] == ["C2", "C3"]
    by_id = {e["id"]: e for e in ex}
    assert by_id["C1"]["reason"] == "checkpoint-landed"
    assert by_id["C1"]["detail"] == "landed by checkpoint " + "a" * 10
    assert "C2" not in by_id


def test_default_is_identical_to_empty_mapping(tmp_path):
    repo = _repo(tmp_path)
    plan = _plan(repo / "docs" / "plans" / "own.md", _OWN)
    ex_default: list = []
    ex_empty: list = []

    a = read_spine(plan, exclusions=ex_default)
    b = read_spine(plan, exclusions=ex_empty, landed_plan_chunks={})

    assert [r.id for r in a] == [r.id for r in b] == ["C1", "C2", "C3"]
    assert ex_default == ex_empty == []


def test_key_for_another_plan_does_not_exclude(tmp_path):
    repo = _repo(tmp_path)
    plan = _plan(repo / "docs" / "plans" / "own.md", _OWN)

    rows = read_spine(plan, landed_plan_chunks={("docs/plans/other.md", "C1"): "b" * 40})

    assert [r.id for r in rows] == ["C1", "C2", "C3"]


def test_landed_edge_target_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    _plan(repo / "docs" / "plans" / "pred.md", _PRED)
    dep = _plan(repo / "docs" / "plans" / "dep.md", _EDGE)

    held: list = []
    assert read_spine(dep, exclusions=held) == []
    assert held[0]["reason"] == "depends_on_plan"

    rows = read_spine(dep, landed_plan_chunks={("docs/plans/pred.md", "P1"): "c" * 40})
    assert [r.id for r in rows] == ["D1"]


def test_landed_row_that_is_not_open_is_not_relabelled(tmp_path):
    repo = _repo(tmp_path)
    plan = _plan(repo / "docs" / "plans" / "own.md", _OWN + "  disposition: coded\n")
    ex: list = []

    read_spine(plan, exclusions=ex, landed_plan_chunks={("docs/plans/own.md", "C3"): "d" * 40})

    assert {e["id"]: e["reason"] for e in ex}["C3"] == "disposition"
