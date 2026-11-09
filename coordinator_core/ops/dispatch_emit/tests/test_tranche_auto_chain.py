"""End to end: a default emit over a 3-tranche inventory chains t1 -> t2 -> t3 through
`land_run` alone and stops when `passes_remaining` reaches 0. The headless fire is an
in-process recorder; the emit is the real `dispatch.emit` body."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import land_run, tranche_chain
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.emission_receipt import stamp_landed
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_inventory_emit_plan_edge_and_tranche_exit import (
    _BASE,
    _mark_coded,
    _review_loaders,  # noqa: F401  (autouse fixture)
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import _n_rows, _repo

_BUDGET = 3
_PLANS = (("pa", "A"), ("pb", "B"), ("pc", "C"))


@pytest.fixture(autouse=True)
def _default_budget(monkeypatch):
    monkeypatch.setattr(op_module, "DEFAULT_TRANCHE_ROW_BUDGET", _BUDGET)


class _Chain:
    """Replaces the detached spawn: runs `run_chain` in-process with the real emit and a recording fire."""

    def __init__(self) -> None:
        self.requests: list = []
        self.emitted: list = []
        self.fired: list = []

    def spawn(self, root: str, run_id: str, request: dict) -> None:
        def emit(r: str, req: dict) -> dict:
            self.requests.append(req)
            out = tranche_chain._emit_next(r, req)
            self.emitted.append(out)
            return out

        outcome = tranche_chain.run_chain(
            root, run_id, request, emit=emit, fire=lambda script, r, rid: self.fired.append((script, rid))
        )
        assert outcome["chain_error"] is None, outcome


def _chain_record(root: Path, run_id: str):
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    path = record_dir(root, run_id) / tranche_chain.CHAIN_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _land(tmp_path: Path, script: Path, plan: str, prefix: str) -> None:
    """The run's plan is coded, its script receipt landed, and `land_run` hands it to the chain."""
    _mark_coded(tmp_path, plan, prefix, _BUDGET)
    stamp_landed(script, "abc123")
    land_run._chain_landed_tranche(
        tmp_path,
        {"run_id": script.name.removesuffix(".workflow.mjs"), "script_path": str(script.relative_to(tmp_path)), "incomplete_chunks": []},
        {"committed": True, "sha": "a" * 40},
    )


def test_default_emit_chains_t2_and_t3_then_stops_at_zero_passes(tmp_path, monkeypatch):
    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    chain = _Chain()
    monkeypatch.setattr(tranche_chain, "_spawn_detached", chain.spawn)

    t1 = _dispatch_emit({"inventory_path": str(inventory), "target_root": str(tmp_path), "max_rows": 2})
    assert t1["ok"] is True, t1["findings"]
    assert f"{_BASE}-t1" in Path(t1["path"]).name
    assert t1["tranche"]["row_budget"] == _BUDGET
    assert t1["tranche"]["remaining"]["passes_remaining"] == 2

    _land(tmp_path, Path(t1["path"]), "pa", "A")
    assert [Path(e["path"]).name for e in chain.emitted] == [f"{_BASE}-t2.spine.workflow.mjs"]
    assert chain.emitted[0]["tranche"]["remaining"]["passes_remaining"] == 1

    _land(tmp_path, Path(chain.emitted[0]["path"]), "pb", "B")
    assert [Path(e["path"]).name for e in chain.emitted] == [
        f"{_BASE}-t2.spine.workflow.mjs",
        f"{_BASE}-t3.spine.workflow.mjs",
    ]
    assert chain.emitted[1]["tranche"]["remaining"]["passes_remaining"] == 0

    _land(tmp_path, Path(chain.emitted[1]["path"]), "pc", "C")

    expected = {"inventory_path": str(inventory), "target_root": str(tmp_path), "row_budget": _BUDGET}
    assert chain.requests == [expected, expected]
    assert [Path(script).name for script, _ in chain.fired] == [Path(e["path"]).name for e in chain.emitted]
    assert all(e["tranche"]["row_budget"] == _BUDGET for e in chain.emitted)

    names = [Path(p).name.removesuffix(".workflow.mjs") for p in (t1["path"], *(e["path"] for e in chain.emitted))]
    hops = [_chain_record(tmp_path, run_id) for run_id in names]
    assert [h and h["chained"] for h in hops] == [names[1], names[2], None]
    assert hops[0]["chain_error"] is None and hops[1]["chain_error"] is None
    assert hops[2] is None, "the last tranche owes nothing: no spawn, no record"


