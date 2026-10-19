"""plan.write_order: derived edges and run order over certified plans, read-only by construction.

Git is never spawned; plans are synthetic files under tmp_path.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_seam_fix
from coordinator_core.ops import plan_write_order as op

STAMP = 'mise_prepped_sha: "abc123"\n'


def _row(rid, writes=None, extra=""):
    r = f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n"
    if writes is not None:
        r += f"  writes: {writes}\n"
    return r + extra


def _plan(root: Path, name: str, rows: str, fm: str = STAMP) -> str:
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: t\nstatus: approved\n{fm}---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
                 encoding="utf-8", newline="\n")
    return rel


def _fm_edge(plan_rel):
    return f"depends_on_plan:\n  - {{plan: {plan_rel}, status: approved, gate_kind: output-consumption-runtime}}\n"


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _order(root, plans):
    return op._handler({"plans": plans}, repo_root=root)


def _snapshot(root, plans):
    return {p: ((root / p).read_bytes(), (root / p).stat().st_mtime_ns) for p in plans}


def test_pair_orders_later_path_after_earlier(root):
    a = _plan(root, "2026-10-01-a", _row("R1", "[x.py]"))
    b = _plan(root, "2026-10-02-b", _row("R0", "[y.py]") + _row("R1", "[x.py]"))
    r = _order(root, [b, a])
    assert r["set"] == [a, b] and r["order"] == [a, b]
    assert r["edges"] == [{
        "before": a, "after": b, "paths": ["x.py"],
        "rows": [{"after_row": "R1", "before_row": "R1", "path": "x.py"}],
    }]
    assert r["cycles"] == [] and r["already_ordered"] == [] and r["uncertified"] == []


def test_parity_with_seam_fix_dry_run(root):
    a = _plan(root, "2026-10-01-a", _row("R1", "[x.py, z.py]"))
    b = _plan(root, "2026-10-02-b", _row("R1", "[x.py]") + _row("R2", "[z.py]"))
    c = _plan(root, "2026-10-03-c", _row("R1", "[x.py]"))
    plans = [a, b, c]
    fixed = plan_seam_fix._handler({"plans": plans, "dry_run": True}, repo_root=root)
    got = _order(root, plans)
    regrouped = {}
    for e in got["edges"]:
        for row in e["rows"]:
            regrouped[(e["after"], row["after_row"], e["before"], row["before_row"], row["path"])] = 1
    expected = {
        (e["plan"], e["chunk"], e["depends_on"]["plan"], e["depends_on"]["chunk"], e["path"]): 1
        for e in fixed["edges_added"]
    }
    assert regrouped == expected and expected
    assert got["cycles"] == [
        {"plans": c_["plans"], "path": c_["path"], "detail": c_["detail"]} for c_ in fixed["skipped_cycles"]
    ]


def test_input_order_does_not_change_the_reply(root):
    plans = [
        _plan(root, f"2026-10-0{i}-p{i}", _row("R1", "[x.py]") + _row("R2", f"[o{i}.py]"))
        for i in range(1, 6)
    ]
    first = _order(root, plans)
    shuffled = list(plans)
    random.Random(7).shuffle(shuffled)
    assert _order(root, shuffled) == first
    assert _order(root, plans + plans[:2]) == first


def test_inputs_are_byte_and_mtime_unchanged(root):
    a = _plan(root, "2026-10-01-a", _row("R1", "[x.py]"))
    b = _plan(root, "2026-10-02-b", _row("R1", "[x.py]"))
    u = _plan(root, "2026-10-03-u", _row("R1", "[x.py]"), fm="")
    for p in (a, b, u):
        os.utime(root / p, ns=(1_000_000_000, 1_000_000_000))
    before = _snapshot(root, [a, b, u])
    _order(root, [a, b, u])
    assert _snapshot(root, [a, b, u]) == before
    assert b'mise_prepped_sha: "abc123"' in (root / a).read_bytes()


def test_uncertified_is_listed_and_in_no_edge(root):
    a = _plan(root, "2026-10-01-a", _row("R1", "[x.py]"))
    b = _plan(root, "2026-10-02-b", _row("R1", "[x.py]"), fm="")
    blank = _plan(root, "2026-10-03-c", _row("R1", "[x.py]"), fm='mise_prepped_sha: ""\n')
    r = _order(root, [a, b, blank])
    assert r["uncertified"] == [b, blank]
    assert r["set"] == [a] and r["order"] == [a] and r["edges"] == []


def test_unparseable_frontmatter_lands_in_uncertified(root):
    a = _plan(root, "2026-10-01-a", _row("R1", "[x.py]"))
    bad = _plan(root, "2026-10-02-bad", _row("R1", "[x.py]"), fm="key: [unclosed\n")
    r = _order(root, [a, bad])
    assert r["uncertified"] == [bad] and r["set"] == [a]


def test_cycle_is_reported_not_edged(root):
    c = _plan(root, "c", _row("R1", "[y.py]"))
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=STAMP + _fm_edge(c))
    b = _plan(root, "b", _row("R1", "[x.py, y.py]"))
    r = _order(root, [a, b, c])
    assert [e["after"] for e in r["edges"]] == [b]
    assert [(x["plans"], x["path"]) for x in r["cycles"]] == [([c, b], "y.py")]
    assert r["cycles"][0]["detail"]
    assert r["order"].index(c) < r["order"].index(a) < r["order"].index(b)


def test_already_ordered_pair_is_reported(root):
    a = _plan(root, "a", _row("R1", "[x.py, y.py]"))
    b = _plan(root, "b", _row("R1", "[x.py, y.py]"))
    r = _order(root, [a, b])
    assert [(e["before"], e["after"], e["paths"]) for e in r["edges"]] == [(a, b, ["x.py"])]
    assert r["already_ordered"] == [{"before": a, "after": b, "path": "y.py"}]
    assert r["order"] == [a, b]


def test_appends_only_pair_yields_no_edge(root):
    extra = "  appends: [log.md]\n"
    a = _plan(root, "2026-10-01-a", _row("R1", "[log.md]", extra))
    b = _plan(root, "2026-10-02-b", _row("R1", "[log.md]", extra))
    r = _order(root, [a, b])
    assert r["edges"] == [] and r["cycles"] == [] and r["order"] == [a, b]


def test_unreadable_spine_is_reported_and_excluded(root):
    a = _plan(root, "2026-10-01-a", _row("R1", "[x.py]"))
    bad = _plan(root, "2026-10-02-bad", _row("R1", "[x.py]"))
    (root / bad).write_bytes(b"---\n\xff\xfe\n---\n")
    r = _order(root, [a, bad])
    assert [u["plan"] for u in r["unreadable"]] == [bad]
    assert r["set"] == [a] and r["edges"] == [] and r["uncertified"] == []


@pytest.mark.parametrize("params", [
    {}, {"plans": []}, {"plans": ["../outside.md"]}, {"plans": ["docs/plans/missing.md"]},
    {"plans": ["README.md"]},
])
def test_bad_params_are_refused(root, params):
    (root / "README.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        op._handler(params, repo_root=root)


def test_201_entries_refused_before_any_path_resolves(root):
    plans = [f"docs/plans/ghost-{i:03d}.md" for i in range(201)]
    with pytest.raises(ValueError, match="bound is 200"):
        op._handler({"plans": plans}, repo_root=root)


def test_200_entries_accepted(root):
    plans = [_plan(root, f"2026-10-01-b{i:03d}", _row("R1", f"[own{i}.py]")) for i in range(200)]
    assert len(_order(root, plans)["set"]) == 200


def test_handler_requires_repo_root():
    with pytest.raises(ValueError):
        op._handler({"plans": ["docs/plans/a.md"]}, repo_root=None)


def test_spawns_nothing(root, monkeypatch):
    plans = []
    for i in range(30):
        rows = _row("R1", f"[own{i}.py, shared{i % 10}.py]") + _row("R2", f"[more{i}.py]")
        plans.append(_plan(root, f"2026-10-01-p{i:03d}", rows))
    with _count_spawns_attributed(monkeypatch) as spawns:
        r = _order(root, plans)
    assert len(r["set"]) == 30 and r["edges"], "operation must have succeeded before the count means anything"
    assert len(spawns) == 0, [s.argv for s in spawns]
