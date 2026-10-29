import threading
import time

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import live_run_index as idx
from coordinator_core.ops.dispatch_emit import live_run_writes


def _root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_add_stage_remove_read(tmp_path):
    root = _root(tmp_path)
    assert idx.add(root, "r1", 1.0)
    assert idx.read(root, 1000) == {"r1": None}
    idx.stage(root, "r1", ["a/b.py", "c.py", "c.py"])
    idx.add(root, "r2", 2.0)
    got = idx.read(root, 1000)
    assert got == {"r1": frozenset({"a/b.py", "c.py"}), "r2": None}
    assert idx.read(root, 1000, exclude_run_id="r1") == {"r2": None}
    idx.remove(root, "r1")
    assert idx.read(root, 1000) == {"r2": None}


def test_add_keeps_existing_footprint(tmp_path):
    root = _root(tmp_path)
    idx.stage(root, "r1", ["x.py"], minted_at=1.0)
    idx.add(root, "r1", 9.0)
    assert idx.read(root, 1000) == {"r1": frozenset({"x.py"})}


def test_missing_without_common_dir_or_file(tmp_path):
    assert idx.read(tmp_path, 1000) is hrr.MISSING
    assert idx.add(tmp_path, "r", 1.0) is False
    assert idx.read(_root(tmp_path), 1000) is hrr.MISSING


def test_overlap_equals_live_run_writes(tmp_path):
    root = _root(tmp_path)
    fps = {"r1": ["src/a.py", "docs"], "r2": ["other/z.py"], "r3": ["src"]}
    for rid, fp in fps.items():
        idx.stage(root, rid, fp, minted_at=1.0)
    idx.add(root, "r4", 1.0)
    index = idx.read(root, 1000)
    direct = {k: (frozenset(v) if v else None) for k, v in fps.items()}
    direct["r4"] = None
    for writes in (["src/a.py"], ["src/q.py"], ["docs/x.md"], ["nothing"], ["src"], []):
        assert idx.overlaps(writes, index) == live_run_writes.overlaps(writes, direct)
    assert idx.overlaps(["src/a.py"], index) == {"r1": ["src/a.py"], "r3": ["src"]}


def test_stalled_index_read(tmp_path, monkeypatch):
    root = _root(tmp_path)
    idx.add(root, "r1", 1.0)
    gate = threading.Event()
    monkeypatch.setattr(hrr, "_read_file", lambda p: (gate.wait(5), b"")[1])
    t0 = time.perf_counter()
    assert idx.read(root, 30) is hrr.STALLED
    assert time.perf_counter() - t0 < 1
    gate.set()
    time.sleep(0.1)
