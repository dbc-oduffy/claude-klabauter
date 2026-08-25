"""Tests for `coordinator_core.git.git_state`.

Fixture index files are SYNTHESISED byte-for-byte (per the EM addendum in
`state/dispatch-briefs/2026-08-21-the-commit-path-reads-git-state-without-spawning-git/C1.md`),
never harvested from a `git update-index` run against this box's repo --
this box's own index never carries a symlink, a gitlink, or a v4 index, so
those shapes must be built directly.

`_build_index` below is the shared fixture builder; `_encode_varint` is the
INVERSE of `git_state._decode_varint`, independently re-derived from
`Documentation/technical/index-format.txt`'s offset-encoding description
rather than copied from the module under test, so a bug shared by both would
not cancel out silently.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.git_state import (  # noqa: E402
    IndexParseError,
    head_blobs,
    head_sha,
    head_tree_sha,
    read_index,
    read_tree_spine,
)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

_SIGNATURE = b"DIRC"
_ZERO_SHA = "0" * 40


def _encode_varint(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        value -= 1
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def _entry_fixed(mode: int, sha_hex: str, stage: int, name_len: int, extended: bool) -> bytes:
    fixed = struct.pack(
        ">IIIIIIIIII",
        0, 0,  # ctime s/ns
        0, 0,  # mtime s/ns
        0,     # dev
        0,     # ino
        mode,
        0,     # uid
        0,     # gid
        0,     # size
    )
    fixed += bytes.fromhex(sha_hex)
    flags = ((stage & 0x3) << 12) | min(name_len, 0x0FFF)
    if extended:
        flags |= 0x4000
    fixed += struct.pack(">H", flags)
    if extended:
        fixed += struct.pack(">H", 0)
    return fixed


def _build_index(entries, *, version=2, extensions=b""):
    """`entries`: list of dicts with keys mode(int), sha(hex str), name(str),
    stage(int, default 0), extended(bool, default False)."""
    out = _SIGNATURE + struct.pack(">II", version, len(entries))
    prev_name = b""
    for e in entries:
        name = e["name"].encode("utf-8")
        mode = e["mode"]
        sha_hex = e.get("sha", _ZERO_SHA)
        stage = e.get("stage", 0)
        extended = e.get("extended", False)
        fixed = _entry_fixed(mode, sha_hex, stage, len(name), extended)

        if version == 4:
            common = 0
            while (
                common < len(prev_name)
                and common < len(name)
                and prev_name[common] == name[common]
            ):
                common += 1
            strip = len(prev_name) - common
            suffix = name[common:]
            out += fixed + _encode_varint(strip) + suffix + b"\x00"
            prev_name = name
        else:
            entry_len = len(fixed) + len(name) + 1
            padding = (8 - (entry_len % 8)) % 8
            out += fixed + name + b"\x00" + (b"\x00" * padding)
    out += extensions
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


# ---------------------------------------------------------------------------
# Version parsing


def test_read_index_v2_matches_entries(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [
            {"mode": 0o100644, "sha": "a" * 40, "name": "a.txt"},
            {"mode": 0o100755, "sha": "b" * 40, "name": "bin/tool.sh"},
        ]
    )
    _write_index(repo / ".git", raw)

    snap = read_index(repo)
    assert snap["a.txt"].mode == 0o100644
    assert snap["a.txt"].sha == "a" * 40
    assert snap["a.txt"].stage == 0
    assert snap["bin/tool.sh"].mode == 0o100755


def test_read_index_v3_extended_flags(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"mode": 0o100644, "sha": "c" * 40, "name": "extended.txt", "extended": True}],
        version=3,
    )
    _write_index(repo / ".git", raw)

    snap = read_index(repo)
    assert snap["extended.txt"].sha == "c" * 40


def test_read_index_v4_prefix_compression(tmp_path):
    repo = _plain_repo(tmp_path)
    entries = [
        {"mode": 0o100644, "sha": "1" * 40, "name": "dir/alpha.txt"},
        {"mode": 0o100644, "sha": "2" * 40, "name": "dir/alpha2.txt"},
        {"mode": 0o100644, "sha": "3" * 40, "name": "dir/beta.txt"},
        {"mode": 0o120000, "sha": "4" * 40, "name": "dir/symlink"},
        {"mode": 0o160000, "sha": "5" * 40, "name": "dir/gitlink-submodule"},
    ]
    raw = _build_index(entries, version=4)
    _write_index(repo / ".git", raw)

    snap = read_index(repo)
    assert snap["dir/alpha.txt"].sha == "1" * 40
    assert snap["dir/alpha2.txt"].sha == "2" * 40
    assert snap["dir/beta.txt"].sha == "3" * 40
    assert snap["dir/symlink"].mode == 0o120000
    assert snap["dir/gitlink-submodule"].mode == 0o160000


# ---------------------------------------------------------------------------
# Fail-loud paths


def test_bad_signature_fails_loud(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = b"XXXX" + struct.pack(">II", 2, 0) + b"\x00" * 20
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexParseError):
        read_index(repo)


def test_unsupported_version_fails_loud(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _SIGNATURE + struct.pack(">II", 5, 0) + b"\x00" * 20
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexParseError):
        read_index(repo)


def test_unmerged_stage_fails_loud(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"mode": 0o100644, "sha": "d" * 40, "name": "conflict.txt", "stage": 1}]
    )
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexParseError):
        read_index(repo)


def test_link_extension_fails_loud_as_split_index(tmp_path):
    repo = _plain_repo(tmp_path)
    ext_data = b"\x00" * 20
    ext = b"link" + struct.pack(">I", len(ext_data)) + ext_data
    raw = _build_index(
        [{"mode": 0o100644, "sha": "e" * 40, "name": "f.txt"}], extensions=ext
    )
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexParseError):
        read_index(repo)


def test_sharedindex_sibling_fails_loud_as_split_index(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([{"mode": 0o100644, "sha": "f" * 40, "name": "g.txt"}])
    gitdir = repo / ".git"
    _write_index(gitdir, raw)
    (gitdir / "sharedindex.abc123").write_bytes(b"\x00" * 12)

    with pytest.raises(IndexParseError):
        read_index(repo)


def test_missing_index_file_returns_empty_snapshot_not_an_error(tmp_path):
    repo = _plain_repo(tmp_path)

    snap = read_index(repo)
    assert dict(snap) == {}
    assert snap.stat_identity is None


# ---------------------------------------------------------------------------
# Path shapes


def test_path_with_space(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([{"mode": 0o100644, "sha": "1" * 40, "name": "has space.txt"}])
    _write_index(repo / ".git", raw)

    snap = read_index(repo)
    assert "has space.txt" in snap


def test_non_ascii_path(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([{"mode": 0o100644, "sha": "2" * 40, "name": "café.txt"}])
    _write_index(repo / ".git", raw)

    snap = read_index(repo)
    assert "café.txt" in snap


def test_staged_deletion_absent_path_not_in_snapshot(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([{"mode": 0o100644, "sha": "3" * 40, "name": "kept.txt"}])
    _write_index(repo / ".git", raw)

    snap = read_index(repo)
    assert "kept.txt" in snap
    assert "removed.txt" not in snap


# ---------------------------------------------------------------------------
# stat identity / no-cache


def test_stat_identity_differs_after_index_mutated_between_calls(tmp_path):
    repo = _plain_repo(tmp_path)
    raw1 = _build_index([{"mode": 0o100644, "sha": "1" * 40, "name": "a.txt"}])
    index_path = _write_index(repo / ".git", raw1)

    snap1 = read_index(repo)

    raw2 = _build_index(
        [
            {"mode": 0o100644, "sha": "1" * 40, "name": "a.txt"},
            {"mode": 0o100644, "sha": "2" * 40, "name": "b.txt"},
        ]
    )
    index_path.write_bytes(raw2)

    snap2 = read_index(repo)

    assert snap1.stat_identity != snap2.stat_identity
    assert "b.txt" in snap2


# ---------------------------------------------------------------------------
# head_sha


def test_unborn_branch_no_head_returns_none(tmp_path):
    repo = _plain_repo(tmp_path)
    # No HEAD file at all -- an unborn repo before `git init` writes one.
    assert head_sha(repo) is None


def test_head_sha_detached(tmp_path):
    repo = _plain_repo(tmp_path)
    (repo / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")

    assert head_sha(repo) == "a" * 40


def test_head_sha_symref_to_loose_ref(tmp_path):
    repo = _plain_repo(tmp_path)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    ref_dir = repo / ".git" / "refs" / "heads"
    ref_dir.mkdir(parents=True)
    (ref_dir / "main").write_text("b" * 40 + "\n", encoding="utf-8")

    assert head_sha(repo) == "b" * 40


def test_head_sha_symref_falls_back_to_packed_refs(tmp_path):
    repo = _plain_repo(tmp_path)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n" + "c" * 40 + " refs/heads/main\n",
        encoding="utf-8",
    )

    assert head_sha(repo) == "c" * 40


def test_head_sha_symref_no_ref_no_packed_refs_returns_none(tmp_path):
    repo = _plain_repo(tmp_path)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    assert head_sha(repo) is None


# ---------------------------------------------------------------------------
# Linked worktree: index/HEAD are worktree-private, packed-refs is shared


def test_linked_worktree_reads_private_index_and_head_not_main(tmp_path):
    main_common = tmp_path / "main" / ".git"
    private_gitdir = main_common / "worktrees" / "wt"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

    main_common.mkdir(parents=True, exist_ok=True)
    main_raw = _build_index([{"mode": 0o100644, "sha": "1" * 40, "name": "main-only.txt"}])
    (main_common / "index").write_bytes(main_raw)
    (main_common / "HEAD").write_text("m" * 40 + "\n", encoding="utf-8")

    wt_raw = _build_index([{"mode": 0o100644, "sha": "2" * 40, "name": "wt-only.txt"}])
    (private_gitdir / "index").write_bytes(wt_raw)
    (private_gitdir / "HEAD").write_text("w" * 40 + "\n", encoding="utf-8")

    repo_root = tmp_path / "wt"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    snap = read_index(repo_root)
    assert "wt-only.txt" in snap
    assert "main-only.txt" not in snap
    assert head_sha(repo_root) == "w" * 40


def test_linked_worktree_head_symref_falls_back_to_shared_packed_refs(tmp_path):
    main_common = tmp_path / "main" / ".git"
    private_gitdir = main_common / "worktrees" / "wt"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (private_gitdir / "HEAD").write_text("ref: refs/heads/shared\n", encoding="utf-8")

    main_common.mkdir(parents=True, exist_ok=True)
    (main_common / "packed-refs").write_text(
        "d" * 40 + " refs/heads/shared\n", encoding="utf-8"
    )

    repo_root = tmp_path / "wt"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    assert head_sha(repo_root) == "d" * 40


def test_linked_worktree_head_symref_resolves_loose_ref_from_common_dir(tmp_path):
    # refs/heads/* are never worktree-private -- the loose ref for a branch
    # tip must be read from the SHARED common dir, not the worktree-private
    # gitdir, even though HEAD itself lives in the private gitdir. Before
    # the fix this read `(gitdir / ref)` (worktree-private), which misses
    # for any unpacked branch tip and silently falls through to
    # packed-refs, returning None or a stale sha.
    main_common = tmp_path / "main" / ".git"
    private_gitdir = main_common / "worktrees" / "wt"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (private_gitdir / "HEAD").write_text("ref: refs/heads/loose\n", encoding="utf-8")

    main_common.mkdir(parents=True, exist_ok=True)
    (main_common / "refs" / "heads").mkdir(parents=True)
    (main_common / "refs" / "heads" / "loose").write_text(
        "l" * 40 + "\n", encoding="utf-8"
    )

    repo_root = tmp_path / "wt"
    repo_root.mkdir()
    (repo_root / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    assert head_sha(repo_root) == "l" * 40


# ---------------------------------------------------------------------------
# Equality against `git ls-files -s` over this repo's own live index


@pytest.mark.spawns_process
def test_read_index_matches_git_ls_files_over_live_repo():
    repo_root = str(Path(__file__).resolve().parents[3])
    out = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "-s"],
        capture_output=True,
        text=True,
        timeout=30,
        **no_console_creationflags(),
    ).stdout

    git_map = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode_str, sha, stage_str = meta.split(" ")
        git_map[path] = (int(mode_str, 8), sha, int(stage_str))

    snap = read_index(repo_root)

    only_in_git = [p for p in git_map if p not in snap]
    only_in_parse = [p for p in snap if p not in git_map]
    mismatched = [
        p
        for p, v in git_map.items()
        if p in snap and (snap[p].mode, snap[p].sha, snap[p].stage) != v
    ]

    assert only_in_git == []
    assert only_in_parse == []
    assert mismatched == []
    assert len(snap) == len(git_map)


# ---------------------------------------------------------------------------
# head_blobs -- the one retained spawn


@pytest.mark.spawns_process
def test_head_blobs_reads_this_repos_own_head_tree():
    # `git_state.py` itself is uncommitted while this chunk lands, so this
    # probes a file already present in HEAD -- `run.py`, its sibling module.
    repo_root = str(Path(__file__).resolve().parents[3])
    result = head_blobs(repo_root, ["coordinator_core/git/run.py"])

    assert "coordinator_core/git/run.py" in result
    mode, sha = result["coordinator_core/git/run.py"]
    assert mode in (0o100644, 0o100755)
    assert len(sha) == 40


@pytest.mark.spawns_process
def test_head_blobs_empty_paths_returns_empty_dict_no_spawn():
    assert head_blobs(".", []) == {}


@pytest.mark.spawns_process
def test_head_blobs_admits_gitlink_160000(tmp_path):
    """A submodule gitlink at HEAD MUST appear in `head_blobs`'s result --
    filtering it out (obj_type == "commit", not "blob") makes any caller
    that treats an absent `head_entry` as "no HEAD counterpart" misreport a
    genuinely dirty tracked gitlink as staged (the regression this test
    pins). SYNTHESISED via a direct index write, per this file's module
    docstring -- this repo's own index carries no 160000 entry to build a
    fixture from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    kwargs = dict(cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags())
    subprocess.run(["git", "init", "-q"], **kwargs)
    subprocess.run(["git", "config", "user.email", "t@t.example"], **kwargs)
    subprocess.run(["git", "config", "user.name", "t"], **kwargs)
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], **kwargs)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], **kwargs)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], **kwargs
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{head_sha},vendor/sub"],
        **kwargs,
    )
    subprocess.run(["git", "commit", "-q", "-m", "add gitlink"], **kwargs)

    result = head_blobs(str(repo), ["vendor/sub"])

    assert "vendor/sub" in result
    mode, sha = result["vendor/sub"]
    assert mode == 0o160000
    assert sha == head_sha


