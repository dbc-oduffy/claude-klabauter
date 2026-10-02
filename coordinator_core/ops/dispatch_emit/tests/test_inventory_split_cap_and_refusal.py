"""emit-dispatch-workflow over the Workflow script cap: non-zero exit, automatic
whole-plan parts with distinct run ids, and refusal of an inventory outside the
repo's state/mise-inventory/."""

from __future__ import annotations

import json
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import emit as emit_module
from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)

_RUN_ID = "20261002T000000-split"
_ITEMS = 8


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _repo_with_inventory(tmp_path, *, items: int = _ITEMS, deps_on_first: bool = False):
    (tmp_path / ".git").mkdir()
    inv_dir = tmp_path / "state" / "mise-inventory"
    inv_dir.mkdir(parents=True)
    lines = []
    for i in range(1, items + 1):
        deps = "I1" if deps_on_first and i > 1 else "—"
        lines.append(
            f"| I{i} | `docs/plans/p{i}.md` | item {i} | `src/mod_{i}.py` | {deps} "
            f"| scoped pytest | S | queued |"
        )
    text = (
        f"---\nrun_id: {_RUN_ID}\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(lines) + "\n"
    )
    inventory = inv_dir / f"{_RUN_ID}.md"
    inventory.write_text(text, encoding="utf-8")
    return inventory


def _argv(tmp_path, inventory, out_name="run.workflow.mjs"):
    return [
        "--inventory", str(inventory),
        "--out", str(tmp_path / out_name),
        "--repo-root", str(tmp_path),
    ]


def _script_size(path) -> int:
    return len(path.read_bytes())


def _cap_between_whole_and_part(tmp_path, monkeypatch) -> int:
    inventory = _repo_with_inventory(tmp_path)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    assert cli_module.main(_argv(tmp_path, inventory, "whole.workflow.mjs")) == 0
    whole = _script_size(tmp_path / "whole.workflow.mjs")
    params = {
        "inventory_path": str(inventory),
        "output_path": str(tmp_path / "half.workflow.mjs"),
        "inventory_part": [1, 2],
        "target_root": str(tmp_path),
    }
    op_mod._dispatch_emit(params, repo_root=tmp_path)
    half = _script_size(tmp_path / "half.workflow.mjs")
    assert half < whole
    return half + (whole - half) // 4


def test_plan_route_over_cap_exits_non_zero(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    plan = tmp_path / "p.md"
    plan.write_text(
        "---\n---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: t\n  change_kind: script-edit\n  surface: a.py\n"
        "  writes: [a.py]\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", 100)

    rc = cli_module.main(
        ["--plan", str(plan), "--out", str(tmp_path / "p.workflow.mjs"), "--repo-root", str(tmp_path)]
    )

    assert rc == cli_module.EXIT_DATA_ERROR
    assert not (tmp_path / "p.workflow.mjs").exists()


def test_over_cap_inventory_is_split_into_parts_with_distinct_run_ids(tmp_path, monkeypatch, capsys):
    cap = _cap_between_whole_and_part(tmp_path, monkeypatch)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    inventory = tmp_path / "state" / "mise-inventory" / f"{_RUN_ID}.md"
    capsys.readouterr()

    rc = cli_module.main(_argv(tmp_path, inventory))

    assert rc == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    assert [p["part"] for p in reply["parts"]] == ["1/2", "2/2"]
    inv_dir = inventory.parent
    spines = [inv_dir / f"{_RUN_ID}-p{i}.spine.md" for i in (1, 2)]
    assert all(s.is_file() for s in spines)
    rows = [
        {r["id"] for r in im.load_rows(s.read_text(encoding="utf-8")).rows}
        for s in spines
    ]
    assert rows[0].isdisjoint(rows[1])
    assert rows[0] | rows[1] == {f"I{i}" for i in range(1, _ITEMS + 1)}
    for i in (1, 2):
        script = tmp_path / f"run-p{i}.workflow.mjs"
        assert script.is_file() and _script_size(script) <= cap


def test_part_edge_onto_an_earlier_part_is_dropped(tmp_path, monkeypatch):
    inventory = _repo_with_inventory(tmp_path, deps_on_first=True)

    text, path = im.mint_spine(str(inventory), part=(2, 2))

    assert path.name == f"{_RUN_ID}-p2.spine.md"
    assert "chunk: I1" not in text
    assert "I8" in text


def test_part_edge_onto_a_later_part_is_refused(tmp_path):
    inventory = _repo_with_inventory(tmp_path)
    text = inventory.read_text(encoding="utf-8").replace("| item 1 | `src/mod_1.py` | —", "| item 1 | `src/mod_1.py` | I8")
    inventory.write_text(text, encoding="utf-8")

    with pytest.raises(im.InventoryPartError, match="I8"):
        im.mint_spine(str(inventory), part=(1, 2))


def test_fire_with_split_refuses_and_fires_nothing(tmp_path, monkeypatch):
    cap = _cap_between_whole_and_part(tmp_path, monkeypatch)
    monkeypatch.setattr(emit_module, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    inventory = tmp_path / "state" / "mise-inventory" / f"{_RUN_ID}.md"

    rc = cli_module.main([*_argv(tmp_path, inventory), "--fire"])

    assert rc == cli_module.EXIT_DATA_ERROR


def test_inventory_outside_the_repo_is_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    inventory = outside / "inv.md"
    inventory.write_text(
        textwrap.dedent(
            """\
            ---
            run_id: x
            ---

            ## Chunk table

            | id | spec path | summary | footprint | deps | verification | complexity | disposition |
            |---|---|---|---|---|---|---|---|
            | I1 | `docs/plans/p.md` | one | `a.py` | — | t | S | queued |
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    rc = cli_module.main(
        ["--inventory", str(inventory), "--out", str(repo / "x.workflow.mjs")]
    )

    assert rc == cli_module.EXIT_DATA_ERROR
    assert not (repo / "x.workflow.mjs").exists()
    assert not (outside / "x.spine.md").exists()


def test_terminal_commit_maps_a_part_spine_back_to_its_inventory(tmp_path):
    from coordinator_core.ops.dispatch_emit.terminal_commit import _source_rows_by_plan

    inventory = _repo_with_inventory(tmp_path)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "p8.md").write_text(
        "# p\n\n## Tasks\n\n```yaml plan-tasks\n- id: I8\n  title: t\n  surface: a.py\n```\n",
        encoding="utf-8",
    )
    text, spine = im.mint_spine(str(inventory), part=(2, 2))
    spine.write_text(text, encoding="utf-8")

    mapped = _source_rows_by_plan(
        tmp_path, spine.relative_to(tmp_path).as_posix(), ["I8"]
    )

    assert mapped == {"docs/plans/p8.md": {"I8"}}
