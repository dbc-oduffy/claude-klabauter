"""
`mint_rows` omits a `plan:chunk` named in the inventory's `## Withheld / routed
out` section, and that chunk's in-plan dependents, from a plan expansion.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im


def _chunk(cid, writes, deps=()):
    row = {
        "id": cid,
        "title": f"chunk {cid}",
        "change_kind": "code-edit",
        "surface": writes[0],
        "body": f"body {cid}\n",
        "writes": writes,
    }
    if deps:
        row["depends_on"] = [{"chunk": d, "gate_kind": "epistemic-premise"} for d in deps]
    return row


PLAN_ROWS = [
    _chunk("C1", ["src/a.py"]),
    _chunk("C2", ["src/b.py"], deps=["C1"]),
    _chunk("C3", ["src/c.py"], deps=["C2"]),
    _chunk("C4", ["src/d.py"]),
]


def _mint(tmp_path: Path, withheld_section: str):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        + yaml.safe_dump(PLAN_ROWS, sort_keys=False)
        + "```\n",
        encoding="utf-8",
    )
    inv = tmp_path / "state" / "mise-inventory" / "x.md"
    inv.write_text(
        "---\nrun_id: 20261006T000000-fixture\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| P1 | `docs/plans/p.md` | s | `src/a.py` | — | v | S | pending |\n"
        + withheld_section,
        encoding="utf-8",
    )
    rows = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    accounting: dict = {}
    minted = im.mint_rows(rows, inventory_path=inv, accounting_out=accounting)
    return [r["id"] for r in minted], accounting


def test_withheld_plan_chunk_and_dependents_are_omitted(tmp_path):
    ids, _ = _mint(
        tmp_path,
        "\n## Withheld / routed out\n\n- `p:C2` — validation row, routed out.\n",
    )
    assert ids == ["P1.C1", "P1.C4"]


def test_withheld_chunk_keyed_by_row_id(tmp_path):
    ids, _ = _mint(tmp_path, "\n## Withheld / routed out\n\n- P1:C3 routed out\n")
    assert ids == ["P1.C1", "P1.C2", "P1.C4"]


def test_no_withheld_section_mints_every_chunk(tmp_path):
    ids, _ = _mint(tmp_path, "")
    assert ids == ["P1.C1", "P1.C2", "P1.C3", "P1.C4"]


def test_token_outside_the_withheld_section_is_ignored(tmp_path):
    ids, _ = _mint(
        tmp_path,
        "\n## Notes\n\n- p:C2 mentioned here\n\n## Withheld / routed out\n\n- p:C4\n\n## Later\n\n- p:C1\n",
    )
    assert ids == ["P1.C1", "P1.C2", "P1.C3"]


def test_unknown_plan_ref_withholds_nothing(tmp_path):
    ids, _ = _mint(tmp_path, "\n## Withheld / routed out\n\n- other-plan:C2\n")
    assert ids == ["P1.C1", "P1.C2", "P1.C3", "P1.C4"]


def test_every_chunk_withheld_is_accounted(tmp_path):
    ids, accounting = _mint(
        tmp_path,
        "\n## Withheld / routed out\n\n- p:C1\n- p:C4\n",
    )
    assert ids == []
    assert "P1" in accounting


def test_row_depending_on_a_withheld_chunk_is_withheld_not_minted(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    for name, rows in (("p", PLAN_ROWS), ("q", [_chunk("D1", ["src/q.py"])])):
        (tmp_path / "docs" / "plans" / f"{name}.md").write_text(
            "---\ntitle: t\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
            + yaml.safe_dump(rows, sort_keys=False)
            + "```\n",
            encoding="utf-8",
        )
    inv = tmp_path / "state" / "mise-inventory" / "x.md"
    inv.write_text(
        "---\nrun_id: 20261006T000000-fixture\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| P1 | `docs/plans/p.md` | s | `src/a.py` | — | v | S | pending |\n"
        "| P2 | `docs/plans/q.md` | s | `src/q.py` | P1.C2 | v | S | pending |\n"
        "\n## Withheld / routed out\n\n- p:C2\n",
        encoding="utf-8",
    )
    rows = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    withheld: dict = {}
    minted = im.mint_rows(rows, inventory_path=inv, withheld_out=withheld)
    ids = [r["id"] for r in minted]
    assert ids == ["P1.C1", "P1.C4"]
    assert "P2" in withheld
