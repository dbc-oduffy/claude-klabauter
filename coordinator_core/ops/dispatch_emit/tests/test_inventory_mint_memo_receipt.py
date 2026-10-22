"""`mint_rows` carries a plan row's declared memo keys onto the minted spine row on both mint paths."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.spine_read import MemoRowNoReceiptError, read_spine

RECEIPT = ".coordinator-local/memo-outbox/sent/some-topic.md"
MEMO_KEYS = ("receipt", "memo_to", "memo_kind", "kind", "performer", "disposition_ref")


def _memo_chunk(cid: str, **extra) -> dict:
    row = {
        "id": cid,
        "title": f"memo {cid}",
        "change_kind": "code-edit",
        "surface": "src/x.py",
        "body": f"Spec: docs/plans/p.md ({cid})\nbody\n",
        "writes": ["src/x.py"],
        "kind": "memo-send",
        "memo_to": "peer-repo",
    }
    row.update(extra)
    return row


def _plain_chunk(cid: str) -> dict:
    return {
        "id": cid,
        "title": f"plain {cid}",
        "change_kind": "code-edit",
        "surface": "src/y.py",
        "body": f"Spec: docs/plans/p.md ({cid})\nbody\n",
        "writes": ["src/y.py"],
    }


def _setup(tmp_path: Path, plan_rows: list, table_rows: list) -> Path:
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        + yaml.safe_dump(plan_rows, sort_keys=False)
        + "```\n",
        encoding="utf-8",
    )
    head = (
        "---\nrun_id: 20261022T000000-fixture\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    inv = tmp_path / "state" / "mise-inventory" / "x.md"
    inv.write_text(
        head
        + "".join(
            f"| {i} | `docs/plans/p.md` | s {i} | {fp} | — | v | S | pending |\n" for i, fp in table_rows
        ),
        encoding="utf-8",
    )
    return inv


def _mint(inv: Path) -> dict:
    rows = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        minted = im.mint_rows(rows, inventory_path=inv)
    return {r["id"]: r for r in minted}


def _read_minted_spine(inv: Path, tmp_path: Path):
    text, _ = im.mint_spine(str(inv))
    spine = tmp_path / "minted-spine.md"
    spine.write_text(text, encoding="utf-8")
    return read_spine(spine)


def test_single_row_path_carries_every_declared_memo_key(tmp_path):
    plan = [
        _memo_chunk(
            "C5a",
            receipt=RECEIPT,
            memo_kind="notice",
            performer="em",
            disposition_ref="abc1234",
        )
    ]
    inv = _setup(tmp_path, plan, [("I02-C5a", "`src/x.py`")])
    entry = _mint(inv)["I02-C5a"]
    assert entry["receipt"] == RECEIPT
    for key in MEMO_KEYS:
        assert key in entry


def test_sub_row_expansion_path_carries_receipt(tmp_path):
    plan = [_memo_chunk("C5a", receipt=RECEIPT)]
    inv = _setup(tmp_path, plan, [("I02", "`src/x.py`")])
    by_id = _mint(inv)
    assert by_id["I02.C5a"]["receipt"] == RECEIPT
    assert by_id["I02.C5a"]["memo_to"] == "peer-repo"
    assert by_id["I02.C5a"]["kind"] == "memo-send"


def test_minted_memo_send_row_declaring_receipt_is_not_refused(tmp_path):
    plan = [_memo_chunk("C5a", receipt=RECEIPT)]
    inv = _setup(tmp_path, plan, [("I02", "`src/x.py`")])
    rows = _read_minted_spine(inv, tmp_path)
    assert [r.id for r in rows] == ["I02.C5a"]


def test_memo_send_row_with_neither_receipt_nor_topic_still_refuses(tmp_path):
    plan = [_memo_chunk("C5a")]
    inv = _setup(tmp_path, plan, [("I02-C5a", "`src/x.py`")])
    with pytest.raises(MemoRowNoReceiptError):
        _read_minted_spine(inv, tmp_path)


@pytest.mark.parametrize("item_id", ["I02", "I02-C1"])
def test_row_without_memo_keys_gains_none(tmp_path, item_id):
    inv = _setup(tmp_path, [_plain_chunk("C1")], [(item_id, "`src/y.py`")])
    for entry in _mint(inv).values():
        assert not any(key in entry for key in MEMO_KEYS)
