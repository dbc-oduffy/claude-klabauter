"""dispatch.emit's inventory reply carries `inventory_accounting`: every Chunk-table
item emitted, skipped-with-reason or refused, counts summing to the inventory."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_mint_accounting import (
    _dogfood_inventory,
)
from coordinator_core.ops.dispatch_emit.tests.test_landed_reconcile import _PLAN, _SHA
from coordinator_core.session.record_homes import home_dir


def _emit(tmp_path, monkeypatch, inventory, **extra):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)
    return op_mod._dispatch_emit(
        {
            "inventory_path": str(inventory),
            "output_path": str(tmp_path / "run.workflow.mjs"),
            "target_root": str(tmp_path),
            **extra,
        },
        repo_root=tmp_path,
    )


def test_dogfood_shaped_reply_counts_sum_to_the_inventory(tmp_path, monkeypatch):
    inv = _dogfood_inventory(tmp_path)
    (tmp_path / ".git" / "HEAD").write_text(_SHA + "\n", encoding="utf-8")

    reply = _emit(tmp_path, monkeypatch, inv, max_rows=100)

    acc = reply["inventory_accounting"]
    counts = acc["counts"]
    assert acc["inventory_rows"] == 59
    assert counts["emitted"] + counts["skipped"] + counts["refused"] == 59
    assert counts["emitted"] == 43
    skipped = {e["id"]: e["reason"] for e in acc["skipped"]}
    assert all(f"m{i}" in skipped for i in range(51, 60))
    assert skipped["m57"] == "routed-out: plan not prepped"
    assert skipped["m58"] == "dependency m51 routed out"


def test_tranche_reply_accounts_the_deferred_items_too(tmp_path, monkeypatch):
    inv = _dogfood_inventory(tmp_path)

    reply = _emit(tmp_path, monkeypatch, inv, row_budget=20)

    acc = reply["inventory_accounting"]
    assert sum(acc["counts"].values()) == acc["inventory_rows"] == 59
    assert acc["counts"]["emitted"] == 20
    assert any(e["reason"].startswith("deferred to a later tranche") for e in acc["skipped"])


def test_part_reply_marks_the_sibling_parts_items_in_another_part(tmp_path, monkeypatch):
    inv = _dogfood_inventory(tmp_path)

    reply = _emit(tmp_path, monkeypatch, inv, inventory_part=[1, 2], max_rows=100)

    acc = reply["inventory_accounting"]
    assert sum(acc["counts"].values()) == 59
    assert any(e["reason"] == "in another part" for e in acc["skipped"])


def test_already_fixed_plan_edit_is_reported_beside_the_accounting(tmp_path, monkeypatch):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(_SHA + "\n", encoding="utf-8")
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for n in (1, 3):
        (plans / f"p{n}.md").write_text(_PLAN.format(n=n), encoding="utf-8")
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    inv = inv_dir / "run.md"
    inv.write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| P1 | `docs/plans/p1.md` | landed | `src/a1.py` | — | t | S | landed at HEAD |\n"
        "| P3 | `docs/plans/p3.md` | live | `src/a3.py` | — | t | S | queued |\n",
        encoding="utf-8",
    )

    reply = _emit(tmp_path, monkeypatch, inv)

    assert reply["landed_reconciled"] == {"docs/plans/p1.md": ["C1", "C2"]}
    acc = reply["inventory_accounting"]
    assert acc["emitted"] == ["P3"]
    assert [e["id"] for e in acc["skipped"]] == ["P1"]


def test_operator_mode_item_is_refused_with_its_reason(tmp_path, monkeypatch):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(_SHA + "\n", encoding="utf-8")
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "p3.md").write_text(_PLAN.format(n=3), encoding="utf-8")
    (plans / "ops.md").write_text(
        "---\nstatus: draft\n---\n\n# Ops\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: O1\n  title: human step\n  change_kind: code-edit\n  surface: src/o.py\n"
        "  writes: [src/o.py]\n  execution_mode: operator\n  disposition: open\n```\n",
        encoding="utf-8",
    )
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    inv = inv_dir / "run.md"
    inv.write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| O1 | `docs/plans/ops.md` | operator | `src/o.py` | — | t | S | queued |\n"
        "| P3 | `docs/plans/p3.md` | live | `src/a3.py` | — | t | S | queued |\n",
        encoding="utf-8",
    )

    reply = _emit(tmp_path, monkeypatch, inv)

    acc = reply["inventory_accounting"]
    assert acc["emitted"] == ["P3"]
    assert [e["id"] for e in acc["refused"]] == ["O1"]
    assert "operator" in acc["refused"][0]["reason"]
    assert sum(acc["counts"].values()) == acc["inventory_rows"] == 2
