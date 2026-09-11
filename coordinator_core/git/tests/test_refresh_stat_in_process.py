"""`commit.refresh_stat_in_process` -- clears the stat-only ` M` a content-
identical rewrite leaves behind, and never touches a real change.

The measured shape (claude-klabauter, 2026-09-11): a checkout under
`core.autocrlf=true` records the CRLF file's size in the index; a percolate
round then writes the LF payload over it. Same blob, different size, so git's
stat check reports ` M` without hashing while `git diff` shows nothing, and
`percolate-push` refuses on dirt that carries no content.

NEGATIVE SPEC: a refresh never stages. A path whose bytes check in to a
different blob -- an ordinary edit, or an LF write over a blob committed WITH
CRLF -- keeps its index entry byte-for-byte.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.git_index import parse_index_identity

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_CRLF = b"alpha\r\nbeta\r\ngamma\r\n"
_LF = b"alpha\nbeta\ngamma\n"


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path, autocrlf):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", autocrlf)
    return repo


def _commit_crlf_checkout(tmp_path):
    """HEAD holds an LF blob; the worktree and the index's recorded size are
    the CRLF checkout of it -- what any `git checkout`/ff-merge writes under
    `core.autocrlf=true`."""
    repo = _repo(tmp_path, "true")
    (repo / "f.txt").write_bytes(_CRLF)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _status(repo):
    return _git(repo, "status", "--porcelain").stdout


def test_lf_rewrite_over_crlf_checkout_is_refreshed_clean(tmp_path):
    repo = _commit_crlf_checkout(tmp_path)
    (repo / "f.txt").write_bytes(_LF)
    assert _status(repo) == " M f.txt\n"
    assert _git(repo, "diff", "--name-only").stdout == ""

    before = parse_index_identity(repo, wanted={"f.txt"})["f.txt"]
    refreshed = gcommit.refresh_stat_in_process(repo, ["f.txt"])

    assert refreshed == ("f.txt",)
    assert _status(repo) == ""
    after = parse_index_identity(repo, wanted={"f.txt"})["f.txt"]
    assert (after.sha, after.mode) == (before.sha, before.mode)
    assert after.size == len(_LF)


def test_real_edit_is_never_refreshed(tmp_path):
    repo = _commit_crlf_checkout(tmp_path)
    (repo / "f.txt").write_bytes(b"alpha\nCHANGED\ngamma\n")
    before = parse_index_identity(repo, wanted={"f.txt"})["f.txt"]

    assert gcommit.refresh_stat_in_process(repo, ["f.txt"]) == ()
    assert parse_index_identity(repo, wanted={"f.txt"})["f.txt"] == before
    assert _status(repo) == " M f.txt\n"


def test_lf_write_over_a_blob_committed_with_crlf_is_a_real_change(tmp_path):
    """A blob stored WITH CRLF (`i/crlf`, 572 such paths at claude-klabauter)
    checks an LF worktree in to a different blob -- git reports it modified,
    so a refresh must leave it modified."""
    repo = _repo(tmp_path, "false")
    (repo / "f.txt").write_bytes(_CRLF)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "f.txt").write_bytes(_LF)
    assert _git(repo, "diff", "--name-only").stdout == "f.txt\n"

    assert gcommit.refresh_stat_in_process(repo, ["f.txt"]) == ()
    assert _git(repo, "diff", "--name-only").stdout == "f.txt\n"


def test_untracked_missing_and_already_clean_paths_are_skipped(tmp_path):
    repo = _commit_crlf_checkout(tmp_path)
    (repo / "new.txt").write_bytes(_LF)
    index_before = (repo / ".git" / "index").read_bytes()

    assert gcommit.refresh_stat_in_process(repo, ["f.txt", "new.txt", "gone.txt"]) == ()
    assert (repo / ".git" / "index").read_bytes() == index_before
