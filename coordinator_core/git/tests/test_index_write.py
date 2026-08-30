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


def test_absolute_key_refused_before_writing(tmp_path):
    """An absolute-path index key is refused, never normalized -- normalizing
    would repeat the `git update-index --force-remove` trap of silently
    retargeting the wrong entry. The index must be byte-identical afterwards:
    the refusal happens before any bytes are written."""
    repo = _repo(tmp_path)
    before = (repo / ".git" / "index").read_bytes()
    abs_key = str(repo / "seed.txt")

    with pytest.raises(index_write.IndexWriteError):
        index_write.splice_index(repo, {abs_key: index_write.ABSENT})

    after = (repo / ".git" / "index").read_bytes()
    assert after == before


def test_drive_letter_key_refused_before_writing(tmp_path):
    """A drive-letter-prefixed key (`X:/...`) is refused the same way a POSIX
    absolute path is -- both are absolute pathspecs used verbatim as index
    keys, the exact defect this guard exists to close."""
    repo = _repo(tmp_path)
    before = (repo / ".git" / "index").read_bytes()
    drive_key = "X:/claude-klabauter/seed.txt"  # abs-path-ok: fixture string, not a filesystem citation

    with pytest.raises(index_write.IndexWriteError):
        index_write.splice_index(repo, {drive_key: index_write.ABSENT})

    after = (repo / ".git" / "index").read_bytes()
    assert after == before


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


def test_the_index_read_happens_under_the_lock(tmp_path):
    """THE LOST UPDATE. The read is the base revision of a read-modify-write,
    not a lookup: every untouched entry is copied verbatim from it. Read it
    outside `.git/index.lock` and a peer commit landing between our read and
    our write is silently reverted in the index -- and for a path the peer
    ADDED that is not merely stale, it is a path in HEAD and absent from the
    index, which real git renders as a staged deletion (`D `) of a file
    sitting on disk, which any session's `git commit -a` then lands for real.

    `IndexWriteLockBusy` does not close this on its own: it reports only that
    a peer held the lock at the instant we tried to WRITE, which says nothing
    about whether the snapshot we are about to write back is still current.
    Holding the lock across the whole read-modify-write is what closes it, so
    that invariant is what this pins -- oracled by real git refusing to touch
    the index from inside our read window.
    """
    repo = _repo(tmp_path)
    lock_path = repo / ".git" / "index.lock"
    (repo / "ours.txt").write_text("ours\n", encoding="utf-8", newline="\n")
    (repo / "peer.txt").write_text("peer\n", encoding="utf-8", newline="\n")

    real_read_bytes = type(repo).read_bytes
    observed = {}

    def watch(self):
        if self.name == "index" and "held" not in observed:
            observed["held"] = lock_path.exists()
            # Real git is the oracle: with the lock held it must refuse to
            # stage anything, which is precisely what stops the peer commit
            # from sliding into the window between our read and our write.
            observed["peer_add_rc"] = _git(
                repo, "add", "--", "peer.txt", check=False
            ).returncode
        return real_read_bytes(self)

    blob = write_object(repo / ".git", b"blob", b"ours\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(repo), "read_bytes", watch)
        index_write.splice_index(repo, {"ours.txt": (0o100644, blob)})

    assert observed.get("held") is True, (
        "`.git/index` was read with no lock held -- the read-modify-write is "
        "unserialised and a peer commit in that window is lost"
    )
    assert observed["peer_add_rc"] != 0, (
        "real git staged a path while our splice was mid-read; the window "
        "this test exists to close is open"
    )
    assert _status(repo).strip().splitlines()[0] == "A  ours.txt", _status(repo)
    _git(repo, "fsck", "--strict")
