"""Tests for grind_split.split_row: child minting, parent closure, both idempotency paths, bounds."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.backlog_grind_assemble import grind_split
from coordinator_core.backlog_grind_assemble.grind_split import MAX_SPLIT_ITEMS, split_row

PARENT_ID = "2026-11-01-three-failures-abc123def456"


@pytest.fixture(autouse=True)
def _no_output_override(monkeypatch):
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)


def _seed_parent(root: Path, row_id: str = PARENT_ID) -> Path:
    d = root / "state" / "bug-backlog"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{row_id}.yaml"
    path.write_text(
        "created: 2026-11-01\n"
        "title: three unrelated failures\n"
        "body: |-\n  a lists b and c\n"
        "status: open\n"
        "surface: a/test_x.py\n"
        "severity: P2\n"
        "notes: keep me\n",
        encoding="utf-8",
    )
    return path


def _items(n: int):
    return [
        {"title": f"item {i}", "evidence": f"evidence {i}", "declared_files": [f"pkg/f{i}.py"]}
        for i in range(n)
    ]


def _rows(root: Path) -> dict[str, dict]:
    return {
        p.stem: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted((root / "state" / "bug-backlog").glob("*.yaml"))
    }


def test_three_items_mint_three_children_and_close_parent(tmp_path):
    _seed_parent(tmp_path)
    ids = split_row(tmp_path, "bug", PARENT_ID, _items(3))
    rows = _rows(tmp_path)
    assert len(ids) == 3 and len(set(ids)) == 3
    for i, cid in enumerate(ids):
        child = rows[cid]
        assert child["split_from"] == PARENT_ID
        assert child["status"] == "open"
        assert child["title"] == f"item {i}"
        assert child["surface"] == f"pkg/f{i}.py"
        assert child["severity"] == "P2"
        assert "evidence {}".format(i) in child["body"]
    parent = rows[PARENT_ID]
    assert parent["split_into"] == ids
    assert parent["status"] == "closed"
    assert parent["closed_by"] == grind_split.CLOSED_BY
    assert parent["notes"] == "keep me"
    assert len(rows) == 4


def test_second_call_writes_nothing_and_returns_same_ids(tmp_path):
    _seed_parent(tmp_path)
    ids = split_row(tmp_path, "bug", PARENT_ID, _items(3))
    before = {p: p.read_bytes() for p in (tmp_path / "state" / "bug-backlog").glob("*")}
    assert split_row(tmp_path, "bug", PARENT_ID, _items(3)) == ids
    after = {p: p.read_bytes() for p in (tmp_path / "state" / "bug-backlog").glob("*")}
    assert before == after


def test_crash_before_parent_stamp_reuses_children(tmp_path, monkeypatch):
    parent = _seed_parent(tmp_path)
    original = parent.read_bytes()
    monkeypatch.setattr(
        grind_split, "_close_parent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        split_row(tmp_path, "bug", PARENT_ID, _items(3))
    assert parent.read_bytes() == original
    minted = {k for k in _rows(tmp_path) if k != PARENT_ID}
    assert len(minted) == 3
    monkeypatch.undo()
    ids = split_row(tmp_path, "bug", PARENT_ID, _items(3))
    assert set(ids) == minted
    assert len(_rows(tmp_path)) == 4
    assert _rows(tmp_path)[PARENT_ID]["split_into"] == ids


def test_crash_mid_mint_mints_only_missing_indices(tmp_path, monkeypatch):
    _seed_parent(tmp_path)
    real = grind_split._mint_child
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(grind_split, "_mint_child", flaky)
    with pytest.raises(RuntimeError):
        split_row(tmp_path, "bug", PARENT_ID, _items(3))
    monkeypatch.setattr(grind_split, "_mint_child", real)
    ids = split_row(tmp_path, "bug", PARENT_ID, _items(3))
    assert len(ids) == 3 and len(_rows(tmp_path)) == 4


@pytest.mark.parametrize("n", [0, 1, MAX_SPLIT_ITEMS + 1])
def test_item_count_out_of_bounds_raises_and_mints_nothing(tmp_path, n):
    _seed_parent(tmp_path)
    with pytest.raises(ValueError):
        split_row(tmp_path, "bug", PARENT_ID, _items(n))
    assert list(_rows(tmp_path)) == [PARENT_ID]


def test_max_items_accepted(tmp_path):
    _seed_parent(tmp_path)
    assert len(split_row(tmp_path, "bug", PARENT_ID, _items(MAX_SPLIT_ITEMS))) == MAX_SPLIT_ITEMS


def test_untitled_item_raises_before_any_write(tmp_path):
    _seed_parent(tmp_path)
    items = _items(2)
    items[1] = {"evidence": "x"}
    with pytest.raises(ValueError):
        split_row(tmp_path, "bug", PARENT_ID, items)
    assert list(_rows(tmp_path)) == [PARENT_ID]


def test_unknown_profile_and_missing_parent_raise(tmp_path):
    _seed_parent(tmp_path)
    with pytest.raises(ValueError):
        split_row(tmp_path, "nope", PARENT_ID, _items(2))
    with pytest.raises(ValueError):
        split_row(tmp_path, "bug", "absent-row", _items(2))


def test_unrelated_parent_children_are_not_reused(tmp_path):
    _seed_parent(tmp_path)
    _seed_parent(tmp_path, "2026-11-01-other-row-000000000000")
    other = split_row(tmp_path, "bug", "2026-11-01-other-row-000000000000", _items(2))
    ids = split_row(tmp_path, "bug", PARENT_ID, _items(2))
    assert not set(ids) & set(other)
