"""The push ladder's merge declines upstream `control/plugin/` changes while the compile hold is set."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import push as push_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo_behind_a_peer_push(
    tmp_path: Path, peer_path: str, *, seeded: bool = False, peer_deletes: bool = False
) -> Path:
    """`seeded` commits `peer_path` in the seed; `peer_deletes` has the peer remove it instead of writing it."""
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    if seeded:
        (repo / peer_path).parent.mkdir(parents=True, exist_ok=True)
        (repo / peer_path).write_text("seed", encoding="utf-8")
        _git(["add", "--", peer_path], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)

    other = tmp_path / "other"
    _git(["clone", "-q", str(origin), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    target = other / peer_path
    if peer_deletes:
        _git(["rm", "-q", "--", peer_path], other)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("peer", encoding="utf-8")
        _git(["add", "--", peer_path], other)
    _git(["commit", "-q", "-m", "peer"], other)
    _git(["push", "-q", "origin", "work/x"], other)

    (repo / "ours.txt").write_text("ours", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours"], repo)
    return repo


def _upstream_tree(repo: Path) -> str:
    _git(["fetch", "-q", "origin"], repo)
    merged = _git(["merge-tree", "--write-tree", "HEAD", "origin/work/x"], repo)
    return merged.splitlines()[0].strip()


def test_hold_set_and_plugin_change_upstream_refuses_touching_nothing(tmp_path):
    repo = _repo_behind_a_peer_push(tmp_path, "control/plugin/X.cpp")
    _git(["config", "coordinator.compileHold", "compiling"], repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    index = _git(["ls-files", "-s"], repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert outcome.failed and "compile-hold" in outcome.failed[0], outcome.failed
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert _git(["ls-files", "-s"], repo) == index
    assert not (repo / "control" / "plugin" / "X.cpp").exists()


def test_hold_clear_lands(tmp_path):
    repo = _repo_behind_a_peer_push(tmp_path, "control/plugin/X.cpp")

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert outcome.exit_code == 0
    assert (repo / "control" / "plugin" / "X.cpp").exists()


def test_hold_set_and_merged_tree_stamped_green_lands(tmp_path):
    repo = _repo_behind_a_peer_push(tmp_path, "control/plugin/X.cpp")
    _git(["config", "coordinator.compileHold", "compiling"], repo)
    _git(["config", "coordinator.compileGreenTree", _upstream_tree(repo)], repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert outcome.exit_code == 0


def test_hold_set_but_change_outside_plugin_lands(tmp_path):
    repo = _repo_behind_a_peer_push(tmp_path, "docs/x.md")
    _git(["config", "coordinator.compileHold", "compiling"], repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert outcome.exit_code == 0


def test_hold_set_and_plugin_deletion_upstream_refuses_touching_nothing(tmp_path):
    repo = _repo_behind_a_peer_push(tmp_path, "control/plugin/X.cpp", seeded=True, peer_deletes=True)
    _git(["config", "coordinator.compileHold", "compiling"], repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert outcome.failed and "compile-hold" in outcome.failed[0], outcome.failed
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert (repo / "control" / "plugin" / "X.cpp").exists()


def test_hold_clear_and_plugin_deletion_upstream_lands(tmp_path):
    repo = _repo_behind_a_peer_push(tmp_path, "control/plugin/X.cpp", seeded=True, peer_deletes=True)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert not (repo / "control" / "plugin" / "X.cpp").exists()


def test_dirty_regenerable_receipt_the_merge_changes_is_reset_and_merge_lands(tmp_path):
    receipt = "state/test-runs/fast-last.json"
    repo = _repo_behind_a_peer_push(tmp_path, receipt, seeded=True)
    (repo / receipt).write_text("local rewrite", encoding="utf-8")

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert (repo / receipt).read_text(encoding="utf-8") == "peer"


def test_dirty_non_receipt_file_the_merge_changes_still_refuses(tmp_path):
    path = "docs/x.md"
    repo = _repo_behind_a_peer_push(tmp_path, path, seeded=True)
    (repo / path).write_text("local rewrite", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo).strip()

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert (repo / path).read_text(encoding="utf-8") == "local rewrite"
