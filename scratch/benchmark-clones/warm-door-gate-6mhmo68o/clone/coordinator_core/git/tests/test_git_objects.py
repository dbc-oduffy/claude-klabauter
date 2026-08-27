"""Tests for `coordinator_core.git.git_objects`.

The point of the module under test is byte-for-byte compatibility with
real git's own object/ref-writing machinery -- these assert against real
`git` (`git cat-file`, `git write-tree`, `git fsck --strict`, `git
reflog`) in a tmp repo, never against hand-computed shas. `test_cas_ref_*`
exercises the read-under-lock window itself (a lock file pre-created by a
"concurrent" writer), not merely a before/after HEAD comparison, per the
chunk's CAS test requirement.

Spec backlink: docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md, chunk C2
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_objects import (
    _read_loose_object,
    _read_object,
    append_reflog,
    build_tree,
    cas_ref,
    write_object,
)
from coordinator_core.win_portability import no_console_creationflags


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@t", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    (path / "seed.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "seed", cwd=path)


# ---------------------------------------------------------------------------
# write_object
# ---------------------------------------------------------------------------


def test_write_object_blob_matches_git_hash_object(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)
    content = b"hello from write_object\n"

    sha = write_object(gitdir, b"blob", content)

    hash_object = _git("hash-object", "--stdin", cwd=tmp_path, )
    # git hash-object --stdin reads from stdin; use a dedicated call since
    # the helper above pipes DEVNULL.
    result = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(tmp_path),
        input=content,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    expected_sha = result.stdout.decode("ascii").strip()
    assert sha == expected_sha

    cat = _git("cat-file", "-p", sha, cwd=tmp_path)
    assert cat.stdout == content.decode("utf-8")

    fsck = subprocess.run(
        ["git", "fsck", "--strict"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert fsck.returncode == 0, fsck.stderr


def test_write_object_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)
    content = b"idempotent payload\n"

    sha1 = write_object(gitdir, b"blob", content)
    obj_path = gitdir / "objects" / sha1[:2] / sha1[2:]
    first_bytes = obj_path.read_bytes()

    sha2 = write_object(gitdir, b"blob", content)
    second_bytes = obj_path.read_bytes()

    assert sha1 == sha2
    assert first_bytes == second_bytes


# ---------------------------------------------------------------------------
# build_tree
# ---------------------------------------------------------------------------


def test_build_tree_matches_git_write_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)

    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "b.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "dir" / "sub").mkdir()
    (tmp_path / "dir" / "sub" / "c.txt").write_text("c\n", encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)

    expected_tree_sha = _git("write-tree", cwd=tmp_path).stdout.strip()

    ls_tree = _git("ls-tree", "-r", "HEAD" if False else expected_tree_sha, cwd=tmp_path)
    # Build `{path: (mode, sha)}` straight from git's own index listing so
    # the input to `build_tree` is exactly what `git write-tree` consumed.
    ls_files = _git("ls-files", "-s", cwd=tmp_path).stdout
    entries: dict = {}
    for line in ls_files.splitlines():
        if not line:
            continue
        meta, _, path = line.partition("\t")
        mode_str, sha, _stage = meta.split(" ")
        entries[path] = (int(mode_str, 8), sha)

    built_tree_sha = build_tree(gitdir, entries)

    assert built_tree_sha == expected_tree_sha

    fsck = subprocess.run(
        ["git", "fsck", "--strict"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert fsck.returncode == 0, fsck.stderr


# ---------------------------------------------------------------------------
# read side (extracted from pickup_assemble)
# ---------------------------------------------------------------------------


def test_read_object_reads_loose_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    common_dir = resolve_git_dir(tmp_path)
    head_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    result = _read_object(common_dir, head_sha)

    assert result is not None
    kind, payload = result
    assert kind == "commit"
    assert b"seed" in payload


def test_read_object_reads_packed_commit_after_gc(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    common_dir = resolve_git_dir(tmp_path)
    head_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    # Force the loose commit into a pack so the pack-read path is exercised,
    # not merely the loose fallback.
    _git("gc", cwd=tmp_path)
    assert _read_loose_object(common_dir, head_sha) is None

    result = _read_object(common_dir, head_sha)

    assert result is not None
    kind, _payload = result
    assert kind == "commit"


# ---------------------------------------------------------------------------
# cas_ref + reflog
# ---------------------------------------------------------------------------


def test_cas_ref_moves_ref_and_git_agrees(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    common_dir = resolve_git_dir(tmp_path)
    old_sha = _git("rev-parse", "refs/heads/main", cwd=tmp_path).stdout.strip()

    (tmp_path / "extra.md").write_text("extra\n", encoding="utf-8")
    tree_input = {}
    _git("add", "-A", cwd=tmp_path)
    ls_files = _git("ls-files", "-s", cwd=tmp_path).stdout
    for line in ls_files.splitlines():
        meta, _, path = line.partition("\t")
        mode_str, sha, _stage = meta.split(" ")
        tree_input[path] = (int(mode_str, 8), sha)
    tree_sha = build_tree(common_dir, tree_input)
    who = "T T <t@t> 1700000000 +0000"
    commit_payload = (
        f"tree {tree_sha}\nparent {old_sha}\nauthor {who}\ncommitter {who}\n\nnext\n"
    ).encode("utf-8")
    new_sha = write_object(common_dir, b"commit", commit_payload)

    gitdir = resolve_git_dir(tmp_path)
    ok = cas_ref(
        gitdir,
        "refs/heads/main",
        old_sha,
        new_sha,
        reflog_committer=who,
        reflog_message="next",
    )
    assert ok is True

    resolved = _git("rev-parse", "refs/heads/main", cwd=tmp_path).stdout.strip()
    assert resolved == new_sha

    fsck = subprocess.run(
        ["git", "fsck", "--strict"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert fsck.returncode == 0, fsck.stderr

    reflog = _git("reflog", "show", "refs/heads/main", cwd=tmp_path).stdout
    assert new_sha[:7] in reflog
    assert "next" in reflog


def test_cas_ref_rejects_mismatched_expected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)
    real_sha = _git("rev-parse", "refs/heads/main", cwd=tmp_path).stdout.strip()
    bogus_expected = "0" * 40
    fake_new = "1" * 40

    ok = cas_ref(gitdir, "refs/heads/main", bogus_expected, fake_new)

    assert ok is False
    still = _git("rev-parse", "refs/heads/main", cwd=tmp_path).stdout.strip()
    assert still == real_sha
    assert not (gitdir / "refs" / "heads" / "main.lock").exists()


def test_cas_ref_refuses_when_lock_already_held(tmp_path: Path) -> None:
    """Exercises the read->write window itself: a concurrent holder's lock
    file exists BEFORE this call attempts its own CAS, so the O_EXCL
    lockfile acquisition itself must be what fails -- not merely a stale
    HEAD comparison taken before some other process moved it."""
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)
    real_sha = _git("rev-parse", "refs/heads/main", cwd=tmp_path).stdout.strip()

    lock_path = gitdir / "refs" / "heads" / "main.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    try:
        ok = cas_ref(gitdir, "refs/heads/main", real_sha, "2" * 40)
        assert ok is False
        still = _git("rev-parse", "refs/heads/main", cwd=tmp_path).stdout.strip()
        assert still == real_sha
        # The pre-existing lock (not ours) must survive our failed attempt.
        assert lock_path.exists()
    finally:
        lock_path.unlink()


def test_cas_ref_new_ref_requires_expected_none(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)
    head_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    ok = cas_ref(gitdir, "refs/heads/feature", None, head_sha)

    assert ok is True
    resolved = _git("rev-parse", "refs/heads/feature", cwd=tmp_path).stdout.strip()
    assert resolved == head_sha


def test_append_reflog_uses_git_format_and_lf_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    gitdir = resolve_git_dir(tmp_path)
    old_sha = "0" * 40
    new_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()
    who = "T T <t@t> 1700000000 +0000"

    append_reflog(gitdir, "refs/heads/scratch", old_sha, new_sha, who, "manual entry")

    log_path = gitdir / "logs" / "refs" / "heads" / "scratch"
    raw = log_path.read_bytes()
    assert b"\r\n" not in raw
    line = raw.decode("utf-8")
    assert line.startswith(f"{old_sha} {new_sha} {who}\t")
    assert line.endswith("manual entry\n")
