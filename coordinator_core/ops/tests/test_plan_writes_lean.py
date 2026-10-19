"""plan_writes_lean: the lean reader feeds derive_edges, orders a superset of read_spine's pairs.

Liveness there is disposition-not-closed (APM ruling), so it keeps rows read_spine drops. Plans
are written under tmp_path; nothing spawns and nothing is written outside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_seam_fix as fix
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.plan_seam_check import _Plan
from coordinator_core.ops.plan_writes_lean import _Fallback, _scan_fast, read_lean_plan

_DEP = "gate_kind: output-consumption-runtime, note: n"


def _row(rid, writes=None, extra=""):
    r = f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n"
    if writes is not None:
        r += f"  writes: {writes}\n"
    return r + extra


def _plan(root: Path, name: str, rows: str, fm: str = "", stamp: str = "abc123") -> str:
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp_line = f"mise_prepped_sha: {stamp}\n" if stamp is not None else ""
    p.write_text(
        f"---\ntitle: t\nstatus: approved\n{stamp_line}{fm}---\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )
    return rel


def _fm_edge(plan_rel):
    return f"depends_on_plan:\n  - {{plan: {plan_rel}, status: approved, {_DEP}}}\n"


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


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _fixture_set(root):
    """Pairs that read_spine-live rows order, pairs only disposition-liveness orders, and the
    shapes that must add no edge."""
    plans = {}
    # read_spine-live writer pair whose rows carry chunk depends_on
    plans["live-1"] = _plan(root, "live-1", _row("L0", "[z0.py]") + _row(
        "L1", "[l.py]", f"  depends_on:\n    - {{chunk: L0, {_DEP}}}\n"))
    plans["live-2"] = _plan(root, "live-2", _row("M0", "[z1.py]") + _row(
        "M1", "[l.py]", f"  depends_on:\n    - chunk: M0\n      {_DEP.replace(', ', chr(10) + '      ')}\n"))
    # rows read_spine drops and disposition-only liveness keeps
    plans["dfr-1"] = _plan(root, "dfr-1", _row("D1", "[d.py]", "  deferred: true\n"))
    plans["dfr-2"] = _plan(root, "dfr-2", _row("D2", "[d.py]"))
    plans["em-1"] = _plan(root, "em-1", _row("E1", "[e.py]", "  performer: em\n"))
    plans["em-2"] = _plan(root, "em-2", _row("E2", "[e.py]"))
    plans["gate-1"] = _plan(root, "gate-1", _row("G1", "[g.py]", "  external_gate:\n    - id: g\n"))
    plans["gate-2"] = _plan(root, "gate-2", _row("G2", "[g.py]"))
    plans["trans-1"] = _plan(root, "trans-1", _row("T1", "[t0.py]", "  deferred: true\n") + _row(
        "T2", "[t.py]", f"  depends_on:\n    - {{chunk: T1, {_DEP}}}\n"))
    plans["trans-2"] = _plan(root, "trans-2", _row("T3", "[t.py]"))
    # declared edges: frontmatter and row, already ordering their pair
    plans["dec-1"] = _plan(root, "dec-1", _row("A1", "[dec.py]"))
    plans["dec-2"] = _plan(root, "dec-2", _row("A2", "[dec.py]"), fm=_fm_edge(plans["dec-1"]))
    plans["row-1"] = _plan(root, "row-1", _row("B1", "[row.py]"))
    plans["row-2"] = _plan(root, "row-2", _row(
        "B2", "[row.py]", f"  depends_on_plan:\n    - {{plan: {plans['row-1']}, chunk: B1, {_DEP}}}\n"))
    # mixed declared/derived cycle: cyc-1 declares it follows cyc-3; the derived cyc-3-after-cyc-2
    # edge then makes cyc-2-after-cyc-1 close a loop
    plans["cyc-3"] = _plan(root, "cyc-3", _row("C3", "[cyc-p.py]"))
    plans["cyc-2"] = _plan(root, "cyc-2", _row("C2", "[cyc-p.py, cyc-q.py]"))
    plans["cyc-1"] = _plan(root, "cyc-1", _row("C1", "[cyc-q.py]"), fm=_fm_edge(plans["cyc-3"]))
    # no edge: append-only pair, hub, closed rows
    appends = "  appends: [log.md]\n"
    plans["app-1"] = _plan(root, "app-1", _row("P1", "[log.md]", appends))
    plans["app-2"] = _plan(root, "app-2", _row("P2", "[log.md]", appends))
    plans["hub-1"] = _plan(root, "hub-1", _row("H1", "[hub.md]"))
    plans["hub-2"] = _plan(root, "hub-2", _row("H2", "[hub.md]"))
    for i, disp in enumerate(("coded", "wont_do", "done"), 1):
        plans[f"closed-{i}"] = _plan(root, f"closed-{i}", _row(
            f"X{i}", "[closed.py]", f"  disposition: {disp}\n"))
    plans["closed-live"] = _plan(root, "closed-live", _row("XL", "[closed.py]"))
    return plans


def _derive(root, plans, load):
    rels = sorted(plans.values())
    pset = {rel: load(rel, root) for rel in rels}
    return fix.derive_edges(pset, {"hub.md"}, root)


def test_reach_superset_over_read_spine_live_rows_is_strict(root):
    plans = _fixture_set(root)
    ref = _derive(root, plans, _Plan)
    lean = _derive(root, plans, read_lean_plan)

    assert ref.pairs
    for later, earlier, path in ref.pairs:
        if _reaches(ref.graph, later, earlier):
            assert _reaches(lean.graph, later, earlier), (later, earlier, path)

    # the fixture holds read_spine-live pairs, including the one whose rows carry depends_on
    ref_paths = {p for _, _, p in ref.pairs}
    assert "l.py" in ref_paths
    assert any(e["path"] == "l.py" for e in lean.edges_added)

    # strict: rows read_spine drops are ordered here and not there
    for name, path in (("dfr", "d.py"), ("em", "e.py"), ("gate", "g.py"), ("trans", "t.py")):
        a, b = sorted((plans[f"{name}-1"], plans[f"{name}-2"]))
        assert not _reaches(ref.graph, b, a) and not _reaches(ref.graph, a, b), name
        assert _reaches(lean.graph, b, a), name
        assert any(e["path"] == path for e in lean.edges_added), name


def test_closed_rows_add_no_write_and_no_edge(root):
    plans = _fixture_set(root)
    lean = _derive(root, plans, read_lean_plan)
    for i in (1, 2, 3):
        p = read_lean_plan(plans[f"closed-{i}"], root)
        assert p.load_rows() == []
        assert not any(plans[f"closed-{i}"] in (e["plan"], e["depends_on"]["plan"])
                       for e in lean.edges_added)
    assert not any(e["path"] == "closed.py" for e in lean.edges_added)


def test_append_only_hub_and_declared_edges_add_no_edge(root):
    plans = _fixture_set(root)
    lean = _derive(root, plans, read_lean_plan)
    added = {e["path"] for e in lean.edges_added}
    assert "log.md" not in added and "hub.md" not in added
    assert "dec.py" not in added and "row.py" not in added
    assert _reaches(lean.graph, plans["dec-2"], plans["dec-1"])
    assert _reaches(lean.graph, plans["row-2"], plans["row-1"])


def test_declared_cycle_is_skipped_not_edged(root):
    plans = _fixture_set(root)
    lean = _derive(root, plans, read_lean_plan)
    assert len(lean.skipped_cycles) == 1 and lean.skipped_cycles[0]["path"].startswith("cyc-")
    skipped = lean.skipped_cycles[0]["path"]
    assert not any(e["path"] == skipped for e in lean.edges_added)


def test_absent_and_null_writes_read_undeclared_and_empty_list_stays_empty(root):
    rows = _row("N1") + _row("N2", "") + _row("N3", "[]") + _row("N4", "[a.py]")
    rel = _plan(root, "w", rows)
    got = {r.id: r.writes for r in read_lean_plan(rel, root).load_rows()}
    assert got["N1"] is UNDECLARED and got["N2"] is UNDECLARED
    assert got["N3"] == [] and got["N4"] == ["a.py"]


def test_writes_under_alone_reads_as_declared_empty_writes(root):
    rel = _plan(root, "wu", _row("U1", None, "  writes_under:\n    - src\\lib/\n"))
    (row,) = read_lean_plan(rel, root).load_rows()
    assert row.writes == [] and row.writes_under == ("src/lib/",)


def test_uncertified_plan_has_no_rows(root):
    for stamp in (None, "null", "''"):
        rel = _plan(root, "u", _row("R1", "[x.py]"), stamp=stamp)
        p = read_lean_plan(rel, root)
        assert p.certified_sha == "" and p.unreadable is None and p.load_rows() == []
    assert read_lean_plan(_plan(root, "c", _row("R1", "[x.py]")), root).certified_sha == "abc123"


def test_edge_targets_come_from_frontmatter_and_rows_and_drop_escapes(root):
    fm = (f"depends_on_plan:\n  - {{plan: docs/plans/f.md, {_DEP}}}\n"
          f"  - {{plan: ../outside.md, {_DEP}}}\n")
    rel = _plan(root, "e", _row("R1", "[x.py]", f"  depends_on_plan:\n    - plan: docs/plans/r.md\n      {_DEP}\n"),
                fm=fm)
    assert read_lean_plan(rel, root).edge_plans() == {"docs/plans/f.md", "docs/plans/r.md"}


def test_unreadable_classes_each_carry_a_detail(root):
    no_spine = "docs/plans/nospine.md"
    (root / no_spine).parent.mkdir(parents=True, exist_ok=True)
    (root / no_spine).write_text("---\nmise_prepped_sha: abc\n---\n\n# no tasks\n", encoding="utf-8")
    malformed = _plan(root, "malformed", "- id: R1\n  writes: [unclosed\n  title: 'x\n")
    not_list = _plan(root, "notlist", _row("R1", "x.py"))
    under = _plan(root, "under", _row("R1", "[a.py]", "  writes_under: src/\n"))
    not_mapping = _plan(root, "notmap", "- just\n- strings\n")
    missing = "docs/plans/missing.md"
    for rel in (no_spine, malformed, not_list, under, not_mapping, missing):
        p = read_lean_plan(rel, root)
        p.load_rows()
        assert p.unreadable, rel
    assert "absent" in read_lean_plan(no_spine, root).unreadable
    assert "not a list" in (lambda p: (p.load_rows(), p.unreadable)[1])(read_lean_plan(not_list, root))
    assert "writes_under" in (lambda p: (p.load_rows(), p.unreadable)[1])(read_lean_plan(under, root))


def test_reading_writes_nothing(root):
    plans = _fixture_set(root)
    snap = {rel: ((root / rel).read_bytes(), (root / rel).stat().st_mtime_ns) for rel in plans.values()}
    for rel in plans.values():
        read_lean_plan(rel, root).load_rows()
    assert snap == {rel: ((root / rel).read_bytes(), (root / rel).stat().st_mtime_ns)
                    for rel in plans.values()}


_SHAPES = [
    "- id: A\n  title: t\n  writes: [a.py, b/c.py]\n  writes_under: [src/]\n  disposition: open\n",
    "- id: A\n  writes:\n    - a.py\n    - 'b c.py'  # note\n  appends: [a.py]\n",
    "- id: A\n  depends_on:\n    - chunk: B\n      gate_kind: x\n    - {chunk: C, gate_kind: y}\n",
    "- id: A\n  depends_on_plan:\n    - {plan: docs/plans/p.md, chunk: B}\n"
    "    # comment between items\n    - plan: docs/plans/q.md\n",
    "# leading comment\n- id: A\n  writes: []\n  body: |\n    text: not a key\n    - not: a row\n- id: B\n",
    "- id: A\n  writes:\n  disposition: coded\n- id: B\n  writes:\n    - x.py\n",
    "- id: A\n  depends_on: []\n  depends_on_plan: []\n  writes: [a.py]\n",
]


@pytest.mark.parametrize("body", _SHAPES)
def test_hand_scan_equals_yaml_on_the_wanted_keys(body):
    wanted = {"id", "disposition", "writes", "writes_under", "appends"}

    def project(rows):
        out = []
        for r in rows:
            row = {k: r[k] for k in wanted if k in r}
            row["depends_on"] = [e.get("chunk") for e in r.get("depends_on") or []]
            row["depends_on_plan"] = [e.get("plan") for e in r.get("depends_on_plan") or []]
            out.append(row)
        return out

    try:
        scanned = _scan_fast(body)
    except _Fallback:
        pytest.skip("shape routed to the YAML path")
    assert project(scanned) == project(yaml.safe_load(body))


@pytest.mark.parametrize("body", [
    "- id: A\n  writes: null\n",
    "- id: A\n  writes: [a, [b]]\n",
    "- id: A\n  writes: [\"a\\n\"]\n",
    "- id: 12\n",
    "- id: A\n  disposition: *anchor\n",
    "- id: A\n  depends_on: [{chunk: B}]\n",
])
def test_shapes_yaml_could_type_differently_fall_back(body):
    with pytest.raises(_Fallback):
        _scan_fast(body)


def test_fallback_plan_reads_through_yaml_with_same_result(root):
    rel = _plan(root, "fb", "- id: R1\n  writes: [a.py, [b.py]]\n- id: R2\n  writes: [c.py]\n")
    p = read_lean_plan(rel, root)
    assert p.raw_rows[1]["writes"] == ["c.py"]
    assert p.unreadable is None
