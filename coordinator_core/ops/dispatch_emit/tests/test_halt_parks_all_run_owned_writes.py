"""``plan_tree_eviction`` parks every run-owned unlanded path (importable ``.py`` and non-``.py``) in the
halted record, leaves peer-owned and record-dir paths out, and never writes the working tree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halt_tree_evict
from coordinator_core.ops.dispatch_emit.halted_record_eviction import EVICTED_PARKED, plan_tree_eviction

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
    return plan_tree_eviction(root, REC, written, set(written) | {PEER}, list(written), withheld, paths)


def test_importable_py_and_md_are_both_parked_and_the_tree_is_untouched(tmp_path: Path) -> None:
    root = _world(tmp_path)
    tree = {p: (root / p).read_bytes() for p in (GOOD_PY, NOTE, PEER)}
    withheld = {NOTE: "halt-record-only", PEER: "peer-held"}
    paths: list = []
    ev = _plan(root, withheld, paths, [GOOD_PY, NOTE, PEER])
    assert ev is not None
    assert withheld[GOOD_PY] == EVICTED_PARKED and withheld[NOTE] == EVICTED_PARKED
    assert withheld[PEER] == "peer-held"
    ev.apply()
    assert ev.evicted == sorted([GOOD_PY, NOTE])
    assert {p: (root / p).read_bytes() for p in (GOOD_PY, NOTE, PEER)} == tree
    index = json.loads((root / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert set(index) == {GOOD_PY, NOTE}
    for rel, entry in index.items():
        assert entry["head"] == entry["blob"]
        assert (root / REC / halt_tree_evict.BLOBS_DIR / entry["blob"]).read_bytes() == tree[rel]
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths


def _tree_bytes(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts and REC not in p.relative_to(root).as_posix()
    }


def test_default_halt_path_leaves_every_uncommitted_path_byte_identical_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The halt entry as `land_run_halt` calls it: plan, then apply. A tracked edit and an untracked new
    file survive byte-identical; no worktree file is written or unlinked; both sit in `evicted.json`."""
    root = _world(tmp_path)
    before = _tree_bytes(root)
    record = root / REC

    real_write = Path.write_bytes

    def _guarded_write(self: Path, *a, **k):
        if not str(self).startswith(str(record)):
            pytest.fail(f"halt wrote the worktree: {self}")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_bytes", _guarded_write)
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: pytest.fail(f"halt unlinked: {self}"))
    withheld: dict = {NOTE: "halt-record-only"}
    paths: list = []
    ev = _plan(root, withheld, paths, [GOOD_PY, NOTE])
    assert ev is not None
    ev.apply()
    monkeypatch.undo()

    assert _tree_bytes(root) == before
    index = json.loads((root / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert set(index) == {GOOD_PY, NOTE}
    for rel in (GOOD_PY, NOTE):
        assert (root / REC / halt_tree_evict.BLOBS_DIR / index[rel]["blob"]).read_bytes() == before[rel]
    assert f"{REC}/{halt_tree_evict.EVICTED_FILE}" in paths


def test_unimportable_py_is_parked_under_the_one_reason_and_listed_as_a_diagnostic(tmp_path: Path) -> None:
    root = _world(tmp_path)
    withheld: dict = {}
    ev = _plan(root, withheld, [], [GOOD_PY, BAD_PY, NOTE])
    assert ev is not None
    assert {withheld[p] for p in (BAD_PY, GOOD_PY, NOTE)} == {EVICTED_PARKED}
    assert ev.unimportable == [BAD_PY]
    ev.apply()
    assert (root / BAD_PY).read_bytes() == b"def broken(:\n"


def test_landed_unchanged_and_record_dir_paths_are_not_parked(tmp_path: Path) -> None:
    root = _world(tmp_path)
    (root / REC / "halted.json").write_text("{}", encoding="utf-8")
    withheld: dict = {}
    written = [GOOD_PY, NOTE, f"{REC}/halted.json"]
    ev = plan_tree_eviction(root, REC, written, {GOOD_PY, f"{REC}/halted.json"}, written, withheld, [NOTE])
    assert ev is not None
    assert set(withheld) == {GOOD_PY}


def test_refire_finds_every_parked_path_already_in_the_tree(tmp_path: Path) -> None:
    root = _world(tmp_path)
    tree = {p: (root / p).read_bytes() for p in (GOOD_PY, NOTE)}
    ev = _plan(root, {}, [], [GOOD_PY, NOTE])
    ev.apply()
    result = halt_tree_evict.restore(root, root / REC, owned_paths=[GOOD_PY, NOTE])
    assert result == {"restored": [], "drift": {}, "refused": {}}
    assert {p: (root / p).read_bytes() for p in (GOOD_PY, NOTE)} == tree


def test_nothing_run_owned_returns_none(tmp_path: Path) -> None:
    root = _world(tmp_path)
    assert _plan(root, {PEER: "peer-held"}, [], [PEER]) is None
