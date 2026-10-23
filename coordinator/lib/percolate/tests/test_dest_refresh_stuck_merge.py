"""Round-start recovery of an interrupted peer-merge, and derived-file regeneration in the
pre-push peer merge, driven through the dest_refresh entry points against real git."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.dest_refresh import (  # noqa: E402
    reconcile_dest_before_push,
    refresh_dest_from_origin,
)
from percolate.interrupted_op import PEER_MERGE_MARKER  # noqa: E402
from percolate.round_derived import EXPECTED_MANIFEST_REL, manifest_bytes  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
STAMP = "coordinator_core/_engine_stamp"
PUBLISHED_AT = "coordinator_core/_engine_published_at"
CONTRACT = "coordinator_core/contract/a.md"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
        **_NO_CONSOLE,
    )


def _out(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, files: dict, message: str) -> None:
    for rel, text in files.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def _derived(tag: str, head: str, extra: dict = None) -> dict:
    paths = {STAMP: tag, PUBLISHED_AT: tag, CONTRACT: "base"}
    paths.update(extra or {})
    return {
        STAMP: tag,
        PUBLISHED_AT: tag,
        EXPECTED_MANIFEST_REL: manifest_bytes({k: "0" * 40 for k in paths}, head).decode("utf-8"),
    }


@pytest.fixture()
def origin_and_clone(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    _commit(origin, {"seed": "seed"}, "seed")
    _git(origin, "checkout", "-b", "candidate")
    _commit(origin, {**_derived("base", "b" * 40), CONTRACT: "base"}, "candidate-seed")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--branch", "candidate", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    _git(clone, "branch", "main", "origin/main")
    _git(origin, "checkout", "-b", "parked")
    return origin, clone


def _peer_commit(origin: Path, files: dict, message: str = "peer") -> None:
    _git(origin, "checkout", "candidate")
    _commit(origin, files, message)
    _git(origin, "checkout", "parked")


def _leave_merge_in_progress(clone: Path, message: str) -> None:
    _git(clone, "fetch", "--no-tags", "origin")
    _git(clone, "merge", "--no-ff", "--no-commit", "-m", message, "origin/candidate")
    assert (clone / ".git" / "MERGE_HEAD").is_file()


def _streams():
    return io.StringIO(), io.StringIO()


def test_marker_merge_left_by_killed_round_is_aborted_at_refresh_start(origin_and_clone):
    origin, clone = origin_and_clone
    _peer_commit(origin, {"peer.txt": "p"})
    _leave_merge_in_progress(clone, f"{PEER_MERGE_MARKER} Merge origin/candidate into candidate")
    out, err = _streams()

    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok, result.reason
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert "aborted an interrupted peer-merge of origin/candidate" in out.getvalue()


def test_default_message_merge_of_the_upstream_is_aborted(origin_and_clone):
    origin, clone = origin_and_clone
    _peer_commit(origin, {"peer.txt": "p"})
    _leave_merge_in_progress(clone, "Merge remote-tracking branch 'origin/candidate'")
    out, err = _streams()

    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok, result.reason
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert "aborted an interrupted peer-merge" in out.getvalue()


def test_foreign_merge_in_progress_is_refused_and_left_alone(origin_and_clone):
    origin, clone = origin_and_clone
    _peer_commit(origin, {"peer.txt": "p"})
    _leave_merge_in_progress(clone, "Merge branch 'feature'")
    out, err = _streams()

    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert not result.ok
    assert "merge in progress" in result.reason
    assert "merge --abort" in result.reason
    assert (clone / ".git" / "MERGE_HEAD").is_file()


def test_cherry_pick_in_progress_is_refused_naming_it(origin_and_clone):
    origin, clone = origin_and_clone
    _commit(clone, {CONTRACT: "ours"}, "ours")
    _peer_commit(origin, {CONTRACT: "theirs"}, "theirs")
    _git(clone, "fetch", "--no-tags", "origin")
    picked = _git(clone, "cherry-pick", "origin/candidate", check=False)
    assert picked.returncode != 0
    assert (clone / ".git" / "CHERRY_PICK_HEAD").is_file()
    out, err = _streams()

    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert not result.ok
    assert "cherry-pick in progress" in result.reason
    assert (clone / ".git" / "CHERRY_PICK_HEAD").is_file()


def test_reconcile_regenerates_derived_files_instead_of_conflicting(origin_and_clone):
    origin, clone = origin_and_clone
    _commit(clone, {**_derived("ours", "a" * 40), "own.txt": "own"}, "ours")
    _peer_commit(origin, {**_derived("peer", "c" * 40), "peer.txt": "p"})
    ours_stamp = (clone / STAMP).read_text(encoding="utf-8")
    out, err = _streams()

    result = reconcile_dest_before_push(clone, out=out, err=err)

    assert result.ok, result.reason
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert len(_out(clone, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert PEER_MERGE_MARKER in _out(clone, "log", "-1", "--format=%s")
    assert (clone / STAMP).read_text(encoding="utf-8") == ours_stamp
    manifest = json.loads((clone / EXPECTED_MANIFEST_REL).read_text(encoding="utf-8"))
    assert manifest["source_head"] == "a" * 40
    assert manifest["paths"]["peer.txt"] == _out(clone, "rev-parse", "HEAD:peer.txt")
    assert manifest["paths"][STAMP] == _out(clone, "rev-parse", f"HEAD:{STAMP}")
    assert _out(clone, "diff", "--name-only", "--diff-filter=U") == ""
    assert "regenerated" in out.getvalue()


def test_reconcile_with_a_real_conflict_aborts_and_leaves_head_unmoved(origin_and_clone):
    origin, clone = origin_and_clone
    _commit(clone, {**_derived("ours", "a" * 40), CONTRACT: "ours"}, "ours")
    _peer_commit(origin, {**_derived("peer", "c" * 40), CONTRACT: "theirs"})
    before = _out(clone, "rev-parse", "HEAD")
    out, err = _streams()

    result = reconcile_dest_before_push(clone, out=out, err=err)

    assert not result.ok
    assert CONTRACT in result.reason
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert _out(clone, "rev-parse", "HEAD") == before


def test_reconcile_start_aborts_an_own_interrupted_merge_first(origin_and_clone):
    origin, clone = origin_and_clone
    _commit(clone, {"own.txt": "own"}, "ours")
    _peer_commit(origin, {"peer.txt": "p"})
    _leave_merge_in_progress(clone, f"{PEER_MERGE_MARKER} Merge origin/candidate into candidate")
    out, err = _streams()

    result = reconcile_dest_before_push(clone, out=out, err=err)

    assert result.ok, result.reason
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert "aborted an interrupted peer-merge" in out.getvalue()
    assert (clone / "peer.txt").is_file()


def test_reconcile_refuses_when_git_will_not_start_the_merge(origin_and_clone):
    origin, clone = origin_and_clone
    _commit(clone, {"own.txt": "own"}, "ours")
    _peer_commit(origin, {"peer.txt": "p"})
    _write(clone, "peer.txt", "untracked local")
    before = _out(clone, "rev-parse", "HEAD")
    out, err = _streams()

    result = reconcile_dest_before_push(clone, out=out, err=err)

    assert not result.ok
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert _out(clone, "rev-parse", "HEAD") == before
    assert (clone / "peer.txt").read_text(encoding="utf-8") == "untracked local"


def test_reconcile_with_nothing_ahead_fast_forwards_without_a_new_commit(origin_and_clone):
    origin, clone = origin_and_clone
    _peer_commit(origin, {"peer.txt": "p"})
    out, err = _streams()

    result = reconcile_dest_before_push(clone, out=out, err=err)

    assert result.ok, result.reason
    assert _out(clone, "rev-parse", "HEAD") == _out(clone, "rev-parse", "origin/candidate")