# ---------------------------------------------------------------------------
# head_tree_sha / read_tree_spine -- real repos, real git.
#
# `head_sha` above is exercised against synthesised `.git/HEAD` bytes; these
# two readers are exercised against a real `git init` repo because the
# assertion IS "matches real git's own commit/tree object encoding and
# `git ls-tree` output", per this chunk's test-surface brief -- a
# synthesised commit/tree object would just be re-asserting this module's
# own encoding against itself.


def _git(args, *, cwd):
    kwargs = dict(cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())
    return subprocess.run(["git", *args], **kwargs)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@t.example"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)


def _ls_tree_one_dir(repo: Path, dirpath: str) -> dict:
    """`{name: (mode, sha)}` for one directory's immediate children, via
    `git ls-tree HEAD -- <dirpath>` (non-recursive) -- the independent
    oracle `read_tree_spine`'s output is asserted against."""
    target = dirpath + "/" if dirpath else "./"
    out = _git(["ls-tree", "HEAD", target], cwd=repo).stdout
    entries = {}
    for line in out.splitlines():
        if not line:
            continue
        meta, _, name = line.partition("\t")
        mode_str, _obj_type, sha = meta.split(" ")
        entries[name.rsplit("/", 1)[-1]] = (int(mode_str, 8), sha)
    return entries


