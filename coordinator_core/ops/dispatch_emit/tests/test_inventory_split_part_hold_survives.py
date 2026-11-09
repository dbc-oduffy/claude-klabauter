"""A split `-pN` emit keeps the inventory's `## Withheld / routed out` hold: a row listed there
(including a `phase: validation` row) lands in no part's spine, whichever route cut the parts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import emit as emit_module
from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.session.record_homes import home_dir

pytestmark = pytest.mark.complete

_RUN_ID = "20261109T000000-holdsplit"
_PLANS = 8
_CHUNKS = 6
_HELD_PLAN = "p3"
_HELD_CHUNK = "C2"


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _plan_text(n: int) -> str:
    rows = []
    for c in range(1, _CHUNKS + 1):
        body = f"body of p{n}.C{c}\n"
        if n == int(_HELD_PLAN[1:]) and f"C{c}" == _HELD_CHUNK:
            body = "phase: validation\n" + body
        row = {
            "id": f"C{c}",
            "title": f"chunk {n}.{c}",
            "change_kind": "code-edit",
            "surface": f"src/m{n}_{c}.py",
            "body": body,
            "writes": [f"src/m{n}_{c}.py"],
        }
        if c == 2:
            row["depends_on"] = [{"chunk": "C1", "gate_kind": "epistemic-premise"}]
        rows.append(row)
    return "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n" + yaml.safe_dump(rows, sort_keys=False) + "```\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    lines = []
    for n in range(1, _PLANS + 1):
        (plans / f"p{n}.md").write_text(_plan_text(n), encoding="utf-8")
        lines.append(
            f"| P{n} | `docs/plans/p{n}.md` | plan {n} | `src/m{n}_1.py`, `src/m{n}_2.py` | — "
            f"| scoped pytest | S | queued |"
        )
    inventory = inv_dir / f"{_RUN_ID}.md"
    inventory.write_text(
        f"---\nrun_id: {_RUN_ID}\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(lines) + "\n\n"
        f"## Withheld / routed out\n\n- `P3:{_HELD_CHUNK}` -- phase: validation (code-first runbook)\n",
        encoding="utf-8",
    )
    return inventory


def _argv(tmp_path: Path, inventory: Path, *extra: str) -> list:
    return [
        "--inventory", str(inventory),
        "--out", str(tmp_path / "run.workflow.mjs"),
        "--repo-root", str(tmp_path),
        *extra,
    ]


def _part_spines(inventory: Path) -> list:
    return sorted(inventory.parent.glob(f"{_RUN_ID}*-p*.spine.md"))


def _fired_ids(spines: list) -> set:
    fired: set = set()
    for spine in spines:
        fired |= {r["id"] for r in im.load_rows(spine.read_text(encoding="utf-8")).rows}
    return fired


def _held_id() -> str:
    return f"P{_HELD_PLAN[1:]}.{_HELD_CHUNK}"


def test_unsplit_mint_holds_the_withheld_row(tmp_path):
    inventory = _repo(tmp_path)
    text, _ = im.mint_spine(str(inventory))
    assert f"id: {_held_id()}\n" not in text
    assert "id: P3.C1\n" in text


def test_whole_set_part_cut_holds_the_withheld_row(tmp_path, capsys):
    inventory = _repo(tmp_path)

    rc = cli_module.main(_argv(tmp_path, inventory, "--max-rows", "10"))

    assert rc == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    assert len(reply["parts"]) >= 2
    spines = _part_spines(inventory)
    assert len(spines) >= 2
    fired = _fired_ids(spines)
    assert _held_id() not in fired
    assert "P3.C1" in fired


@pytest.mark.parametrize("budget", ["100", "30", "20"])
def test_over_cap_tranche_parts_hold_the_withheld_row(tmp_path, monkeypatch, capsys, budget):
    inventory = _repo(tmp_path)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    assert cli_module.main(_argv(tmp_path, inventory, "--row-budget", budget)) == 0
    whole = (tmp_path / "run.workflow.mjs").stat().st_size
    for stale in inventory.parent.glob(f"{_RUN_ID}*"):
        if stale != inventory:
            stale.unlink()
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", whole * 3 // 4)
    capsys.readouterr()

    rc = cli_module.main(_argv(tmp_path, inventory, "--row-budget", budget))

    assert rc == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    assert len(reply["parts"]) >= 2
    spines = _part_spines(inventory)
    assert len(spines) >= 2
    fired = _fired_ids(spines)
    assert _held_id() not in fired
    assert "P3.C1" in fired


def _single_chunk_repo(tmp_path: Path) -> Path:
    """One Chunk-table row per plan chunk (`P3-C2`), the single-chunk reference shape."""
    inventory = _repo(tmp_path)
    lines = []
    for n in range(1, _PLANS + 1):
        for c in range(1, _CHUNKS + 1):
            lines.append(
                f"| P{n}-C{c} | `docs/plans/p{n}.md` | chunk {n}.{c} | `src/m{n}_{c}.py` | — "
                f"| scoped pytest | S | queued |"
            )
    inventory.write_text(
        f"---\nrun_id: {_RUN_ID}\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(lines) + "\n\n"
        f"## Withheld / routed out\n\n- `p3:{_HELD_CHUNK}` -- phase: validation (code-first runbook)\n",
        encoding="utf-8",
    )
    return inventory


def test_single_chunk_row_listed_withheld_is_not_minted(tmp_path):
    inventory = _single_chunk_repo(tmp_path)
    text, _ = im.mint_spine(str(inventory))
    assert "id: P3-C2\n" not in text
    assert "id: P3-C1\n" in text and "id: P4-C2\n" in text


def test_single_chunk_rows_hold_the_withheld_row_in_every_part(tmp_path, capsys):
    inventory = _single_chunk_repo(tmp_path)

    rc = cli_module.main(_argv(tmp_path, inventory, "--max-rows", "10"))

    assert rc == cli_module.EXIT_OK
    assert len(json.loads(capsys.readouterr().out)["parts"]) >= 2
    fired = _fired_ids(_part_spines(inventory))
    assert "P3-C2" not in fired
    assert {"P3-C1", "P4-C2"} <= fired
