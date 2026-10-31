"""landing_halted_view: the halted-hold pass reads each shared fact once per landing and answers like the readers it replaced."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_index, git_state
from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import landing_halted_view as view
from coordinator_core.ops.dispatch_emit import sibling_claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/s.yaml"
PATHS = ("a.py", "b.py", "c.py")
BASE_TEXT = "def f():\n    return 1\n"
KEPT_TEXT = BASE_TEXT + "\n\ndef g(a, b):\n    return a + b\n"


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _repo(root: Path) -> None:
    _git(root, "init", "-q")
    for p in PATHS:
        _write(root / p, BASE_TEXT)
    _write(root / ".gitignore", "ignored.txt\n")
    _git(root, "add", *PATHS, ".gitignore")
    _git(root, "commit", "-q", "-m", "base")


def _halted_runs(root: Path) -> tuple:
    """Three halted runs, each withholding one path whose kept blob is not at HEAD and is dirty in the tree."""
    base = _git(root, "rev-parse", "HEAD")
    kept_blob = _git(root, "hash-object", "-w", "--stdin", stdin=KEPT_TEXT)
    _write(root / SIZING, "status: open\n")
    halted = []
    for i, p in enumerate(PATHS):
        run_id = _rid(300 + i, f"{i}" * 6)
        rec = root / "state/warp" / run_id
        _write(rec / "run.json", json.dumps({"sizing_rel": SIZING}))
        _write(rec / "manifest.json", json.dumps({"rows": [{"writes": [p]}]}))
        _write(rec / "halted.json", json.dumps({
            "status": "halted", "sizing_rel": SIZING, "run_base_sha": base,
            "run_artifacts": {"withheld": {p: "halt-record-only"}},
        }))
        _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {p: kept_blob}}}, "plan_gate": None}))
        _write(root / p, KEPT_TEXT)
        halted.append(run_id)
    return halted, _rid(10, "ffffff")


def test_shared_computes_once_inside_a_scope_and_every_time_outside() -> None:
    calls: list = []
    assert [view.shared("t", "k", lambda: calls.append(1) or len(calls)) for _ in range(2)] == [1, 2]
    calls.clear()
    with hr.release_scope():
        assert [view.shared("t", "k", lambda: calls.append(1) or len(calls)) for _ in range(3)] == [1, 1, 1]
    assert len(calls) == 1
    with hr.release_scope():
        view.shared("t", "k", lambda: calls.append(1))
    assert len(calls) == 2  # a new scope starts empty


def test_index_walk_is_shared_and_widened_only_by_a_new_path(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    walks: list = []
    real = git_index.parse_index_identity
    monkeypatch.setattr(
        git_index, "parse_index_identity", lambda *a, **k: (walks.append(set(k.get("wanted") or ())), real(*a, **k))[1],
    )
    with hr.release_scope():
        view.hint_index(tmp_path, {"b.py", "c.py"})
        first = view.index_identity(tmp_path, ["a.py"])
        again = view.index_identity(tmp_path, ["b.py"])
        assert len(walks) == 1 and walks[0] == {"a.py", "b.py", "c.py"}
        assert set(first) == {"a.py"} and set(again) == {"b.py"}
        absent = view.index_identity(tmp_path, ["a.py", "nope.py"])  # outside the walked set: one wider walk
        assert set(absent) == {"a.py"} and len(walks) == 2
        view.index_identity(tmp_path, ["nope.py", "b.py"])
        assert len(walks) == 2
    assert view.index_identity(tmp_path, ["a.py"]) == real(tmp_path, wanted={"a.py"})


def test_index_walk_repeats_after_the_index_file_changes(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    walks: list = []
    real = git_index.parse_index_identity
    monkeypatch.setattr(git_index, "parse_index_identity", lambda *a, **k: (walks.append(1), real(*a, **k))[1])
    with hr.release_scope():
        view.index_identity(tmp_path, ["a.py"])
        view.index_identity(tmp_path, ["a.py"])
        assert len(walks) == 1
        _write(tmp_path / "d.py", BASE_TEXT)
        _git(tmp_path, "add", "d.py")
        assert "d.py" in view.index_identity(tmp_path, ["d.py"])
        assert len(walks) == 2


def test_dirty_withheld_decides_each_path_once_per_index_state(tmp_path, monkeypatch) -> None:
    from coordinator_core.ops.ceremony import commit_companion

    _repo(tmp_path)
    _write(tmp_path / "a.py", KEPT_TEXT)
    seen: list = []
    real = commit_companion.own_left_behind
    monkeypatch.setattr(
        commit_companion, "own_left_behind",
        lambda root, held, sid, spec, *a: (seen.append(tuple(held[sid])), real(root, held, sid, spec, *a))[1],
    )
    with hr.release_scope():
        assert view.dirty_withheld(tmp_path, ["a.py", "b.py"]) == {"a.py"}
        assert view.dirty_withheld(tmp_path, ["b.py", "a.py"]) == {"a.py"}
        assert view.dirty_withheld(tmp_path, ["c.py", "a.py"]) == {"a.py"}
    assert seen == [("a.py", "b.py"), ("c.py",)]


def _porcelain(root: Path, paths) -> set:
    out = _git(root, "-c", "core.quotepath=off", "--literal-pathspecs", "status", "--porcelain", "--untracked-files=all", "--", *paths)
    return {line.split(None, 1)[1] for line in out.splitlines()}


def test_dirty_status_matches_git_status_over_each_kind_of_dirt(tmp_path) -> None:
    _repo(tmp_path)
    _write(tmp_path / "a.py", KEPT_TEXT)  # modified in the worktree
    _write(tmp_path / "staged.py", BASE_TEXT)
    _git(tmp_path, "add", "staged.py")  # staged add
    _write(tmp_path / "b.py", KEPT_TEXT)
    _git(tmp_path, "add", "b.py")  # staged modify
    os.remove(tmp_path / "c.py")  # deleted in the worktree
    _write(tmp_path / "new.py", BASE_TEXT)  # untracked
    _write(tmp_path / "ignored.txt", "x\n")  # untracked and ignored
    _write(tmp_path / ".gitignore", "ignored.txt\n")
    paths = ["a.py", "b.py", "c.py", "staged.py", "new.py", "ignored.txt", "missing.py", ".gitignore"]
    with hr.release_scope():
        decided = view.dirty_status(tmp_path, paths)
    assert decided == _porcelain(tmp_path, paths) == {"a.py", "b.py", "c.py", "staged.py", "new.py"}


def test_dirty_status_of_a_clean_tree_is_empty_and_a_directory_is_left_to_git(tmp_path) -> None:
    _repo(tmp_path)
    assert view.dirty_status(tmp_path, list(PATHS)) == set() == _porcelain(tmp_path, PATHS)
    (tmp_path / "sub").mkdir()
    assert view.dirty_status(tmp_path, ["sub"]) is None


def test_dirty_reader_spawns_no_git_when_the_view_decides_and_asks_git_when_it_cannot(tmp_path, monkeypatch) -> None:
    from coordinator_core.git import run as git_run

    _repo(tmp_path)
    _write(tmp_path / "a.py", KEPT_TEXT)
    spawned: list = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda args, *a, **k: (spawned.append(list(args)), real(args, *a, **k))[1])
    assert sibling_claims._dirty_reader(tmp_path, {"a.py", "b.py"})() == {"a.py"}
    assert spawned == []
    (tmp_path / "sub").mkdir()
    sibling_claims._dirty_reader(tmp_path, {"sub"})()
    assert any("status" in a for a in spawned)


def test_tree_entries_reads_each_directory_once_across_path_sets(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    _write(tmp_path / "d/e/f.py", BASE_TEXT)
    _git(tmp_path, "add", "d")
    _git(tmp_path, "commit", "-q", "-m", "more")
    head = git_state.head_sha(tmp_path)
    view._SPINES.clear()
    loaded: list = []
    real = git_state.read_tree_spine
    monkeypatch.setattr(git_state, "read_tree_spine", lambda repo, paths, **k: (loaded.append(list(paths)), real(repo, paths, **k))[1])
    first = view.tree_entries(tmp_path, head, ["a.py", "d/e/f.py"])
    again = view.tree_entries(tmp_path, head, ["a.py", "d/e/f.py", "d/e/new.py", "gone/q.py"])
    assert loaded == [["a.py", "d/e/f.py"]]
    assert set(first) == {"a.py", "d/e/f.py"} and set(again) == set(first)
    assert first == git_state.head_blobs(tmp_path, ["a.py", "d/e/f.py"])


def test_committer_variants_share_one_weighing_of_the_runs_own_rules(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    halted, committing = _halted_runs(tmp_path)
    other = _rid(20, "eeeeee")
    _write(tmp_path / "state/warp" / other / "run.json", json.dumps({"sizing_rel": SIZING}))
    heads: list = []
    real = hr._release_head
    monkeypatch.setattr(hr, "_release_head", lambda root, run_id: (heads.append(run_id), real(root, run_id))[1])
    run = halted[0]
    with hr.release_scope():
        assert hr.release_reason(tmp_path, run, committing, None) is None
        assert hr.release_reason(tmp_path, run, None, None) is None
        # the committer-dependent rules still apply on top of the shared head: the same sizing frees the run
        assert hr.release_reason(tmp_path, run, other, None) == hr.RELEASE_SUCCESSOR
    assert heads == [run]


def test_sibling_claim_pass_over_halted_runs_walks_the_index_once_and_spawns_no_git(tmp_path, monkeypatch) -> None:
    from coordinator_core.git import run as git_run

    _repo(tmp_path)
    halted, committing = _halted_runs(tmp_path)
    walks: list = []
    real = git_index.parse_index_identity
    monkeypatch.setattr(git_index, "parse_index_identity", lambda *a, **k: (walks.append(1), real(*a, **k))[1])
    spawned: list = []
    real_git = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda args, *a, **k: (spawned.append(list(args)), real_git(args, *a, **k))[1])

    claims = sibling_claims.sibling_run_claims(tmp_path, committing, PATHS)

    assert sorted(claims) == sorted(PATHS) and set(claims.values()) == set(halted)
    assert spawned == []
    assert len(walks) <= 1
