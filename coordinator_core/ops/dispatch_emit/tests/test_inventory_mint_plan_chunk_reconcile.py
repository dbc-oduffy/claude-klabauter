"""
`mint_rows` reconciles each plan-sourced single-chunk row against its source
chunk: repairs `writes`/`writes_under`/`depends_on`, carries `deferred_until`,
and refuses a chunk edge whose target is unaccounted for.
"""

from __future__ import annotations

import textwrap
import warnings
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.spine_read import read_spine


def _plan(rows: list) -> str:
    return (
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        + yaml.safe_dump(rows, sort_keys=False)
        + "```\n"
    )


def _chunk(cid, writes=None, **extra):
    row = {
        "id": cid,
        "title": f"chunk {cid}",
        "change_kind": "code-edit",
        "surface": (writes or ["src/x.py"])[0],
        "body": f"Spec: docs/plans/p.md ({cid})\nbody\n",
        "writes": writes or ["src/x.py"],
    }
    row.update(extra)
    return row


def _inventory(table_rows: list) -> str:
    head = (
        "---\nrun_id: 20261006T000000-fixture\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    return head + "".join(
        f"| {i} | `docs/plans/p.md` | s {i} | {fp} | {deps} | v | S | {disp} |\n"
        for i, fp, deps, disp in table_rows
    )


def _mint(tmp_path: Path, plan_rows: list, table_rows: list, **kw):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(_plan(plan_rows), encoding="utf-8")
    inv = tmp_path / "state" / "mise-inventory" / "x.md"
    inv.write_text(_inventory(table_rows), encoding="utf-8")
    rows = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        minted = im.mint_rows(rows, inventory_path=inv, **kw)
    return {r["id"]: r for r in minted}, caught, inv


def test_dropped_writes_under_is_repaired(tmp_path):
    plan = [_chunk("C5", ["src/a.py"], writes_under=["out/dir/"])]
    by_id, caught, _ = _mint(tmp_path, plan, [("P091-C5", "`src/a.py`", "—", "pending")])
    assert by_id["P091-C5"]["writes_under"] == ["out/dir/"]
    assert any("P091-C5" in str(w.message) and "out/dir/" in str(w.message) for w in caught)


def test_dash_deps_cell_gains_live_chunk_edge(tmp_path):
    plan = [
        _chunk("C1", ["src/a.py"]),
        _chunk(
            "C3",
            ["src/b.py"],
            depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}],
        ),
    ]
    by_id, _, _ = _mint(
        tmp_path,
        plan,
        [("P057-C1", "`src/a.py`", "—", "pending"), ("P057-C3", "`src/b.py`", "—", "pending")],
    )
    assert by_id["P057-C3"]["depends_on"] == [
        {"chunk": "P057-C1", "gate_kind": "epistemic-premise"}
    ]


def test_unwritable_gate_kind_falls_back(tmp_path):
    plan = [
        _chunk("C1", ["src/a.py"]),
        _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C1", "gate_kind": "other"}]),
    ]
    by_id, _, _ = _mint(
        tmp_path,
        plan,
        [("P1-C1", "`src/a.py`", "—", "pending"), ("P1-C2", "`src/b.py`", "—", "pending")],
    )
    assert by_id["P1-C2"]["depends_on"][0]["gate_kind"] == "output-consumption-runtime"


def test_edge_to_closed_satisfied_row_is_dropped(tmp_path):
    plan = [_chunk("C1", ["src/a.py"]), _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C1"}])]
    by_id, _, _ = _mint(
        tmp_path,
        plan,
        [("P1-C1", "`src/a.py`", "—", "landed"), ("P1-C2", "`src/b.py`", "—", "pending")],
    )
    assert "depends_on" not in by_id["P1-C2"]


def test_edge_to_routed_out_row_routes_the_row_out(tmp_path):
    plan = [_chunk("C1", ["src/a.py"]), _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C1"}])]
    by_id, _, _ = _mint(
        tmp_path,
        plan,
        [("P1-C1", "`src/a.py`", "—", "routed out"), ("P1-C2", "`src/b.py`", "—", "pending")],
    )
    assert by_id == {}


def test_absent_target_coded_in_plan_is_satisfied(tmp_path):
    plan = [
        _chunk("C0", ["src/z.py"], disposition="coded"),
        _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C0"}]),
    ]
    by_id, _, _ = _mint(tmp_path, plan, [("P1-C2", "`src/b.py`", "—", "pending")])
    assert "depends_on" not in by_id["P1-C2"]


def test_absent_open_target_raises_naming_row(tmp_path):
    plan = [_chunk("C0", ["src/z.py"]), _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C0"}])]
    with pytest.raises(im.PlanDependencyUnaccountedError, match="P1-C2"):
        _mint(tmp_path, plan, [("P1-C2", "`src/b.py`", "—", "pending")])


def test_deferred_until_is_carried_and_withholds_row_and_dependent(tmp_path):
    hold = {"reason": "pin first", "revisit_trigger": "after pin"}
    plan = [
        _chunk("C1", ["src/a.py"], deferred_until=hold),
        _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C1"}]),
    ]
    by_id, _, inv = _mint(
        tmp_path,
        plan,
        [("P1-C1", "`src/a.py`", "—", "pending"), ("P1-C2", "`src/b.py`", "—", "pending")],
    )
    assert by_id["P1-C1"]["deferred_until"] == hold
    text, path = im.mint_spine(str(inv))
    path.write_text(text, encoding="utf-8")
    exclusions: list = []
    assert read_spine(path, exclusions) == []


def test_cell_entry_beyond_chunk_declaration_is_kept(tmp_path):
    plan = [_chunk("C1", ["src/a.py"])]
    by_id, _, _ = _mint(
        tmp_path, plan, [("P1-C1", "`src/a.py`, `tests/test_a.py`", "—", "pending")]
    )
    assert by_id["P1-C1"]["writes"] == ["src/a.py", "tests/test_a.py"]


def test_empty_cell_footprint_is_repaired_from_chunk(tmp_path):
    plan = [_chunk("C1", ["src/a.py"])]
    by_id, _, _ = _mint(tmp_path, plan, [("P1-C1", "—", "—", "pending")])
    assert by_id["P1-C1"]["writes"] == ["src/a.py"]


def test_plan_file_read_count_is_unchanged(tmp_path, monkeypatch):
    plan = [
        _chunk("C1", ["src/a.py"]),
        _chunk("C2", ["src/b.py"], depends_on=[{"chunk": "C1"}]),
        _chunk("C3", ["src/c.py"]),
    ]
    reads: list = []
    orig = Path.read_text

    def counting(self, *a, **k):
        if self.name == "p.md":
            reads.append(self)
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    _mint(
        tmp_path,
        plan,
        [
            ("P1-C1", "`src/a.py`", "—", "pending"),
            ("P1-C2", "`src/b.py`", "—", "pending"),
            ("P1-C3", "`src/c.py`", "—", "pending"),
        ],
    )
    assert len(reads) == 1


def test_no_inventory_path_changes_nothing():
    rows = im.parse_chunk_table(
        textwrap.dedent(
            """\
            ## Chunk table

            | id | spec path | summary | footprint | deps | verification | complexity | disposition |
            |---|---|---|---|---|---|---|---|
            | C1 | `docs/plans/p.md` | s | `src/a.py` | — | v | S | pending |
            """
        )
    )
    assert im.mint_rows(rows)[0]["writes"] == ["src/a.py"]