@pytest.mark.spawns_process
def test_head_tree_sha_matches_git_rev_parse_loose(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    expected = _git(["rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()
    assert head_tree_sha(repo) == expected


@pytest.mark.spawns_process
def test_head_tree_sha_matches_git_rev_parse_after_gc_packs_head(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)
    _git(["gc", "-q", "--aggressive"], cwd=repo)

    expected = _git(["rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()
    assert head_tree_sha(repo) == expected


@pytest.mark.spawns_process
def test_read_tree_spine_deep_path_matches_ls_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "root.txt").write_text("r", encoding="utf-8")
    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_text("l", encoding="utf-8")
    _git(["add", "--", "."], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    spine = read_tree_spine(repo, ["a/b/c/leaf.txt"])

    assert spine is not None
    for dirpath in ("", "a", "a/b", "a/b/c"):
        assert spine[dirpath] == _ls_tree_one_dir(repo, dirpath), dirpath
    assert "leaf.txt" in spine["a/b/c"]
    assert "root.txt" in spine[""]


@pytest.mark.spawns_process
def test_read_tree_spine_root_path_matches_ls_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "root.txt").write_text("r", encoding="utf-8")
    _git(["add", "--", "."], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    spine = read_tree_spine(repo, ["root.txt"])

    assert spine is not None
    assert spine[""] == _ls_tree_one_dir(repo, "")
    assert "root.txt" in spine[""]


@pytest.mark.spawns_process
def test_read_tree_spine_linked_worktree(tmp_path):
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    nested = main_repo / "dir"
    nested.mkdir()
    (nested / "f.txt").write_text("f", encoding="utf-8")
    _git(["add", "--", "."], cwd=main_repo)
    _git(["commit", "-q", "-m", "seed"], cwd=main_repo)

    wt_path = tmp_path / "wt"
    _git(["worktree", "add", "-q", str(wt_path), "-b", "wt-branch"], cwd=main_repo)

    spine = read_tree_spine(wt_path, ["dir/f.txt"])

    assert spine is not None
    assert spine[""] == _ls_tree_one_dir(wt_path, "")
    assert spine["dir"] == _ls_tree_one_dir(wt_path, "dir")
    assert "f.txt" in spine["dir"]


@pytest.mark.spawns_process
def test_read_tree_spine_gitlink_leaf_not_descended_into(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("x", encoding="utf-8")
    _git(["add", "--", "README.md"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)
    submodule_sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    _git(
        ["update-index", "--add", "--cacheinfo", f"160000,{submodule_sha},vendor/sub"],
        cwd=repo,
    )
    _git(["commit", "-q", "-m", "add gitlink"], cwd=repo)

    spine = read_tree_spine(repo, ["vendor/sub"])

    assert spine is not None
    assert spine["vendor"] == _ls_tree_one_dir(repo, "vendor")
    mode, sha = spine["vendor"]["sub"]
    assert mode == 0o160000
    assert sha == submodule_sha
    assert "vendor/sub" not in spine
