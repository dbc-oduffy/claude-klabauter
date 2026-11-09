"""Falsifier: a default `--inventory` emit over a certified set larger than the row budget
cuts the WHOLE live set into topologically ordered `-pN` parts (a dependent listed before its
predecessor still lands in the same or a later part), withholds only an uncleared
`external_gate` plan and its transitive `depends_on_plan` dependents, and each part's committed
terminal commit hands the next part to the chain with no `land_run`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import land_run, tranche_chain, terminal_commit
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.tests.test_inventory_emit_plan_edge_and_tranche_exit import (
    _review_loaders,  # noqa: F401  (autouse fixture)
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import _chunk, _n_rows, _repo
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

pytestmark = [pytest.mark.complete]

_BUDGET = 3
_GATE = "output-consumption-runtime"
_SPINE_PART_RE = re.compile(r"-p(\d+)\.spine\.md$")


@pytest.fixture(autouse=True)
def _row_budget(monkeypatch):
    monkeypatch.setattr(op_module, "DEFAULT_TRANCHE_ROW_BUDGET", _BUDGET)


def _inventory(tmp_path: Path) -> Path:
    """pb (B1 gated on pa's A2) is listed BEFORE pa; pc, pd are free; pg carries an uncleared
    external_gate and ph depends_on_plan pg. 4 free-able plans x 3 rows over a budget of 3."""
    dependent = _n_rows("B", 3)
    dependent[1]["depends_on_plan"] = [{"plan": "docs/plans/pa.md", "chunk": "A2", "gate_kind": _GATE}]
    gated = [_chunk("G0", "src/g0.py", external_gate=[{"id": "owner-lands-first"}])]
    after_gate = [
        _chunk("H0", "src/h0.py", depends_on_plan=[{"plan": "docs/plans/pg.md", "chunk": "G0", "gate_kind": _GATE}])
    ]
    order = ["pb", "pa", "pc", "pd", "pg", "ph"]
    return _repo(
        tmp_path,
        {"pa": _n_rows("A", 3), "pb": dependent, "pc": _n_rows("C", 3), "pd": _n_rows("D", 3), "pg": gated, "ph": after_gate},
        [(name, "—") for name in order],
    )


def _emit_default(tmp_path: Path, inventory: Path, capsys) -> None:
    capsys.readouterr()
    rc = cli_module.main(
        ["--inventory", str(inventory), "--out", str(tmp_path / "run.workflow.mjs"),
         "--repo-root", str(tmp_path), "--max-rows", "2"]
    )
    out = capsys.readouterr().out
    assert rc == cli_module.EXIT_OK, out
    assert json.loads(out)


def _part_spines(inventory: Path) -> "dict[int, Path]":
    found = {}
    for spine in inventory.parent.glob("*.spine.md"):
        match = _SPINE_PART_RE.search(spine.name)
        if match:
            found[int(match.group(1))] = spine
    return dict(sorted(found.items()))


def _plans_of(spine: Path) -> "set[str]":
    return {row["id"].split(".")[0] for row in im.load_rows(spine.read_text(encoding="utf-8")).rows}


def _script_of(spine: Path) -> Path:
    return spine.with_name(spine.name.removesuffix(".spine.md") + ".workflow.mjs")


@pytest.fixture
def emitted(tmp_path, capsys):
    inventory = _inventory(tmp_path)
    _emit_default(tmp_path, inventory, capsys)
    return tmp_path, inventory, _part_spines(inventory)


def test_default_emit_cuts_the_whole_set_into_ordered_parts(emitted):
    _, _, parts = emitted
    assert len(parts) >= 3, f"expected >= 3 -pN parts over the whole set, got {sorted(parts)}"
    where = {}
    for index, spine in parts.items():
        for plan in _plans_of(spine):
            where[plan] = index
    assert where["pa"] <= where["pb"], where


def test_gate_withholds_only_its_subtree(emitted):
    _, _, parts = emitted
    placed = set().union(*(_plans_of(s) for s in parts.values())) if parts else set()
    assert {"pa", "pb", "pc", "pd"} <= placed, placed
    assert not placed & {"pg", "ph"}, placed


def test_each_part_names_the_next_and_the_last_names_none(emitted):
    root, _, parts = emitted
    assert len(parts) >= 3
    last = max(parts)
    for index, spine in parts.items():
        assert read_frontmatter_field(str(spine), "part_index") == str(index)
        assert read_frontmatter_field(str(spine), "part_count") == str(last)
        request = tranche_chain.next_part_request(root, _script_of(spine))
        if index == last:
            assert request is None
        else:
            assert request is not None
            assert request["inventory_part"] == [index + 1, last]
            assert request["target_root"] == str(root)
            assert "inventory_path" in request


def _commit(root: Path, script: Path, reply: dict, monkeypatch) -> None:
    monkeypatch.setattr(terminal_commit, "_entry_head", lambda _root: None)
    monkeypatch.setattr(terminal_commit, "_terminal_commit_inner", lambda *a, **k: dict(reply))
    terminal_commit._terminal_commit(
        {"run_id": script.name.removesuffix(".workflow.mjs"), "script_path": str(script.relative_to(root))},
        root, {}, {},
    )


def _arm(monkeypatch) -> "list[tuple]":
    spawns: list = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda root, run_id, request: spawns.append((run_id, request)))

    def _no_land(*args, **kwargs):
        raise AssertionError("land_run was invoked")

    monkeypatch.setattr(land_run, "_chain_landed_tranche", _no_land)
    monkeypatch.setattr(tranche_chain, "chain_after_land", _no_land)
    return spawns


def test_committed_terminal_commit_of_part_one_spawns_part_two_without_a_landing(emitted, monkeypatch):
    root, _, parts = emitted
    assert len(parts) >= 3
    spawns = _arm(monkeypatch)
    script = _script_of(parts[1])

    _commit(root, script, {"committed": True, "sha": "a" * 40, "paths": ["src/a0.py"]}, monkeypatch)

    assert len(spawns) == 1, spawns
    assert spawns[0][1]["inventory_part"] == [2, max(parts)]


def test_refired_terminal_commit_spawns_part_two_once(emitted, monkeypatch):
    root, _, parts = emitted
    spawns = _arm(monkeypatch)
    script = _script_of(parts[1])
    reply = {"committed": True, "sha": "a" * 40, "paths": ["src/a0.py"]}

    _commit(root, script, reply, monkeypatch)
    _commit(root, script, reply, monkeypatch)

    assert len(spawns) == 1, spawns


def test_refused_terminal_commit_spawns_nothing(emitted, monkeypatch):
    root, _, parts = emitted
    assert len(parts) >= 3
    spawns = _arm(monkeypatch)

    _commit(root, _script_of(parts[1]), {"committed": False, "refused": "run-token"}, monkeypatch)

    assert spawns == []


def test_fire_of_an_over_budget_set_cuts_parts_and_fires_only_part_one(tmp_path, capsys, monkeypatch):
    import coordinator_core.ops.workflow_fire.fire as fire_module

    fired: list = []
    monkeypatch.setattr(fire_module, "fire_workflow", lambda path, **kw: fired.append((path, kw)) or {"fired": True})
    inventory = _inventory(tmp_path)
    capsys.readouterr()
    rc = cli_module.main(
        ["--inventory", str(inventory), "--out", str(tmp_path / "run.workflow.mjs"),
         "--repo-root", str(tmp_path), "--max-rows", "2", "--fire"]
    )
    capsys.readouterr()
    spines = _part_spines(inventory)
    assert rc == cli_module.EXIT_OK
    assert len(spines) > 1
    assert [Path(path).name for path, _ in fired] == [_script_of(spines[1]).name]


def test_chain_failure_before_hand_off_leaves_a_record(emitted, monkeypatch):
    root, _, parts = emitted
    script = _script_of(parts[1])
    _arm(monkeypatch)
    import coordinator_core.lifecycle as lifecycle

    def _boom(_root):
        raise RuntimeError("no main worktree")

    monkeypatch.setattr(lifecycle, "main_worktree_root", _boom)

    _commit(root, script, {"committed": True, "sha": "a" * 40, "paths": ["src/a0.py"]}, monkeypatch)

    assert tranche_chain._already_chained(root, script.name.removesuffix(".workflow.mjs"))
