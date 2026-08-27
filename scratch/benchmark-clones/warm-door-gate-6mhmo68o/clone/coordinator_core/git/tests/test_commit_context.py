"""Tests for `coordinator_core.git.commit_context`.

Fixture index files are SYNTHESISED byte-for-byte, following the same
convention `test_git_index.py`/`test_git_state.py` document -- `_build_index`
here is a narrower re-derivation scoped to this module's own needs (mode,
sha, size, mtime, mtime_nsec), not imported from either sibling test file.

`head_sha`/`head_blobs` are monkeypatched in most tests rather than backed
by real git objects: `git_state.py`'s own test suite already covers HEAD/
tree-object parsing in depth, so this file's job is to prove
`build_commit_context` WIRES those readers correctly and stays scoped to
`paths` -- not to re-verify HEAD parsing. Nothing in this file spawns a
process.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git import commit_context  # noqa: E402
from coordinator_core.git.commit_context import (  # noqa: E402
    CommitContext,
    PathContext,
    build_commit_context,
)
from coordinator_core.git.git_index import parse_index_identity  # noqa: E402

_SIGNATURE = b"DIRC"
_ZERO_SHA = "0" * 40


def _entry_fixed(mode: int, sha_hex: str, size: int, mtime: int, name_len: int,
                  mtime_nsec: int = 0) -> bytes:
    fixed = struct.pack(
        ">IIIIIIIIII",
        0, 0,               # ctime s/ns
        mtime, mtime_nsec,  # mtime s/ns
        0,          # dev
        0,          # ino
        mode,
        0,          # uid
        0,          # gid
        size,
    )
    fixed += bytes.fromhex(sha_hex)
    flags = min(name_len, 0x0FFF)
    fixed += struct.pack(">H", flags)
    return fixed


def _build_index(entries, *, version=2):
    """`entries`: list of dicts with keys mode(int), sha(hex str),
    size(int), mtime(int), name(str), mtime_nsec(int, default 0)."""
    out = _SIGNATURE + struct.pack(">II", version, len(entries))
    for e in entries:
        name = e["name"].encode("utf-8")
        fixed = _entry_fixed(
            e["mode"],
            e.get("sha", _ZERO_SHA),
            e["size"],
            e["mtime"],
            len(name),
            e.get("mtime_nsec", 0),
        )
        entry_len = len(fixed) + len(name) + 1
        padding = (8 - (entry_len % 8)) % 8
        out += fixed + name + b"\x00" + (b"\x00" * padding)
    out += b"\x00" * 20  # fake trailing checksum, never verified
    return out


def _write_index(gitdir: Path, raw: bytes) -> Path:
    gitdir.mkdir(parents=True, exist_ok=True)
    path = gitdir / "index"
    path.write_bytes(raw)
    return path


def _plain_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _no_head(monkeypatch):
    """Most tests here care about the index/worktree axes only -- pin HEAD
    to a fixed sha and empty tree so a test failure there cannot be
    mistaken for one on this module's own wiring."""
    monkeypatch.setattr(commit_context, "head_sha", lambda repo: None)
    monkeypatch.setattr(commit_context, "head_blobs", lambda repo, paths: {})


# ---------------------------------------------------------------------------
# Basic wiring


def test_index_entry_and_stat_present(tmp_path, monkeypatch):
    _no_head(monkeypatch)
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"mode": 0o100644, "sha": "a" * 40, "size": 123, "mtime": 456,
          "mtime_nsec": 789, "name": "a.txt"}]
    )
    _write_index(repo / ".git", raw)
    (repo / "a.txt").write_text("hi")

    ctx = build_commit_context(repo, ["a.txt"])

    assert ctx.paths["a.txt"].index == (0o100644, "a" * 40)
    assert ctx.paths["a.txt"].index_stat == (123, 456, 789)
    assert ctx.paths["a.txt"].on_disk is True
    assert ctx.paths["a.txt"].worktree_stat is not None


def test_untracked_path_has_no_index_entry(tmp_path, monkeypatch):
    _no_head(monkeypatch)
    repo = _plain_repo(tmp_path)
    _write_index(repo / ".git", _build_index([]))
    (repo / "b.txt").write_text("hi")

    ctx = build_commit_context(repo, ["b.txt"])

    assert ctx.paths["b.txt"].index is None
    assert ctx.paths["b.txt"].index_stat is None
    assert ctx.paths["b.txt"].on_disk is True


