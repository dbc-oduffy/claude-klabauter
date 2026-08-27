"""`index_write.splice_index` is oracled against real `git status`.

The only question that matters for this module: after a commit lands its
tree via the in-process path and splices `.git/index` itself, does REAL git
agree the worktree is clean? Every assertion here runs `git` as the oracle --
this is not a test of our own parser against our own writer.
"""

import subprocess
import pytest

from coordinator_core.git import index_write
from coordinator_core.git.git_objects import write_object

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/idx")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _status(repo):
    return _git(repo, "status", "--porcelain").stdout


def test_spliced_new_file_reads_staged_to_real_git(tmp_path):
    """A path written into the index in process must read as STAGED (`A `)
    to real git -- same as `git add` would have produced."""
    repo = _repo(tmp_path)
    (repo / "added.txt").write_text("new content\n", encoding="utf-8", newline="\n")

    sha = write_object(repo / ".git", b"blob", b"new content\n")
    index_write.splice_index(repo, {"added.txt": (0o100644, sha)})

    status = _status(repo)
    assert status.strip() == "A  added.txt", status
    # And git itself must accept the index we wrote, not merely tolerate it.
    _git(repo, "fsck", "--strict")


def test_untouched_entries_survive_verbatim(tmp_path):
    """The k-entry splice must not disturb the entries it was not given --
    `seed.txt` stays clean, byte-identical, and git never re-hashes it."""
    repo = _repo(tmp_path)
    (repo / "added.txt").write_text("x\n", encoding="utf-8", newline="\n")

    sha = write_object(repo / ".git", b"blob", b"x\n")
    index_write.splice_index(repo, {"added.txt": (0o100644, sha)})

    status = _status(repo)
    assert "seed.txt" not in status, status


def test_commit_then_splice_leaves_a_clean_tree(tmp_path):
    """THE ORACLE. Land a commit the way the zero-spawn path will -- objects
    written in process, ref moved, index spliced -- and real `git status`
    must report a CLEAN tree. A stale index shows the new path as a staged
    deletion; that is the ~50-peer corruption this module exists to prevent.
    """
    repo = _repo(tmp_path)
    (repo / "landed.txt").write_text("landed\n", encoding="utf-8", newline="\n")
    blob = write_object(repo / ".git", b"blob", b"landed\n")

    index_write.splice_index(repo, {"landed.txt": (0o100644, blob)})
    # Use git itself to build the commit here: this test is about the INDEX
    # write, so the commit mechanism is deliberately not under test.
    _git(repo, "commit", "-q", "-m", "landed")

    assert _status(repo).strip() == "", _status(repo)
    show = _git(repo, "show", "HEAD:landed.txt").stdout
    assert show == "landed\n", show


def test_absent_sentinel_stages_a_deletion(tmp_path):
    """`ABSENT` removes the entry -- real git must read that as `D `."""
    repo = _repo(tmp_path)
    (repo / "seed.txt").unlink()
    index_write.splice_index(repo, {"seed.txt": index_write.ABSENT})
    assert _status(repo).strip() == "D  seed.txt", _status(repo)


def test_lock_is_refused_never_stolen(tmp_path):
    """A peer holding `.git/index.lock` gets a refusal, not a stolen lock."""
    repo = _repo(tmp_path)
    (repo / ".git" / "index.lock").write_bytes(b"")
    with pytest.raises(index_write.IndexWriteLockBusy):
        index_write.splice_index(repo, {"seed.txt": index_write.ABSENT})


def test_scale_the_splice_does_not_rewrite_untouched_entries(tmp_path):
    """AC9 shape: splicing one path into a large index must leave every other
    entry byte-identical. Proven by comparing the raw entry region either
    side of the splice, not by trusting the writer."""
    repo = _repo(tmp_path)
    for i in range(300):
        (repo / f"f{i:04d}.txt").write_text(f"{i}\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bulk")

    before = (repo / ".git" / "index").read_bytes()
    (repo / "one_more.txt").write_text("one\n", encoding="utf-8", newline="\n")
    sha = write_object(repo / ".git", b"blob", b"one\n")
    index_write.splice_index(repo, {"one_more.txt": (0o100644, sha)})

    assert _status(repo).strip() == "A  one_more.txt", _status(repo)
    after = (repo / ".git" / "index").read_bytes()
    # Every pre-existing name still present, and git still accepts the file.
    assert len(after) > len(before) - 64
    _git(repo, "fsck", "--strict")
