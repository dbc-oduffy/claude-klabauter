"""End to end through ``plan_tree_eviction(...).apply()``: a halt records a file that carries foreign
uncommitted hunks as the tree holds it, and leaves the tree byte-identical, foreign hunks included."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict
from coordinator_core.ops.dispatch_emit.halted_record_eviction import EVICTED_PARKED, plan_tree_eviction
from pathlib import Path
import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REC = "state/warp/run-x"
RUN = "r1"
PATH = "mod.py"


def _text(edits: dict, n: int = 30) -> bytes:
    return b"".join(
        (edits.get(i, f"line {i}")).encode() + b"\r\n" for i in range(1, n + 1)
    )


def _world(tmp_path, monkeypatch, baseline_edits, tree_edits):
    """HEAD is the plain file; the run's baseline carries ``baseline_edits`` (foreign dirt already
    there when the run started); the tree carries ``tree_edits``."""
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    (root / PATH).write_bytes(_text({}))
    subprocess.run(["git", "add", PATH], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "head"],
        cwd=root, check=True,
    )
    base_sha = git_objects.write_object(resolve_git_common_dir(root), b"blob", _text(baseline_edits))
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": {PATH: base_sha}}), encoding="utf-8"
    )
    tree = _text(tree_edits)
    (root / PATH).write_bytes(tree)
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {p: "x" for p in own})
    return root, tree


def _plan(root, withheld, paths):
    return plan_tree_eviction(root, REC, [PATH], {PATH}, [PATH], withheld, paths)


def test_foreign_and_own_hunks_both_stay_in_the_tree_and_the_record_holds_the_tree_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = {3: "FOREIGN three"}
    root, tree = _world(tmp_path, monkeypatch, foreign, {**foreign, 20: "OWN twenty"})
    withheld: dict = {}
    ev = _plan(root, withheld, [])
    assert ev is not None and withheld == {PATH: EVICTED_PARKED}
    ev.apply()
    assert (root / PATH).read_bytes() == tree
    index = json.loads((root / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert index[PATH] == {"blob": halt_tree_evict.blob_sha(tree), "head": halt_tree_evict.blob_sha(tree)}
    assert ev.evicted == [PATH]
    assert ev.unimportable == [PATH]


def test_restore_finds_the_tree_already_holding_the_recorded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = {3: "FOREIGN three"}
    root, tree = _world(tmp_path, monkeypatch, foreign, {**foreign, 20: "OWN twenty"})
    ev = _plan(root, {}, [])
    ev.apply()
    result = halt_tree_evict.restore(root, root / REC, owned_paths=[PATH])
    assert result == {"restored": [], "drift": {}, "refused": {}}
    assert (root / PATH).read_bytes() == tree


def test_a_tree_with_only_foreign_hunks_is_still_recorded_and_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree = _world(tmp_path, monkeypatch, {5: "FOREIGN five"}, {5: "FOREIGN five"})
    withheld = {PATH: "run-dirt-uncommitted"}
    paths = ["other.txt"]
    ev = _plan(root, withheld, paths)
    assert ev is not None
    ev.apply()
    assert (root / PATH).read_bytes() == tree
    assert ev.evicted == [PATH]
    assert withheld == {PATH: EVICTED_PARKED}
    assert (root / REC / halt_tree_evict.EVICTED_FILE).is_file()
