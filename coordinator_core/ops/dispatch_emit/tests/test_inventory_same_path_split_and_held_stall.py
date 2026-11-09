"""The example-game-repo shape: 20 plans write one shared path (109 rows, budget 100). The tranche
mint cuts the group in dependency order across sequential tranches, and a pass whose every
emitted row is held by the family's last tranche reports a named stall."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import tranche_chain
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _plan_text,
    _repo,
)

_SHARED = "tools/likeness-prototype/likeness/pipeline/manifest.py"
_BUDGET = 100
_SIZES = [6] * 9 + [5] * 11
_HOLD_REASON = "t5 strand landed by hand, coded flip awaits"
_RUN = "20261008T000000-tr"


def _plans() -> dict:
    assert len(_SIZES) == 20 and sum(_SIZES) == 109
    plans: dict = {}
    for k, n in enumerate(_SIZES, start=1):
        name = f"p{k:02d}"
        rows = [
            {
                "id": f"C{j}",
                "title": f"{name} C{j}",
                "surface": _SHARED,
                "writes": [_SHARED, f"src/{name}_{j}.py"],
                "change_kind": "code-edit",
            }
            for j in range(n)
        ]
        if k > 1:
            prev = f"p{k - 1:02d}"
            rows[0]["depends_on_plan"] = [
                {"plan": f"docs/plans/{prev}.md", "chunk": f"C{_SIZES[k - 2] - 1}", "gate_kind": "output-consumption-runtime"}
            ]
        plans[name] = rows
    return plans


def _example_game_repo(tmp_path: Path) -> Path:
    plans = _plans()
    return _repo(tmp_path, plans, [(name, "—") for name in plans])


def _mint(inv: Path) -> tuple:
    report: dict = {}
    text, _ = im.mint_spine(str(inv), row_budget=_BUDGET, tranche_out=report, accounting_out={})
    return text, report


def _hold_first_tranche(inv: Path, rows: list) -> None:
    """A `-t1` record beside the base, and its script's receipt holding every first-tranche row."""
    (inv.parent / f"{inv.stem}-t1.md").write_text(inv.read_text(encoding="utf-8"), encoding="utf-8")
    receipt = inv.parent / f"{_RUN}-t1.workflow.mjs.emitted.json"
    receipt.write_text(
        json.dumps({"hold": {"rows": rows, "reason": _HOLD_REASON, "by": "sess-1", "at": "2026-11-09T10:00:00"}}),
        encoding="utf-8",
    )


def _all_rows(tmp_path: Path) -> tuple:
    inv = _example_game_repo(tmp_path)
    chunk_rows = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    rows = im.mint_rows(chunk_rows, inventory_path=inv)
    return rows, [r["id"] for r in chunk_rows]


def test_group_splits_in_dependency_order_each_tranche_within_budget(tmp_path):
    rows, ids = _all_rows(tmp_path)
    assert len(rows) == 109
    left, order = list(rows), []
    while left:
        placed = {r["id"] for r in rows if r not in left}
        selected, report = im.select_tranche(left, ids, _BUDGET)
        assert selected, "the whole shared-path group was deferred"
        assert report["rows"] <= _BUDGET
        order.extend(report["plans"])
        taken = {r["id"] for r in selected}
        for r in selected:
            for edge in r.get("depends_on", []):
                assert edge["chunk"] in placed | taken
        left = [r for r in left if r["id"] not in taken]
        for r in left:
            r["depends_on"] = [e for e in r.get("depends_on", []) if e["chunk"] not in taken]
    assert order == sorted(order) and len(order) == 20


def test_first_tranche_reports_the_chain_owes_another_pass(tmp_path):
    rows, ids = _all_rows(tmp_path)
    _, report = im.select_tranche(rows, ids, _BUDGET)
    assert report["remaining"]["passes_remaining"] >= 1
    assert "stall" not in report


def test_held_prefix_reports_a_named_stall_and_stops_the_chain(tmp_path):
    inv = _example_game_repo(tmp_path)
    _, first = _mint(inv)
    first_rows = [f"{plan}.C{j}" for plan in first["plans"] for j in range(_SIZES[int(plan[1:]) - 1])]
    assert first["rows"] == len(first_rows)
    _hold_first_tranche(inv, first_rows)

    text, report = _mint(inv)

    stall = report["stall"]
    assert sorted(stall["rows"]) == sorted(first_rows)
    assert stall["held_by"] == {row: _HOLD_REASON for row in stall["rows"]}
    assert report["remaining"]["passes_remaining"] == 0

    spine = inv.parent / f"{_RUN}-t2.spine.md"
    stamped = tranche_chain.stamp_tranche_state(
        "---\nderived_from: mise inventory record\n---\n" + text,
        _BUDGET,
        report["remaining"]["passes_remaining"],
        stall=report["stall"],
    )
    spine.write_text(stamped, encoding="utf-8")
    script = spine.with_name(spine.name.removesuffix(".md") + ".workflow.mjs")
    script.write_text("x", encoding="utf-8")
    assert tranche_chain.next_tranche_request(tmp_path, script) is None
    assert _HOLD_REASON in tranche_chain.stalled_tranche(spine)


def test_unheld_first_tranche_stamp_still_chains_to_the_next_pass(tmp_path):
    inv = _example_game_repo(tmp_path)
    text, report = _mint(inv)
    spine = inv.parent / f"{_RUN}-t1.spine.md"
    spine.write_text(
        tranche_chain.stamp_tranche_state(
            "---\nderived_from: mise inventory record\n---\n" + text,
            _BUDGET,
            report["remaining"]["passes_remaining"],
            stall=report.get("stall"),
        ),
        encoding="utf-8",
    )
    script = spine.with_name(spine.name.removesuffix(".md") + ".workflow.mjs")
    script.write_text("x", encoding="utf-8")
    request = tranche_chain.next_tranche_request(tmp_path, script)
    assert request is not None and request["row_budget"] == _BUDGET


def test_select_tranche_is_cheap_and_spawns_no_process(tmp_path, monkeypatch):
    rows, ids = _all_rows(tmp_path)

    def _no_spawn(*args, **kwargs):
        raise AssertionError("select_tranche spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    start = time.process_time()
    selected, _ = im.select_tranche(rows, ids, _BUDGET)
    elapsed = time.process_time() - start
    assert selected
    assert elapsed < 0.5, f"select_tranche took {elapsed * 1000:.0f}ms process time"


def test_whole_set_part_count_is_cheap_and_spawns_no_process(tmp_path, monkeypatch):
    inv = _example_game_repo(tmp_path)

    def _no_spawn(*args, **kwargs):
        raise AssertionError("whole_set_part_count spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    start = time.process_time()
    count = im.whole_set_part_count(str(inv), 100, _BUDGET)
    elapsed = time.process_time() - start
    assert count == 2
    assert elapsed < 0.5, f"whole_set_part_count took {elapsed * 1000:.0f}ms process time"