def _emit_parts(tmp_path: Path, inventory: Path, capsys) -> "list[Path]":
    from coordinator_core.ops.dispatch_emit import cli as cli_module

    capsys.readouterr()
    rc = cli_module.main(
        ["--inventory", str(inventory), "--repo-root", str(tmp_path), "--max-rows", "2"]
    )
    assert rc == cli_module.EXIT_OK, capsys.readouterr().out
    return sorted(inventory.parent.glob("*-p[0-9].workflow.mjs"))


def test_whole_set_parts_chain_p2_then_p3_from_the_terminal_commit_alone(tmp_path, monkeypatch, capsys):
    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    scripts = _emit_parts(tmp_path, inventory, capsys)
    assert [s.name for s in scripts] == [f"{_BASE}-p{i}.workflow.mjs" for i in (1, 2, 3)]
    chain = _Chain()
    monkeypatch.setattr(tranche_chain, "_spawn_detached", chain.spawn)
    monkeypatch.setattr(land_run, "_chain_landed_tranche", lambda *a, **k: pytest.fail("land_run was invoked"))

    for script in scripts[:2]:
        outcome = tranche_chain.chain_after_part(tmp_path, script.name.removesuffix(".workflow.mjs"), script)
        assert outcome["spawned"] is True, outcome
    last = tranche_chain.chain_after_part(tmp_path, f"{_BASE}-p3", scripts[2])

    assert [r["inventory_part"] for r in chain.requests] == [[2, 3], [3, 3]]
    assert [Path(e["path"]).name for e in chain.emitted] == [s.name for s in scripts[1:]]
    assert [Path(script).name for script, _ in chain.fired] == [s.name for s in scripts[1:]]
    assert last == {"chained": None, "chain_error": None, "request": None, "spawned": False}


def test_part_already_chained_is_not_chained_again_and_a_landing_does_not_refire_it(tmp_path, monkeypatch, capsys):
    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    p1 = _emit_parts(tmp_path, inventory, capsys)[0]
    run_id = p1.name.removesuffix(".workflow.mjs")
    spawns: list = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda root, rid, request: spawns.append(request))

    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is True
    tranche_chain.write_chain_record(tmp_path, run_id, f"{_BASE}-p2", None)

    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is False
    assert tranche_chain.chain_after_land(tmp_path, run_id, p1)["spawned"] is False
    assert len(spawns) == 1


def test_refired_hand_off_before_the_chain_record_exists_spawns_part_two_once(tmp_path, monkeypatch, capsys):
    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    p1 = _emit_parts(tmp_path, inventory, capsys)[0]
    run_id = p1.name.removesuffix(".workflow.mjs")
    spawns: list = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda root, rid, request: spawns.append(request))

    first = tranche_chain.chain_after_part(tmp_path, run_id, p1)
    second = tranche_chain.chain_after_part(tmp_path, run_id, p1)

    assert first["spawned"] is True and second["spawned"] is False
    assert len(spawns) == 1, "no tranche-chain.json yet: the claim alone stops the second spawn"


def test_stale_claim_from_a_dead_detached_chain_is_taken_over_and_a_fresh_one_is_not(tmp_path, monkeypatch, capsys):
    import os
    import time

    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    p1 = _emit_parts(tmp_path, inventory, capsys)[0]
    run_id = p1.name.removesuffix(".workflow.mjs")
    spawns: list = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda root, rid, request: spawns.append(request))

    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is True
    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is False
    claim = record_dir(tmp_path, run_id) / tranche_chain.CLAIM_FILE
    old = time.time() - tranche_chain.STALE_CLAIM_SECONDS - 5
    os.utime(claim, (old, old))
    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is True
    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is False
    assert len(spawns) == 2


