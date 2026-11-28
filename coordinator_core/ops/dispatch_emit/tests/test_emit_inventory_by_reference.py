"""Inventory emit carries plan material by reference: the composed script names the plan's
repo-relative path and never inlines its text or an absolute path, and `row_block_bytes`
charges the `_shared` prompt paragraphs once rather than once per row."""

from __future__ import annotations

import re
import subprocess
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

pytestmark = pytest.mark.complete

_RUN_ID = "20261124T000000-byref"
_SENTINEL = "ZQXJ-PLAN-RATIONALE-SENTINEL"
_ROWS = 4
_HEADER = (
    "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def _plan_text() -> str:
    rows = "\n".join(
        f"- id: C{r}\n  title: row {r}\n  change_kind: script-edit\n  surface: src/m{r}.py\n"
        f"  writes: [src/m{r}.py]\n  disposition: open\n  body: |\n    Deliver row {r}.\n"
        for r in range(1, _ROWS + 1)
    )
    padding = f"{_SENTINEL} padding prose standing in for the plan's rationale.\n" * 500
    return (
        "---\ntitle: \"Plan\"\nstatus: certified\n"
        "prime_exit_criterion:\n  statement: the probe passes\n  falsifier:\n"
        "    how: python -m pytest tests/test_probe.py\n    baseline_output: RED at base\n"
        "    expected_when_true: GREEN\n---\n\n# Plan\n\n## Goal\n\nDeliver.\n\n"
        f"{padding}\n## Tasks\n\n```yaml plan-tasks\n{rows}\n```\n"
    )


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "p1.md").write_text(_plan_text(), encoding="utf-8", newline="\n")
    lines = [
        f"| C{r} | `docs/plans/p1.md` | row {r} | `src/m{r}.py` | — | scoped pytest | S | queued |"
        for r in range(1, _ROWS + 1)
    ]
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


def test_one_plan_inventory_part_carries_the_plan_by_reference(tmp_path, capsys):
    inventory = _repo(tmp_path)
    plan_out = tmp_path / "plan.workflow.mjs"
    inv_out = tmp_path / "inv.workflow.mjs"
    root = ["--repo-root", str(tmp_path)]
    assert cli_module.main(["--plan", str(tmp_path / "docs/plans/p1.md"), "--out", str(plan_out), *root]) == 0
    capsys.readouterr()
    assert cli_module.main(["--inventory", str(inventory), "--out", str(inv_out), *root]) == 0
    script = inv_out.read_text(encoding="utf-8")

    assert "docs/plans/p1.md" in script
    assert _SENTINEL not in script
    assert len(inv_out.read_bytes()) - len(plan_out.read_bytes()) <= 20_000
    root_posix = tmp_path.as_posix()
    assert root_posix not in script.replace("\\\\", "/")
    assert not re.search(r"(?<![\w/])[A-Za-z]:[\\/](?:Users|home)", script)


def test_row_block_bytes_counts_shared_tail_paragraphs_once(tmp_path):
    inventory = _repo(tmp_path)
    chunk_rows = op_mod.parse_chunk_table(inventory.read_text(encoding="utf-8"))
    wave_rows = [op_mod._wave_row_of(r) for r in chunk_rows]
    many = [
        op_mod._wave_row_of({**chunk_rows[0], "id": f"X{i}", "footprint": f"`src/x{i}.py`"})
        for i in range(emit_module._SHARE_ROW_TAILS_MIN_ROWS + 1)
    ]
    ctx = {"plan_path": "state/mise-inventory/x.spine.md", "repo_root": tmp_path}

    few = emit_module.row_block_bytes(wave_rows, predispatch=True, **ctx)
    lots = emit_module.row_block_bytes(many, predispatch=True, **ctx)

    assert sum(few.values()) / len(few) > sum(lots.values()) / len(lots)
