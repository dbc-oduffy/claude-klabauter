"""
`mint_rows` holds a chunk whose id merges members (`a.C3-b.C4`) when any member
is listed in the inventory's `## Withheld / routed out` section, and accepts a
plan ref written as the spec path.
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


def _mint(tmp_path: Path, rows, withheld: str):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
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
        "\n## Withheld / routed out\n\n" + withheld,
        encoding="utf-8",
    )
    parsed = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    minted = im.mint_rows(parsed, inventory_path=inv)
    return [r["id"] for r in minted]


MERGED_ROWS = [
    _chunk("C1", ["src/a.py"]),
    _chunk("C3-C4", ["src/c.py"]),
    _chunk("C5", ["src/e.py"], deps=["C3-C4"]),
    _chunk("C34", ["src/f.py"]),
]


def test_merged_chunk_held_when_a_member_is_withheld(tmp_path):
    ids = _mint(tmp_path, MERGED_ROWS, "- `p:C4`\n")
    assert ids == ["P1.C1", "P1.C34"]


def test_merged_chunk_held_when_first_member_is_withheld(tmp_path):
    ids = _mint(tmp_path, MERGED_ROWS, "- `p:C3`\n")
    assert ids == ["P1.C1", "P1.C34"]


def test_unlisted_members_hold_nothing(tmp_path):
    ids = _mint(tmp_path, MERGED_ROWS, "- `p:C9`\n")
    assert ids == ["P1.C1", "P1.C3-C4", "P1.C5", "P1.C34"]


def test_spec_path_plan_ref_holds(tmp_path):
    ids = _mint(tmp_path, MERGED_ROWS, "docs/plans/p.md:C4\n")
    assert ids == ["P1.C1", "P1.C34"]
