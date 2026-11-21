"""``plan_tree_eviction`` parks every run-owned unlanded path (importable ``.py`` and non-``.py``),
leaves peer-owned and record-dir paths alone, and a refire restores the parked bytes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halt_tree_evict
from coordinator_core.ops.dispatch_emit.halted_record_eviction import (
    EVICTED_NOT_IMPORTABLE, EVICTED_PARKED, plan_tree_eviction,
)

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REC = "state/warp/run-x"
RUN = "r1"
GOOD_PY = "pkg_ok.py"
NOTE = "notes.md"
PEER = "peer.txt"
BAD_PY = "pkg_bad.py"


def _world(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    (root / NOTE).write_bytes(b"head note\n")
    (root / PEER).write_bytes(b"head peer\n")
    subprocess.run(["git", "add", NOTE, PEER], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "head"], cwd=root, check=True
    )
    common = resolve_git_common_dir(root)
    baseline = {
        NOTE: git_objects.write_object(common, b"blob", b"head note\n"),
        GOOD_PY: None,
        BAD_PY: None,
    }
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": baseline}), encoding="utf-8"
    )
    (root / GOOD_PY).write_bytes(b"VALUE = 1\n")
    (root / BAD_PY).write_bytes(b"def broken(:\n")
    (root / NOTE).write_bytes(b"head note\nrun line\n")
    (root / PEER).write_bytes(b"head peer\npeer line\n")
    return root


def _plan(root: Path, withheld: dict, paths: list, written: list):
    dirty = set(written) | {PEER}
    return plan_tree_eviction(
        root, REC, written, dirty, list(written), withheld, paths, run_id=RUN, python_dirt=dirty,
    )


def test_importable_py_and_md_are_both_parked_and_peer_file_untouched(tmp_path: Path) -> None:
    root = _world(tmp_path)
    peer_bytes = (root / PEER).read_bytes()
    withheld = {NOTE: "halt-record-only", PEER: "peer-held"}
    paths: list = []
    ev = _plan(root, withheld, paths, [GOOD_PY, NOTE, PEER])
    assert ev is not None
    assert withheld[GOOD_PY] == EVICTED_PARKED and withheld[NOTE] == EVICTED_PARKED
    assert withheld[PEER] == "peer-held"
    ev.apply()
    assert ev.refused == {}
    assert ev.evicted == sorted([GOOD_PY, NOTE])
    assert not (root / GOOD_PY).exists()
    assert (root / NOTE).read_bytes() == b"head note\n"
    assert (root / PEER).read_bytes() == peer_bytes
    index = json.loads((root / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert set(index) == {GOOD_PY, NOTE}
    for entry in index.values():
        assert (root / REC / halt_tree_evict.BLOBS_DIR / entry["blob"]).is_file()
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths


def test_unimportable_py_keeps_its_own_reason_beside_the_parked_rest(tmp_path: Path) -> None:
    root = _world(tmp_path)
    withheld: dict = {}
    ev = _plan(root, withheld, [], [GOOD_PY, BAD_PY, NOTE])
    assert ev is not None
    assert withheld[BAD_PY] == EVICTED_NOT_IMPORTABLE
    assert withheld[GOOD_PY] == EVICTED_PARKED and withheld[NOTE] == EVICTED_PARKED


def test_landed_unchanged_and_record_dir_paths_are_not_parked(tmp_path: Path) -> None:
    root = _world(tmp_path)
    (root / REC / "halted.json").write_text("{}", encoding="utf-8")
    withheld: dict = {}
    written = [GOOD_PY, NOTE, f"{REC}/halted.json"]
    ev = plan_tree_eviction(
        root, REC, written, {GOOD_PY, f"{REC}/halted.json"}, written, withheld, [NOTE], run_id=RUN,
        python_dirt={GOOD_PY},
    )
    assert ev is not None
    assert set(withheld) == {GOOD_PY}


def test_refire_restores_every_parked_path(tmp_path: Path) -> None:
    root = _world(tmp_path)
    tree = {p: (root / p).read_bytes() for p in (GOOD_PY, NOTE)}
    ev = _plan(root, {}, [], [GOOD_PY, NOTE])
    ev.apply()
    result = halt_tree_evict.restore(root, root / REC, owned_paths=[GOOD_PY, NOTE])
    assert sorted(result["restored"]) == sorted([GOOD_PY, NOTE]) and result["drift"] == {}
    assert {p: (root / p).read_bytes() for p in (GOOD_PY, NOTE)} == tree


def test_nothing_run_owned_returns_none(tmp_path: Path) -> None:
    root = _world(tmp_path)
    assert _plan(root, {PEER: "peer-held"}, [], [PEER]) is None