def test_stalled_hand_off_records_the_stall_and_a_refire_retries(tmp_path, monkeypatch, capsys):
    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    p1 = _emit_parts(tmp_path, inventory, capsys)[0]
    run_id = p1.name.removesuffix(".workflow.mjs")

    def _boom(root, rid, request):
        raise OSError("no spawn")

    monkeypatch.setattr(tranche_chain, "_spawn_detached", _boom)
    stalled = tranche_chain.chain_after_part(tmp_path, run_id, p1)
    assert stalled["spawned"] is False and "no spawn" in stalled["chain_error"]
    assert "no spawn" in _chain_record(tmp_path, run_id)["chain_error"]

    spawns: list = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda root, rid, request: spawns.append(request))
    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is True
    assert len(spawns) == 1


def test_failed_emit_in_the_detached_chain_releases_the_claim(tmp_path, monkeypatch, capsys):
    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    p1 = _emit_parts(tmp_path, inventory, capsys)[0]
    run_id = p1.name.removesuffix(".workflow.mjs")

    def _failing_spawn(root, rid, request):
        outcome = tranche_chain.run_chain(root, rid, request, emit=lambda r, q: {"ok": False, "error": "nope"})
        assert outcome["chain_error"]

    monkeypatch.setattr(tranche_chain, "_spawn_detached", _failing_spawn)
    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is True
    assert _chain_record(tmp_path, run_id)["chain_error"]

    spawns: list = []
    monkeypatch.setattr(tranche_chain, "_spawn_detached", lambda root, rid, request: spawns.append(request))
    assert tranche_chain.chain_after_part(tmp_path, run_id, p1)["spawned"] is True
    assert len(spawns) == 1


def test_fire_of_an_over_cap_inventory_cuts_parts_and_fires_part_one_only(tmp_path, monkeypatch, capsys):
    import coordinator_core.ops.workflow_fire.fire as fire_module
    from coordinator_core.ops.dispatch_emit import cli as cli_module
    from coordinator_core.ops.dispatch_emit import inventory_mint

    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    fired: list = []
    monkeypatch.setattr(fire_module, "fire_workflow", lambda path, **kw: fired.append(path) or {"fired": True})
    counts: list = []
    real = inventory_mint.whole_set_part_count
    monkeypatch.setattr(inventory_mint, "whole_set_part_count", lambda *a, **k: counts.append(1) or real(*a, **k))

    rc = cli_module.main(["--inventory", str(inventory), "--repo-root", str(tmp_path), "--max-rows", "2", "--fire"])

    assert rc == cli_module.EXIT_OK, capsys.readouterr().out
    assert [Path(p).name for p in fired] == [f"{_BASE}-p1.workflow.mjs"]
    assert len(sorted(inventory.parent.glob("*-p[0-9].workflow.mjs"))) == 3
    assert len(counts) == 1, "the part count is computed once, not per part"


def test_terminal_commit_chain_failure_is_recorded_and_never_raised(tmp_path, monkeypatch, capsys):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    inventory = _repo(
        tmp_path,
        {name: _n_rows(prefix, _BUDGET) for name, prefix in _PLANS},
        [(name, "—") for name, _ in _PLANS],
    )
    p1 = _emit_parts(tmp_path, inventory, capsys)[0]
    run_id = p1.name.removesuffix(".workflow.mjs")
    monkeypatch.setattr(tranche_chain, "chain_after_part", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("coordinator_core.lifecycle.main_worktree_root", lambda root: Path(root))

    terminal_commit._chain_next_part(
        {"run_id": run_id, "script_path": str(p1.relative_to(tmp_path))}, tmp_path, {"committed": True}
    )

    assert "boom" in _chain_record(tmp_path, run_id)["chain_error"]


def test_non_part_script_commit_never_reaches_the_chain(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    monkeypatch.setattr(tranche_chain, "chain_after_part", lambda *a, **k: pytest.fail("chained a non-part"))
    terminal_commit._chain_next_part(
        {"run_id": "r", "script_path": "x/r.spine.workflow.mjs"}, tmp_path, {"committed": True}
    )
