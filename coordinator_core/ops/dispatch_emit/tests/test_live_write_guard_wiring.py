"""halt_tree_evict.evict records the tree's bytes and never writes the tree or consults the live-write guard."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halt_tree_evict as hte
from coordinator_core.ops.dispatch_emit import live_write_guard
from pathlib import Path
import pytest

BAD = b"peer hunk\n"


def _world(tmp_path):
    root, rec = tmp_path / "root", tmp_path / "rec"
    root.mkdir()
    (root / "a.py").write_bytes(BAD)
    return root, rec


def test_evict_records_bytes_and_leaves_the_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, rec = _world(tmp_path)

    def boom(*a, **k):
        raise AssertionError("live-write guard consulted by a record-only evict")

    monkeypatch.setattr(live_write_guard, "check_live_write", boom)
    idx = hte.evict(root, rec, ["a.py"])
    sha = idx["a.py"]["blob"]
    assert idx["a.py"]["head"] == sha
    assert (rec / hte.BLOBS_DIR / sha).read_bytes() == BAD
    assert json.loads((rec / hte.EVICTED_FILE).read_text(encoding="utf-8")) == idx
    assert (root / "a.py").read_bytes() == BAD


def test_missing_tree_path_is_skipped_and_writes_no_record(tmp_path: Path) -> None:
    root, rec = _world(tmp_path)
    assert hte.evict(root, rec, ["gone.py"]) == {}
    assert not (rec / hte.EVICTED_FILE).exists()
    assert not (rec / hte.BLOBS_DIR).exists()


def test_tree_eviction_apply_keeps_the_tree(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit.halted_record_eviction import TreeEviction

    root, _ = _world(tmp_path)
    (root / "rec").mkdir()
    ev = TreeEviction(root, "rec", ["a.py"], {}, {}, [])
    ev.apply()
    assert ev.evicted == ["a.py"]
    assert (root / "rec" / hte.EVICTED_FILE).exists()
    assert (root / "a.py").read_bytes() == BAD
