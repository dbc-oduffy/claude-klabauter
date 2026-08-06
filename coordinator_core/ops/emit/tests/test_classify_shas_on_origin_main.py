"""Tests for classify_shas_on_origin_main — the batched tri-state origin/main classifier.

Purpose: pin the tri-state contract (True/False/None) that replaced a per-SHA
sha_on_origin_main spawn loop inside _stamp_shipped_sha / _stamp_node_shipped_sha /
_stamp_closure_reachability with two subprocess spawns total, regardless of record count.
The load-bearing property under test: a naive `git rev-list origin/main` set-membership
check would collapse False (valid commit, not on main) and None (bad object / unreachable)
into the same bucket — this suite proves the batched implementation keeps them apart,
element-for-element identical to calling sha_on_origin_main once per SHA.

Uses real throwaway git repos (no mocking) so the two-spawn git plumbing (rev-list +
cat-file --batch-check) is exercised for real, mirroring the existing real-repo style of
test_check_shipped_on_main.py and the commit_closures reachability tests in
test_emit_parity.py.

Spec backlink: dispatched fix for the 108-spawn sha_on_origin_main hot spot,
coordinator_core/ops/emit/envelope.py:530 (measured against example-doctrine-repo, 2026-07-29).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.emit.envelope import classify_shas_on_origin_main, sha_on_origin_main


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, message: str, content: str) -> str:
    (repo / "file.txt").write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_empty_input_returns_empty_dict_no_git_io(tmp_path: Path) -> None:
    assert classify_shas_on_origin_main(tmp_path, []) == {}


def test_three_way_classification_matches_per_sha_oracle(tmp_path: Path) -> None:
    """The three real-world states classify identically to calling sha_on_origin_main once
    per SHA: on-main -> True, valid-but-not-on-main -> False, bad object -> None."""
    _init_repo(tmp_path)
    on_main_sha = _commit(tmp_path, "base", "base\n")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", on_main_sha)
    off_main_sha = _commit(tmp_path, "unmerged", "unmerged\n")
    bad_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    shas = [on_main_sha, off_main_sha, bad_sha]
    batched = classify_shas_on_origin_main(tmp_path, shas)

    assert batched == {
        on_main_sha: True,
        off_main_sha: False,
        bad_sha: None,
    }

    # Cross-check against the un-batched, per-SHA oracle this function replaces.
    for sha in shas:
        assert batched[sha] == sha_on_origin_main(tmp_path, sha), (
            f"batched result for {sha!r} diverges from the per-SHA sha_on_origin_main oracle"
        )


def test_duplicate_shas_collapse_to_one_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    on_main_sha = _commit(tmp_path, "base", "base\n")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", on_main_sha)

    result = classify_shas_on_origin_main(tmp_path, [on_main_sha, on_main_sha, on_main_sha])

    assert result == {on_main_sha: True}


def test_no_origin_main_ref_degrades_every_sha_to_none(tmp_path: Path) -> None:
    """No refs/remotes/origin/main at all: git rev-list origin/main fails outright, and every
    candidate — including an otherwise-valid commit — must degrade to None, never False."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "solo", "solo\n")
    # Deliberately no refs/remotes/origin/main.

    result = classify_shas_on_origin_main(tmp_path, [sha])

    assert result == {sha: None}, (
        "an unreachable origin/main must degrade every sha to None (indeterminate), "
        "never silently read as False ('definitely not shipped')"
    )


def test_only_bad_object_among_valid_shas_isolates_correctly(tmp_path: Path) -> None:
    """A bad object mixed with valid on-main/off-main SHAs must classify each independently —
    the batch call must not let one bad entry corrupt the others' classification."""
    _init_repo(tmp_path)
    on_main_sha = _commit(tmp_path, "base", "base\n")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", on_main_sha)
    off_main_sha = _commit(tmp_path, "unmerged", "unmerged\n")
    bad_sha = "0000000000000000000000000000000000dead"

    result = classify_shas_on_origin_main(tmp_path, [on_main_sha, bad_sha, off_main_sha])

    assert result[on_main_sha] is True
    assert result[off_main_sha] is False
    assert result[bad_sha] is None
