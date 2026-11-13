"""End to end through ``plan_tree_eviction(...).apply()``: eviction removes only the run's own hunks,
leaves foreign uncommitted hunks byte-identical, and a refusal is routed to the halt."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict
from coordinator_core.ops.dispatch_emit.halted_record_eviction import (
    EVICTED_NOT_IMPORTABLE, plan_tree_eviction,
)

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
    return plan_tree_eviction(
        root, REC, [PATH], {PATH}, [PATH], withheld, paths, run_id=RUN,
    )


def test_foreign_hunk_survives_eviction_and_own_hunk_goes_to_head(tmp_path, monkeypatch):
    foreign = {3: "FOREIGN three"}
    root, tree = _world(tmp_path, monkeypatch, foreign, {**foreign, 20: "OWN twenty"})
    withheld: dict = {}
    paths: list = []
    ev = _plan(root, withheld, paths)
    assert ev is not None and withheld == {PATH: EVICTED_NOT_IMPORTABLE}
    ev.apply()
    assert ev.refused == {}
    assert (root / PATH).read_bytes() == _text(foreign)
    index = json.loads((root / REC / halt_tree_evict.EVICTED_FILE).read_text(encoding="utf-8"))
    assert index[PATH]["blob"] == halt_tree_evict.blob_sha(tree)
    assert withheld == {PATH: EVICTED_NOT_IMPORTABLE}
    assert ev.evicted == [PATH]


def test_restore_round_trips_the_spliced_file(tmp_path, monkeypatch):
    foreign = {3: "FOREIGN three"}
    root, tree = _world(tmp_path, monkeypatch, foreign, {**foreign, 20: "OWN twenty"})
    ev = _plan(root, {}, [])
    ev.apply()
    result = halt_tree_evict.restore(root, root / REC, owned_paths=[PATH])
    assert result["restored"] == [PATH] and result["drift"] == {}
    assert (root / PATH).read_bytes() == tree


def test_tree_with_no_hunk_of_the_run_is_refused_named_unowned_and_bytes_unchanged(tmp_path, monkeypatch):
    # Every changed hunk predates the run's baseline: nothing is the run's to remove.
    root, tree = _world(tmp_path, monkeypatch, {5: "FOREIGN five"}, {5: "FOREIGN five"})
    withheld = {PATH: "run-dirt-uncommitted"}
    paths = ["other.txt"]
    ev = _plan(root, withheld, paths)
    assert ev is not None
    ev.apply()
    assert (root / PATH).read_bytes() == tree
    assert PATH in ev.refused and PATH in ev.unowned
    assert ev.evicted == []
    assert withheld == {PATH: "run-dirt-uncommitted"}
    assert paths == ["other.txt"]
    assert not (root / REC / halt_tree_evict.EVICTED_FILE).exists()


def test_refused_path_without_an_origin_reason_leaves_withheld(tmp_path, monkeypatch):
    root, tree = _world(tmp_path, monkeypatch, {5: "FOREIGN five"}, {5: "FOREIGN five"})
    withheld: dict = {}
    ev = _plan(root, withheld, [])
    ev.apply()
    assert PATH in ev.unowned
    assert withheld == {}
    assert (root / PATH).read_bytes() == tree
