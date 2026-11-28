"""Inventory-mode emit of 8 certified ~30 KB plans: every part under the Workflow script
cap and no refusal, through `cli.main` at default arguments, `--row-budget` and `--lanes`.

Also records the per-plan byte attribution: the same plan emitted via `--plan` and as a
one-plan inventory part."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import emit as emit_module
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.session.record_homes import home_dir

pytestmark = [pytest.mark.complete]

_RUN_ID = "20261124T000000-eight"
_PLANS = 8
_ROWS_PER_PLAN = 5
_ROWS_LISTED = 3
_PLAN_BYTES = 30_000
_HOT_PLANS = 4
_CAP = emit_module._WORKFLOW_SCRIPT_BYTE_CAP
_HEADER = (
    "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _plan_text(n: int) -> str:
    rows = []
    for r in range(1, _ROWS_PER_PLAN + 1):
        footprint = "src/hot.py" if (r == 1 and n <= _HOT_PLANS) else f"src/p{n}_{r}.py"
        rows.append(
            f"- id: P{n}-C{r}\n  title: plan {n} row {r}\n  change_kind: script-edit\n"
            f"  surface: {footprint}\n  writes: [{footprint}]\n  disposition: open\n"
            f"  body: |\n    Deliver plan {n} row {r}.\n"
        )
    head = (
        f"---\ntitle: \"Plan {n}\"\nstatus: certified\n"
        "prime_exit_criterion:\n  statement: the probe passes\n  falsifier:\n"
        "    how: python -m pytest tests/test_probe.py\n    baseline_output: RED at base\n"
        "    expected_when_true: GREEN\n---\n\n"
        f"# Plan {n}\n\n## Goal\n\nDeliver plan {n}.\n\n"
    )
    tail = "\n## Tasks\n\n```yaml plan-tasks\n" + "\n".join(rows) + "```\n"
    pad_line = "Padding prose that stands in for the plan's rationale and design notes.\n"
    padding = pad_line * ((_PLAN_BYTES - len(head) - len(tail)) // len(pad_line))
    return head + padding + tail


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    lines = []
    for n in range(1, _PLANS + 1):
        (plans / f"p{n}.md").write_text(_plan_text(n), encoding="utf-8", newline="\n")
        for r in range(1, _ROWS_LISTED + 1):
            fp = "src/hot.py" if (r == 1 and n <= _HOT_PLANS) else f"src/p{n}_{r}.py"
            lines.append(
                f"| P{n}-C{r} | `docs/plans/p{n}.md` | plan {n} row {r} | `{fp}` | — "
                f"| scoped pytest | S | queued |"
            )
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    inventory = inv_dir / f"{_RUN_ID}.md"
    inventory.write_text(
        f"---\nrun_id: {_RUN_ID}\nstart_sha: {'a' * 40}\n---\n\n## Chunk table\n\n{_HEADER}"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return inventory


def _emitted(tmp_path: Path) -> "list[Path]":
    return sorted(tmp_path.rglob("*.workflow.mjs"))


def _run(tmp_path: Path, extra: "list[str]", capsys) -> int:
    inventory = _repo(tmp_path)
    argv = ["--inventory", str(inventory), "--repo-root", str(tmp_path)]
    if "--lanes" not in extra:
        argv += ["--out", str(tmp_path / "run.workflow.mjs")]
    capsys.readouterr()
    return cli_module.main([*argv, *extra])


def _assert_all_under_cap(tmp_path: Path) -> None:
    scripts = _emitted(tmp_path)
    assert scripts, "no script emitted"
    sizes = {p.name: len(p.read_bytes()) for p in scripts}
    assert max(sizes.values()) < _CAP, sizes


def test_fixture_plans_are_about_thirty_kb(tmp_path):
    _repo(tmp_path)
    sizes = [len(p.read_bytes()) for p in (tmp_path / "docs" / "plans").glob("p*.md")]
    assert len(sizes) == _PLANS and all(28_000 <= s <= 32_000 for s in sizes), sizes


def test_default_inventory_emit_every_part_under_cap(tmp_path, capsys):
    assert _run(tmp_path, [], capsys) == cli_module.EXIT_OK
    _assert_all_under_cap(tmp_path)


@pytest.mark.parametrize("budget", ["100", "15", "6"])
def test_row_budget_inventory_emit_every_part_under_cap(tmp_path, capsys, budget):
    assert _run(tmp_path, ["--row-budget", budget], capsys) == cli_module.EXIT_OK
    _assert_all_under_cap(tmp_path)


def test_lanes_inventory_emit_every_part_under_cap(tmp_path, capsys):
    assert _run(tmp_path, ["--lanes"], capsys) == cli_module.EXIT_OK
    _assert_all_under_cap(tmp_path)


def test_single_plan_inventory_part_is_close_to_its_plan_emit(tmp_path, capsys):
    """Attribution: one plan via `--plan` versus the same plan as a one-plan inventory."""
    inventory = _repo(tmp_path)
    plan = tmp_path / "docs" / "plans" / "p8.md"
    plan_out = tmp_path / "plan.workflow.mjs"
    assert (
        cli_module.main(["--plan", str(plan), "--out", str(plan_out), "--repo-root", str(tmp_path)])
        == cli_module.EXIT_OK
    )
    text = inventory.read_text(encoding="utf-8")
    one_plan = "\n".join(
        ln for ln in text.splitlines() if not ln.startswith("| P") or "docs/plans/p8.md" in ln
    )
    inventory.write_text(one_plan + "\n", encoding="utf-8", newline="\n")
    inv_out = tmp_path / "one.workflow.mjs"
    capsys.readouterr()
    assert (
        cli_module.main(["--inventory", str(inventory), "--out", str(inv_out), "--repo-root", str(tmp_path)])
        == cli_module.EXIT_OK
    )
    plan_bytes = len(plan_out.read_bytes())
    inv_bytes = len(inv_out.read_bytes())
    print(json.dumps({"plan_emit_bytes": plan_bytes, "one_plan_inventory_bytes": inv_bytes}))
    assert inv_bytes - plan_bytes <= 20_000
