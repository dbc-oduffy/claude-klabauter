"""An inventory item routed out as already landed at HEAD flips its plan's open
spine rows to `coded` (with the HEAD sha as `disposition_ref`) rather than
leaving them pending."""

from __future__ import annotations

import textwrap
from pathlib import Path

from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.session.record_homes import home_dir

_SHA = "0123456789abcdef0123456789abcdef01234567"

_PLAN = textwrap.dedent(
    """\
    ---
    status: draft
    ---

    # Plan {n}

    ## Tasks

    ```yaml plan-tasks
    - id: C1
      title: first
      change_kind: code-edit
      surface: src/a{n}.py
      writes: [src/a{n}.py]
      disposition: open
    - id: C2
      title: second
      change_kind: code-edit
      surface: src/b{n}.py
      writes: [src/b{n}.py]
      disposition: open
    - id: C3
      title: shipped earlier
      change_kind: code-edit
      surface: src/c{n}.py
      writes: [src/c{n}.py]
      disposition: coded
      disposition_ref: abc1234
    ```
    """
)


def _repo(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(_SHA + "\n", encoding="utf-8")
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for n in (1, 2, 3):
        (plans / f"p{n}.md").write_text(_PLAN.format(n=n), encoding="utf-8")
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    inventory = inv_dir / "run.md"
    inventory.write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| P1 | `docs/plans/p1.md` | whole plan landed | `src/a1.py` | — | t | S | landed at HEAD |\n"
        "| P2-C2 | `docs/plans/p2.md` | one row landed | `src/b2.py` | — | t | S | routed out — already landed at HEAD |\n"
        "| P3 | `docs/plans/p3.md` | live | `src/a3.py` | — | t | S | queued |\n",
        encoding="utf-8",
    )
    return inventory


def _emit(tmp_path, monkeypatch, inventory):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)
    return op_mod._dispatch_emit(
        {
            "inventory_path": str(inventory),
            "output_path": str(tmp_path / "run.workflow.mjs"),
            "target_root": str(tmp_path),
        },
        repo_root=tmp_path,
    )


def _dispositions(tmp_path, n):
    text = (tmp_path / "docs" / "plans" / f"p{n}.md").read_text(encoding="utf-8")
    return text


def test_landed_items_flip_their_open_rows_to_coded(tmp_path, monkeypatch):
    inventory = _repo(tmp_path)

    reply = _emit(tmp_path, monkeypatch, inventory)

    p1 = _dispositions(tmp_path, 1)
    assert p1.count("disposition: coded") == 3
    assert f"disposition_ref: {_SHA}" in p1
    assert "disposition_ref: abc1234" in p1
    p2 = _dispositions(tmp_path, 2)
    assert p2.count("disposition: coded") == 2
    assert p2.count("disposition: open") == 1
    assert reply["landed_reconciled"] == {"docs/plans/p1.md": ["C1", "C2"], "docs/plans/p2.md": ["C2"]}


def test_live_item_plan_is_untouched_and_a_second_emit_changes_nothing(tmp_path, monkeypatch):
    inventory = _repo(tmp_path)
    before = _dispositions(tmp_path, 3)

    _emit(tmp_path, monkeypatch, inventory)
    after_first = {n: _dispositions(tmp_path, n) for n in (1, 2)}
    reply = _emit(tmp_path, monkeypatch, inventory)

    assert _dispositions(tmp_path, 3) == before
    assert {n: _dispositions(tmp_path, n) for n in (1, 2)} == after_first
    assert reply["landed_reconciled"] == {}
