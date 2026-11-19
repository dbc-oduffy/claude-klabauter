"""withheld-prints.json names the run's parked blob (present in withheld-blobs/) for an evicted path,
never the HEAD bytes the tree was reset to; an unparked path is printed from the tree."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halt_tree_evict, land_run
from coordinator_core.ops.dispatch_emit.halted_record_ledger import _record_withheld_prints
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

REL = "state/warp/run-1"
HEAD_BYTES = b"x = 1\n"
RUN_BYTES = b"x = 2\n"


def _park(root, path, blob_bytes, *, persist=True):
    rec = root / REL
    (rec / halt_tree_evict.BLOBS_DIR).mkdir(parents=True, exist_ok=True)
    sha = blob_sha(blob_bytes)
    if persist:
        (rec / halt_tree_evict.BLOBS_DIR / sha).write_bytes(blob_bytes)
    (rec / halt_tree_evict.EVICTED_FILE).write_text(
        json.dumps({path: {"blob": sha, "head": blob_sha(HEAD_BYTES)}}), encoding="utf-8"
    )
    return sha


def _prints(root):
    return json.loads((root / REL / WITHHELD_PRINTS_FILE).read_text(encoding="utf-8"))


def test_evicted_path_prints_the_parked_run_blob_not_head(tmp_path):
    (tmp_path / "a.py").write_bytes(HEAD_BYTES)  # the tree after eviction holds HEAD
    sha = _park(tmp_path, "a.py", RUN_BYTES)
    paths: list = []

    _record_withheld_prints(tmp_path, REL, {"a.py": "evicted-parked"}, paths)

    got = _prints(tmp_path)
    assert got == {"a.py": sha} and got["a.py"] != blob_sha(HEAD_BYTES)
    assert (tmp_path / REL / halt_tree_evict.BLOBS_DIR / got["a.py"]).is_file()
    assert paths == [f"{REL}/{WITHHELD_PRINTS_FILE}"]


def test_land_run_stamp_after_eviction_prints_the_parked_blob(tmp_path):
    (tmp_path / "a.py").write_bytes(HEAD_BYTES)
    sha = _park(tmp_path, "a.py", RUN_BYTES)

    land_run._stamp_withheld_prints(
        tmp_path, {"committed": True, "withheld": {"a.py": "evicted-parked"}, "record_dir": REL}
    )

    assert _prints(tmp_path) == {"a.py": sha}


def test_unparked_path_prints_the_tree_bytes(tmp_path):
    (tmp_path / REL).mkdir(parents=True)
    (tmp_path / "b.py").write_bytes(RUN_BYTES)

    _record_withheld_prints(tmp_path, REL, {"b.py": "run-dirt-uncommitted"}, [])

    assert _prints(tmp_path) == {"b.py": blob_sha(RUN_BYTES)}


def test_parked_entry_without_its_blob_falls_back_to_the_tree(tmp_path):
    (tmp_path / "a.py").write_bytes(HEAD_BYTES)
    _park(tmp_path, "a.py", RUN_BYTES, persist=False)

    _record_withheld_prints(tmp_path, REL, {"a.py": "evicted-parked"}, [])

    assert _prints(tmp_path) == {"a.py": blob_sha(HEAD_BYTES)}
