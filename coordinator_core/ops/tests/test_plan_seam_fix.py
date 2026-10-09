"""plan.seam_fix: edge minting for in-set writes-collisions, idempotence, cycle skip, dry_run.

Git is never spawned: the fix path reads plans from disk only.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_seam_check
from coordinator_core.ops import plan_seam_fix as op


def _row(rid, writes=None, extra=""):
    r = f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n"
    if writes is not None:
        r += f"  writes: {writes}\n"
    return r + extra


def _plan(root: Path, name: str, rows: str, fm: str = "") -> str:
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: t\nstatus: approved\n{fm}---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
                 encoding="utf-8")
    return rel


def _fm_edge(plan_rel):
    return f"depends_on_plan:\n  - {{plan: {plan_rel}, status: approved, gate_kind: output-consumption-runtime}}\n"


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _fix(root, plans, **kw):
    return op._handler({"plans": plans, **kw}, repo_root=root)


def _rows(root, rel):
    text = (root / rel).read_text(encoding="utf-8")
    body = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(body)


def test_pair_gets_one_edge_on_later_plan(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    b = _plan(root, "b", _row("R0", "[y.py]") + _row("R1", "[x.py]"))
    before_a = (root / a).read_bytes()
    r = _fix(root, [b, a])
    assert r["edges_added"] == [{"plan": b, "chunk": "R1", "depends_on": {"plan": a, "chunk": "R1"}, "path": "x.py"}]
    assert r["skipped_cycles"] == [] and r["files"] == [b] and r["converged"] is True
    assert (root / a).read_bytes() == before_a
    edge = _rows(root, b)[1]["depends_on_plan"][0]
    assert edge["plan"] == a and edge["chunk"] == "R1" and edge["gate_kind"] == "output-consumption-runtime"
    assert "depends_on_plan" not in _rows(root, b)[0]


def test_untouched_rows_keep_their_bytes(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    untouched = "- id: R0\n  title:   'spaced   title'\n  surface: s\n  change_kind: code-edit\n  writes: [y.py]\n"
    b = _plan(root, "b", untouched + _row("R1", "[x.py]"))
    _fix(root, [a, b])
    assert untouched in (root / b).read_text(encoding="utf-8")


def test_every_covering_row_of_the_later_plan_is_edged_and_second_run_is_quiet(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    b = _plan(root, "b", _row("R1", "[x.py]") + _row("R2", "[x.py]"))
    r = _fix(root, [a, b])
    assert [e["chunk"] for e in r["edges_added"]] == ["R1", "R2"] and r["converged"] is True
    assert _fix(root, [a, b])["edges_added"] == []


def test_second_run_writes_nothing(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    b = _plan(root, "b", _row("R1", "[x.py]"))
    _fix(root, [a, b])
    snap = (root / b).read_bytes()
    r = _fix(root, [a, b])
    assert r == {"edges_added": [], "skipped_cycles": [], "files": [], "converged": True}
    assert (root / b).read_bytes() == snap


def test_existing_transitive_order_adds_no_edge(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    m = _plan(root, "m", _row("R1", "[m.py]"), fm=_fm_edge(a))
    z = _plan(root, "z", _row("R1", "[x.py]"), fm=_fm_edge(m))
    snaps = {p: (root / p).read_bytes() for p in (a, m, z)}
    r = _fix(root, [a, m, z])
    assert r["edges_added"] == [] and r["files"] == [] and r["converged"] is True
    assert snaps == {p: (root / p).read_bytes() for p in (a, m, z)}


def test_opposite_order_closing_a_cycle_is_a_finding_not_an_edge(root):
    # a depends on c. a/b collide on x, b/c collide on y. The stable edge b->a puts b after c,
    # so the stable edge c->b would close c->b->a->c.
    c = _plan(root, "c", _row("R1", "[y.py]"))
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_fm_edge(c))
    b = _plan(root, "b", _row("R1", "[x.py, y.py]"))
    r = _fix(root, [a, b, c])
    assert [e["plan"] for e in r["edges_added"]] == [b]
    assert len(r["skipped_cycles"]) == 1
    f = r["skipped_cycles"][0]
    assert f["class"] == "seam-fix-cycle" and f["plans"] == [c, b] and f["path"] == "y.py" and f["detail"]
    assert "depends_on_plan" not in _rows(root, c)[0]
    assert r["converged"] is True


def test_appends_only_pair_gets_no_edge(root):
    a = _plan(root, "a", _row("R1", "[log.md]", extra="  appends: [log.md]\n"))
    b = _plan(root, "b", _row("R1", "[log.md]", extra="  appends: [log.md]\n"))
    r = _fix(root, [a, b])
    assert r["edges_added"] == [] and r["files"] == [] and r["converged"] is True


def test_dry_run_writes_nothing(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    b = _plan(root, "b", _row("R1", "[x.py]"))
    snaps = {p: (root / p).read_bytes() for p in (a, b)}
    r = _fix(root, [a, b], dry_run=True)
    assert len(r["edges_added"]) == 1 and r["files"] == [] and r["converged"] is True
    assert snaps == {p: (root / p).read_bytes() for p in (a, b)}
    assert not list((root / "docs" / "plans").glob("*.tmp*"))


def test_converges_against_seam_check(root):
    names = [_plan(root, f"p{i:02d}", _row("R1", "[shared.py]") + _row("R2", f"[own{i}.py]")) for i in range(5)]
    r = _fix(root, names)
    assert len(r["edges_added"]) == 4 and r["converged"] is True
    pset = {n: plan_seam_check._Plan(n, root) for n in names}
    assert not [f for f in plan_seam_check._collision_findings(pset, {"named_set": True, "hubs": set()}) if f["path"]]
    assert _fix(root, names)["edges_added"] == []


def test_params_validated(root):
    a = _plan(root, "a", _row("R1", "[x.py]"))
    with pytest.raises(ValueError):
        _fix(root, [])
    with pytest.raises(ValueError):
        _fix(root, [a], dry_run="yes")
    with pytest.raises(ValueError):
        _fix(root, ["../outside.md"])


def test_fifty_plan_set_is_within_budget(root):
    names = [_plan(root, f"p{i:02d}", _row("R1", "[shared.py]") + _row("R2", f"[own{i}.py]")) for i in range(50)]
    t0 = time.process_time()
    r = _fix(root, names)
    elapsed_ms = (time.process_time() - t0) * 1000
    assert r["converged"] is True and len(r["edges_added"]) == 49
    print(f"seam_fix 50 plans: {elapsed_ms:.0f} ms process time")
    assert elapsed_ms < 500