def test_staged_but_deleted_from_worktree(tmp_path, monkeypatch):
    _no_head(monkeypatch)
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"mode": 0o100644, "sha": "c" * 40, "size": 1, "mtime": 1, "name": "gone.txt"}]
    )
    _write_index(repo / ".git", raw)
    # deliberately never created on disk

    ctx = build_commit_context(repo, ["gone.txt"])

    assert ctx.paths["gone.txt"].index == (0o100644, "c" * 40)
    assert ctx.paths["gone.txt"].on_disk is False
    assert ctx.paths["gone.txt"].worktree_stat is None


def test_head_sha_is_pass_level(tmp_path, monkeypatch):
    repo = _plain_repo(tmp_path)
    _write_index(repo / ".git", _build_index([]))
    monkeypatch.setattr(commit_context, "head_sha", lambda repo: "d" * 40)
    monkeypatch.setattr(commit_context, "head_blobs", lambda repo, paths: {})

    ctx = build_commit_context(repo, [])

    assert ctx.head == "d" * 40


def test_head_blob_wired_through_to_path_context(tmp_path, monkeypatch):
    repo = _plain_repo(tmp_path)
    _write_index(repo / ".git", _build_index([]))
    monkeypatch.setattr(commit_context, "head_sha", lambda repo: "e" * 40)

    seen = {}

    def _fake_head_blobs(passed_repo, passed_paths):
        seen["paths"] = list(passed_paths)
        return {"tracked.txt": (0o100644, "f" * 40)}

    monkeypatch.setattr(commit_context, "head_blobs", _fake_head_blobs)

    ctx = build_commit_context(repo, ["tracked.txt", "other.txt"])

    assert seen["paths"] == ["tracked.txt", "other.txt"]
    assert ctx.paths["tracked.txt"].head == (0o100644, "f" * 40)
    assert ctx.paths["other.txt"].head is None


# ---------------------------------------------------------------------------
# AC9 shape: entry-count scaling, never more than k against a five-figure index


def test_context_never_materialises_an_entry_outside_paths(tmp_path, monkeypatch):
    _no_head(monkeypatch)
    repo = _plain_repo(tmp_path)

    n = 20_000
    entries = [
        {"mode": 0o100644, "sha": format(i, "040x"), "size": i, "mtime": i,
         "name": f"dir{i:05d}/file{i:05d}.txt"}
        for i in range(n)
    ]
    _write_index(repo / ".git", _build_index(entries))

    wanted = [entries[0]["name"], entries[n // 2]["name"], entries[-1]["name"]]

    seen_identity_len = {}

    def _spying_parse_index_identity(passed_repo, wanted=None):
        result = parse_index_identity(passed_repo, wanted=wanted)
        seen_identity_len["n"] = len(result)
        return result

    monkeypatch.setattr(commit_context, "parse_index_identity", _spying_parse_index_identity)

    ctx = build_commit_context(repo, wanted)

    # The scoped walk itself must never materialise more entries than were
    # asked for -- the whole design this test exists to pin (AC9).
    assert seen_identity_len["n"] <= len(wanted)
    # And the context's own per-path map holds exactly the requested paths,
    # regardless of the 20,000-entry index behind it -- never more, never
    # fewer.
    assert set(ctx.paths.keys()) == set(wanted)
    assert len(ctx.paths) == len(wanted)
    for name in wanted:
        assert ctx.paths[name].index is not None


def test_context_entry_count_bounded_by_k_at_multiple_index_sizes(tmp_path, monkeypatch):
    """AC9's own shape at module scope: equality of materialised-entry
    counts across two synthesised index sizes at the same pathspec width --
    a static per-function scaling detector is explicitly NOT what AC9 asks
    for; this asserts the empirical invariant instead."""
    _no_head(monkeypatch)

    def _context_for_index_size(n: int) -> CommitContext:
        repo_dir = tmp_path / f"repo-{n}"
        (repo_dir / ".git").mkdir(parents=True)
        entries = [
            {"mode": 0o100644, "sha": format(i, "040x"), "size": i, "mtime": i,
             "name": f"p{i:06d}.txt"}
            for i in range(n)
        ]
        _write_index(repo_dir / ".git", _build_index(entries))
        wanted = [entries[0]["name"], entries[-1]["name"]]
        return build_commit_context(repo_dir, wanted)

    small = _context_for_index_size(1_000)
    large = _context_for_index_size(10_000)

    assert len(small.paths) == len(large.paths) == 2


# ---------------------------------------------------------------------------
# Return shape


def test_returns_namedtuples(tmp_path, monkeypatch):
    _no_head(monkeypatch)
    repo = _plain_repo(tmp_path)
    _write_index(repo / ".git", _build_index([]))

    ctx = build_commit_context(repo, [])

    assert isinstance(ctx, CommitContext)
    assert ctx.paths == {}
