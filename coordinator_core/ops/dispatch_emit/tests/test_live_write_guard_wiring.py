"""halt_tree_evict.evict calls live_write_guard.check_live_write and aborts the reset on refusal."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import halt_tree_evict as hte
from coordinator_core.ops.dispatch_emit import live_write_guard

BAD = b"peer hunk\n"
HEAD = b"ok = 1\n"


def _world(tmp_path):
    root, rec = tmp_path / "root", tmp_path / "rec"
    root.mkdir()
    (root / "a.py").write_bytes(BAD)
    return root, rec


def test_refusal_leaves_tree_and_record_untouched_and_names_holder(tmp_path, monkeypatch):
    root, rec = _world(tmp_path)
    seen = []

    def refuse(r, run_id, path, new_bytes):
        seen.append((run_id, path, new_bytes))
        return "write to a.py drops 1 unlanded hunk(s) held by peer-run."

    monkeypatch.setattr(live_write_guard, "check_live_write", refuse)
    refused: dict = {}
    idx = hte.evict(root, rec, ["a.py"], {"a.py": HEAD}, run_id="me", refused=refused)
    assert seen == [("me", "a.py", HEAD)]
    assert "peer-run" in refused["a.py"]
    assert idx == {}
    assert (root / "a.py").read_bytes() == BAD
    assert not (rec / "evicted.json").exists()
    assert not (rec / "withheld-blobs").exists()


def test_no_refusal_still_evicts(tmp_path, monkeypatch):
    root, rec = _world(tmp_path)
    monkeypatch.setattr(live_write_guard, "check_live_write", lambda *a: None)
    refused: dict = {}
    idx = hte.evict(root, rec, ["a.py"], {"a.py": HEAD}, run_id="me", refused=refused)
    assert "a.py" in idx and refused == {}
    assert (root / "a.py").read_bytes() == HEAD


def test_untracked_path_is_checked_as_removal(tmp_path, monkeypatch):
    root, rec = _world(tmp_path)
    seen = []
    monkeypatch.setattr(live_write_guard, "check_live_write", lambda r, i, p, b: seen.append(b))
    hte.evict(root, rec, ["a.py"], {"a.py": None}, run_id="me")
    assert seen == [b""]
    assert not (root / "a.py").exists()


def test_without_run_id_guard_is_not_consulted(tmp_path, monkeypatch):
    root, rec = _world(tmp_path)

    def boom(*a):
        raise AssertionError("guard consulted without run_id")

    monkeypatch.setattr(live_write_guard, "check_live_write", boom)
    hte.evict(root, rec, ["a.py"], {"a.py": HEAD})
    assert (root / "a.py").read_bytes() == HEAD


def test_tree_eviction_apply_surfaces_refusal_as_unowned(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.halted_record_eviction import TreeEviction

    root, _ = _world(tmp_path)
    (root / "rec").mkdir()
    monkeypatch.setattr(live_write_guard, "check_live_write", lambda *a: "held by peer-run")
    ev = TreeEviction(root, "rec", {"a.py": HEAD}, {}, {}, "me")
    ev.apply()
    assert ev.evicted == []
    assert ev.unowned == {"a.py": "held by peer-run"}
    assert (root / "a.py").read_bytes() == BAD
