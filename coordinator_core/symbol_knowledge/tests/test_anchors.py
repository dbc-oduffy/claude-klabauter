"""Tests for coordinator_core.symbol_knowledge.anchors against a synthetic example-retrieval-repo store."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from coordinator_core.symbol_knowledge import anchors, freshness

PREFIX = "scip-python python claude-klabauter 0.1 "
FLAG_SCIP = PREFIX + "`coordinator_core.symbol_knowledge.flag`/flag_symbols()."
FLAG_PATH = "coordinator_core/symbol_knowledge/flag.py"
FLAG_QN = FLAG_SCIP
FN = PREFIX + "`pkg.m`/fn()."
GONE = PREFIX + "`pkg.m`/gone()."
TWIN = PREFIX + "`pkg.m`/twin()."
BARE = PREFIX + "`pkg.m`/bare()."
NOPE = PREFIX + "`pkg.m`/nope()."


def _store(root: Path, symbols: "list[tuple]") -> None:
    """Rows are (file, name, kind, scope, scip_symbol or None); builds the rc_v1_* views."""
    (root / ".project-rag").mkdir(exist_ok=True)
    db = sqlite3.connect(root / freshness.STORE_REL)
    db.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, scope TEXT, file TEXT)")
    db.execute("CREATE TABLE symbol_scip (symbol_id INTEGER, scip_symbol TEXT)")
    for file, name, kind, scope, scip in symbols:
        cur = db.execute("INSERT INTO symbols (name, kind, scope, file) VALUES (?,?,?,?)", (name, kind, scope, file))
        if scip is not None:
            db.execute("INSERT INTO symbol_scip VALUES (?,?)", (cur.lastrowid, scip))
    db.execute(
        "CREATE VIEW rc_v1_symbols AS SELECT id AS symbol_id, name, kind, scope, file FROM symbols"
    )
    db.execute(
        "CREATE VIEW rc_v1_symbol_scip AS SELECT s.id AS symbol_id, m.scip_symbol AS scip_symbol "
        "FROM symbols s JOIN symbol_scip m ON m.symbol_id = s.id"
    )
    db.commit()
    db.close()


def test_resolve_returns_the_indexed_scip_string_for_the_anchor_fact(tmp_path):
    _store(tmp_path, [(FLAG_PATH, "flag_symbols", "function", "", FLAG_SCIP)])
    assert anchors.resolve(tmp_path, FLAG_PATH, FLAG_QN) == FLAG_SCIP


def test_resolve_matches_scoped_method_rows(tmp_path):
    scip = PREFIX + "`pkg.mod`/Box#size()."
    _store(tmp_path, [("pkg/mod.py", "size", "method", "Box", scip)])
    assert anchors.resolve(tmp_path, "pkg/mod.py", scip) == scip


def test_unknown_ambiguous_and_unmapped_names_resolve_to_none(tmp_path):
    _store(
        tmp_path,
        [
            ("pkg/m.py", "twin", "function", "", "scip one"),
            ("pkg/m.py", "twin", "function", "", "scip two"),
            ("pkg/m.py", "bare", "function", "", None),
        ],
    )
    got = anchors.resolve_many(tmp_path, [("pkg/m.py", TWIN), ("pkg/m.py", BARE), ("pkg/m.py", NOPE)])
    assert set(got.values()) == {None}
    assert anchors.gaps(got) == sorted(got)


def test_identical_scip_rows_across_generations_still_resolve(tmp_path):
    _store(tmp_path, [("pkg/m.py", "fn", "function", "", "scip fn")] * 2)
    assert anchors.resolve(tmp_path, "pkg/m.py", FN) == "scip fn"


def test_absent_store_resolves_none_and_never_creates_it(tmp_path):
    assert anchors.resolve(tmp_path, "pkg/m.py", FN) is None
    assert not (tmp_path / ".project-rag").exists()


def test_attach_sets_sym_on_resolved_items_only(tmp_path):
    _store(tmp_path, [("pkg/m.py", "fn", "function", "", "scip fn")])
    items = [{"path": "pkg/m.py", "qualified_name": FN}, {"path": "pkg/m.py", "qualified_name": GONE}]
    assert anchors.attach(tmp_path, items) == [("pkg/m.py", GONE)]
    assert items[0]["sym"] == "scip fn" and "sym" not in items[1]
    assert anchors.tag("scip fn") == " [sym: scip fn]" and anchors.tag(None) == ""


def _h(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_dr3337_decision_anchored_to_a_symbol_whose_body_moved_is_stale():
    key = FLAG_SCIP
    other = PREFIX + "`pkg`/other()."
    edges = [
        {"doc_path": "docs/decisions/DR-3337.md", "symbol_key": key, "relation": "decides", "body_hash": _h("def f(): return 1")},
        {"doc_path": "docs/plans/p.md", "symbol_key": key, "relation": "mentions", "body_hash": _h("def f(): return 1")},
        {"doc_path": "docs/decisions/DR-1.md", "symbol_key": other, "relation": "implements", "body_hash": _h("def g(): pass")},
    ]
    now = {key: _h("def f(): return 2"), other: _h("def g(): pass")}
    stale = anchors.stale_anchors(edges, now)
    assert [e["doc_path"] for e in stale] == ["docs/decisions/DR-3337.md"]
    assert anchors.stale_anchors(edges, {key: _h("def f(): return 1"), other: _h("def g(): pass")}) == []
    assert anchors.stale_anchors(edges, {}) == []


def test_symbols_in_lists_unique_top_level_first_and_skips_ambiguous(tmp_path):
    _store(tmp_path, [
        ("pkg/m.py", "zeta", "function", "", "scip zeta"),
        ("pkg/m.py", "alpha", "function", "", "scip alpha"),
        ("pkg/m.py", "meth", "method", "Cls", "scip meth"),
        ("pkg/m.py", "twin", "function", "", "scip t1"),
        ("pkg/m.py", "twin", "function", "", "scip t2"),
        ("pkg/n.py", "bare", "function", "", None),
    ])
    got = anchors.symbols_in(tmp_path, ["pkg/m.py", "pkg/n.py", "pkg/none.py"], per_file=3)
    assert list(got) == ["pkg/m.py"]
    assert [(s["qualified_name"], s["sym"]) for s in got["pkg/m.py"]] == [
        ("alpha", "scip alpha"), ("zeta", "scip zeta"), ("Cls.meth", "scip meth")]
    assert len(anchors.symbols_in(tmp_path, ["pkg/m.py"], per_file=1)["pkg/m.py"]) == 1
    assert anchors.symbols_in(tmp_path / "absent", ["pkg/m.py"]) == {}
    assert not (tmp_path / "absent").exists()
