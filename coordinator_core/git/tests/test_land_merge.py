"""land_merge: a run's blob lands as the 3-way merge (run base, run blob, HEAD), never as-is."""
import hashlib
from pathlib import Path

import pytest

from coordinator_core.git import git_objects, git_state, land_merge

BASE_SHA = "b" * 40
PATH = "pkg/mod.py"
BASE = b"line A\nline B\nline C\n"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch):
    objects: dict = {}
    trees = {"base": {}, "head": {}}

    def put(data: bytes) -> str:
        sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
        objects[sha] = data
        return sha

    def spine(repo, paths, root_tree_sha=None):
        side = trees["base" if root_tree_sha == "base-tree" else "head"]
        return {"pkg": {n: (0o100644, s) for n, s in side.items()}}

    monkeypatch.setattr(git_state, "read_tree_spine", spine)
    monkeypatch.setattr(git_state, "read_commit", lambda repo, sha: git_state.CommitInfo("base-tree", (), ""))
    monkeypatch.setattr(git_objects, "read_object", lambda common, sha: ("blob", objects[sha]) if sha in objects else None)
    monkeypatch.setattr(git_objects, "write_object", lambda common, kind, data: put(data))
    monkeypatch.setattr("coordinator_core.git.git_dir.resolve_git_common_dir", lambda root: Path("."))

    def land(base: bytes, head: bytes, run: bytes) -> land_merge.Merged:
        trees["base"]["mod.py"], trees["head"]["mod.py"] = put(base), put(head)
        return land_merge.merge_into_head(Path("."), BASE_SHA, {PATH: put(run)}), objects

    return land


def test_head_gained_line_survives_the_runs_own_edit(store) -> None:
    merged, objects = store(BASE, BASE + b"line X\n", BASE.replace(b"line A", b"line A2"))
    assert not merged.conflicts
    assert objects[merged.blobs[PATH]] == b"line A2\nline B\nline C\nline X\n"


def test_same_line_changed_by_both_is_a_typed_conflict(store) -> None:
    merged, _ = store(BASE, BASE.replace(b"line A", b"head A"), BASE.replace(b"line A", b"run A"))
    assert list(merged.conflicts) == [PATH]
    assert merged.conflicts[PATH][0]["base_lines"] == "1-1"
    reply = land_merge.conflict_reply(merged.conflicts)
    assert reply["refused"] == land_merge.LAND_CONFLICT and reply["conflict_paths"] == [PATH]


def test_run_deleting_a_base_line_still_deletes_it(store) -> None:
    merged, objects = store(BASE, BASE + b"line X\n", BASE.replace(b"line B\n", b""))
    assert not merged.conflicts
    assert objects[merged.blobs[PATH]] == b"line A\nline C\nline X\n"


def test_unresolvable_base_passes_the_blob_through_and_says_so(store, monkeypatch) -> None:
    monkeypatch.setattr(git_state, "read_commit", lambda repo, sha: None)
    merged, _ = store(BASE, BASE + b"line X\n", BASE + b"run\n")
    assert merged.unbased == (PATH,) and not merged.conflicts


def test_unbased_trailer_lists_sorted_paths_and_is_none_when_every_path_was_based():
    assert land_merge.unbased_trailer(["b.py", "a.py"]) == "Land-Unbased: a.py, b.py"
    assert land_merge.unbased_trailer([]) is None


def test_run_base_sha_falls_back_to_the_halt_marker_base(tmp_path):
    run = "ask-20261121T000000-aaaaaa"
    record = tmp_path / "state" / "warp" / run
    record.mkdir(parents=True)
    base = "c" * 40
    (record / "halted.json").write_text(f'{{"status": "halted", "run_base_sha": "{base}"}}', encoding="utf-8")
    assert land_merge.run_base_sha(tmp_path, run) == base
    assert land_merge.run_base_sha(tmp_path, "ask-20261121T000000-bbbbbb") is None
