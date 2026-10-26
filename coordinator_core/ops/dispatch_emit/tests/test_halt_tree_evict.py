"""halt_tree_evict: bytes leave the tree into the record, return only while the tree holds HEAD bytes."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halt_tree_evict as hte
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

BAD = b"from x import undefined_name\r\n"
HEAD = b"ok = 1\r\n"


def _setup(tmp_path):
    root = tmp_path / "root"
    rec = tmp_path / "rec"
    root.mkdir()
    (root / "tracked.py").write_bytes(BAD)
    (root / "new.py").write_bytes(BAD)
    (root / "peer.py").write_bytes(b"peer broken\n")
    return root, rec


def test_evict_stores_bytes_resets_tree_and_leaves_peer(tmp_path):
    root, rec = _setup(tmp_path)
    idx = hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None})
    assert (root / "tracked.py").read_bytes() == HEAD
    assert not (root / "new.py").exists()
    assert (root / "peer.py").read_bytes() == b"peer broken\n"
    assert idx["tracked.py"] == {"blob": blob_sha(BAD), "head": blob_sha(HEAD)}
    assert idx["new.py"]["head"] is None
    assert (rec / "withheld-blobs" / blob_sha(BAD)).read_bytes() == BAD
    assert json.loads((rec / "evicted.json").read_text()) == idx


def test_restore_round_trip_byte_identical_and_idempotent(tmp_path):
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None})
    first = hte.restore(root, rec)
    assert first == {"restored": ["new.py", "tracked.py"], "drift": {}}
    assert (root / "tracked.py").read_bytes() == BAD
    assert (root / "new.py").read_bytes() == BAD
    assert hte.restore(root, rec) == {"restored": [], "drift": {}}


def test_restore_refuses_on_drift_and_leaves_tree(tmp_path):
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None})
    (root / "tracked.py").write_bytes(b"someone else\n")
    (root / "new.py").write_bytes(b"peer made this\n")
    out = hte.restore(root, rec)
    assert out == {
        "restored": [],
        "drift": {"tracked.py": "restore-drift", "new.py": "restore-drift"},
    }
    assert (root / "tracked.py").read_bytes() == b"someone else\n"
    assert (root / "new.py").read_bytes() == b"peer made this\n"


def test_restore_without_index_is_empty(tmp_path):
    assert hte.restore(tmp_path, tmp_path / "none") == {"restored": [], "drift": {}}


def test_evict_skips_absent_path_and_unlisted_paths(tmp_path):
    root, rec = _setup(tmp_path)
    assert hte.evict(root, rec, ["gone.py"], {"gone.py": HEAD}) == {}
    assert not (rec / "evicted.json").exists()
    assert (root / "peer.py").exists()


def test_second_evict_after_reset_keeps_original_entry(tmp_path):
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py"], {"tracked.py": HEAD})
    again = hte.evict(root, rec, ["tracked.py"], {"tracked.py": HEAD})
    assert again["tracked.py"]["blob"] == blob_sha(BAD)
    assert (root / "tracked.py").read_bytes() == HEAD
    assert hte.restore(root, rec)["restored"] == ["tracked.py"]
