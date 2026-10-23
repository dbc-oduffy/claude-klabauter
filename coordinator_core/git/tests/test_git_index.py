"""Tests for `coordinator_core.git.git_index`.

Fixture index files are SYNTHESISED byte-for-byte, following the same
convention `test_git_state.py` documents: this box's own index never
carries a v4 file, so that shape must be built directly rather than
harvested. `_build_index` here is a NARROWER re-derivation scoped to this
module's own (mode, size, mtime) fields -- not imported from
`test_git_state.py`, so a bug shared by both fixture builders would not
cancel out silently.

Nothing in this file spawns a process.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.git_index import (  # noqa: E402
    IndexFlaggedIdentity,
    IndexIdentity,
    parse_index_identity,
    IndexParseError,
    IndexV4Unsupported,
    parse_index_stat,
    scoped_status,
)

_SIGNATURE = b"DIRC"
_ZERO_SHA = bytes(20)


def _entry_fixed(
    mode: int,
    size: int,
    mtime: int,
    name_len: int,
    extended: bool,
    mtime_nsec: int = 0,
    stage: int = 0,
    xflags: int = 0,
    assume_valid: bool = False,
) -> bytes:
    fixed = struct.pack(
        ">IIIIIIIIII",
        0, 0,
        mtime, mtime_nsec,
        0,
        0,
        mode,
        0,
        0,
        size,
    )
    fixed += _ZERO_SHA
    flags = min(name_len, 0x0FFF)
    if extended:
        flags |= 0x4000
    flags |= (stage & 0x3) << 12
    if assume_valid:
        flags |= 0x8000
    fixed += struct.pack(">H", flags)
    if extended:
        fixed += struct.pack(">H", xflags)
    return fixed


def _build_index(entries, *, version=2):
    out = _SIGNATURE + struct.pack(">II", version, len(entries))
    for e in entries:
        name = e["name"].encode("utf-8")
        fixed = _entry_fixed(
            e["mode"],
            e["size"],
            e["mtime"],
            len(name),
            e.get("extended", False),
            e.get("mtime_nsec", 0),
            e.get("stage", 0),
            e.get("xflags", 0),
            e.get("assume_valid", False),
        )
        entry_len = len(fixed) + len(name) + 1
        padding = (8 - (entry_len % 8)) % 8
        out += fixed + name + b"\x00" + (b"\x00" * padding)
    out += b"\x00" * 20
    return out


def _build_v4_index(entries):
    out = _SIGNATURE + struct.pack(">II", 4, len(entries))
    for e in entries:
        name = e["name"].encode("utf-8")
        fixed = _entry_fixed(e["mode"], e["size"], e["mtime"], len(name), False)
        out += fixed + b"\x00" + name + b"\x00"
    out += b"\x00" * 20
    return out


def _write_index(gitdir: Path, raw: bytes) -> Path:
    gitdir.mkdir(parents=True, exist_ok=True)
    path = gitdir / "index"
    path.write_bytes(raw)
    return path


def _plain_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_v4_index_refuses(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_v4_index([{"name": "a.txt", "mode": 0o100644, "size": 3, "mtime": 100}])
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexV4Unsupported):
        parse_index_stat(repo)


def test_stat_matching_file_reads_clean_without_reading_bytes(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "clean.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index(
        [{"name": "clean.txt", "mode": 0o100644, "size": st.st_size, "mtime": int(st.st_mtime)}]
    )
    _write_index(repo / ".git", raw)

    orig_read_bytes = Path.read_bytes

    def _guard(self, *a, **kw):
        if self.name == "clean.txt":
            raise AssertionError("scoped_status must not read file bytes")
        return orig_read_bytes(self, *a, **kw)

    Path.read_bytes = _guard
    try:
        verdicts = scoped_status(repo, ["clean.txt"])
    finally:
        Path.read_bytes = orig_read_bytes

    assert verdicts == {"clean.txt": "clean"}


def test_size_only_change_reads_candidate(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "grew.txt"
    f.write_bytes(b"abcdef")
    st = f.stat()
    raw = _build_index(
        [{"name": "grew.txt", "mode": 0o100644, "size": 3, "mtime": int(st.st_mtime)}]
    )
    _write_index(repo / ".git", raw)

    verdicts = scoped_status(repo, ["grew.txt"])
    assert verdicts == {"grew.txt": "candidate"}


def test_mtime_only_change_reads_candidate(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "touched.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index(
        [{
            "name": "touched.txt",
            "mode": 0o100644,
            "size": st.st_size,
            "mtime": int(st.st_mtime) - 1000,
        }]
    )
    _write_index(repo / ".git", raw)

    verdicts = scoped_status(repo, ["touched.txt"])
    assert verdicts == {"touched.txt": "candidate"}


def test_deleted_path_reads_deleted(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"name": "gone.txt", "mode": 0o100644, "size": 3, "mtime": 100}]
    )
    _write_index(repo / ".git", raw)

    verdicts = scoped_status(repo, ["gone.txt"])
    assert verdicts == {"gone.txt": "deleted"}


def test_untracked_path_reads_untracked(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([])
    _write_index(repo / ".git", raw)
    (repo / "new.txt").write_bytes(b"hi")

    verdicts = scoped_status(repo, ["new.txt"])
    assert verdicts == {"new.txt": "untracked"}


def test_same_size_rewrite_inside_the_same_mtime_second_reads_candidate(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "racy.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index([{
        "name": "racy.txt",
        "mode": 0o100644,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "mtime_nsec": st.st_mtime_ns % 1_000_000_000,
    }])
    _write_index(repo / ".git", raw)

    f.write_bytes(b"xyz")
    os.utime(f, ns=(st.st_atime_ns, (int(st.st_mtime) * 1_000_000_000) + 123_456_789))

    assert scoped_status(repo, ["racy.txt"]) == {"racy.txt": "candidate"}


def test_matching_sub_second_mtime_still_reads_clean(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "settled.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    raw = _build_index([{
        "name": "settled.txt",
        "mode": 0o100644,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "mtime_nsec": st.st_mtime_ns % 1_000_000_000,
    }])
    _write_index(repo / ".git", raw)

    assert scoped_status(repo, ["settled.txt"]) == {"settled.txt": "clean"}


def test_zero_stored_nsec_falls_back_to_the_second_compare(tmp_path):
    repo = _plain_repo(tmp_path)
    f = repo / "nonsec.txt"
    f.write_bytes(b"abc")
    st = f.stat()
    assert st.st_mtime_ns % 1_000_000_000 != 0, "fixture needs a sub-second mtime on disk"
    raw = _build_index([{
        "name": "nonsec.txt",
        "mode": 0o100644,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "mtime_nsec": 0,
    }])
    _write_index(repo / ".git", raw)

    assert scoped_status(repo, ["nonsec.txt"]) == {"nonsec.txt": "clean"}


def test_scoped_walk_returns_only_wanted_and_matches_the_full_walk(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([
        {"name": f"f{i}.txt", "mode": 0o100644, "size": i, "mtime": 100 + i}
        for i in range(6)
    ])
    _write_index(repo / ".git", raw)

    full = parse_index_identity(repo)
    scoped = parse_index_identity(repo, wanted=["f1.txt", "f4.txt", "absent.txt"])

    assert set(scoped) == {"f1.txt", "f4.txt"}
    assert all(scoped[k] == full[k] for k in scoped), "a scoped value must equal its full-walk value"


def test_scoped_walk_stops_once_every_wanted_path_is_found(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([
        {"name": f"f{i}.txt", "mode": 0o100644, "size": i, "mtime": 100 + i}
        for i in range(6)
    ])
    corrupt = _SIGNATURE + struct.pack(">II", 2, 99) + raw[12:]
    _write_index(repo / ".git", corrupt)

    with pytest.raises(IndexParseError):
        parse_index_identity(repo)

    assert set(parse_index_identity(repo, wanted=["f0.txt"])) == {"f0.txt"}


def test_scoped_walk_sha_is_the_entry_sha(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index([{"name": "a.txt", "mode": 0o100644, "size": 3, "mtime": 100}])
    _write_index(repo / ".git", raw)

    assert parse_index_identity(repo)["a.txt"].sha == _ZERO_SHA.hex()


def test_absent_index_file_returns_empty(tmp_path):
    repo = _plain_repo(tmp_path)
    assert parse_index_stat(repo) == {}


def test_bad_signature_raises(tmp_path):
    repo = _plain_repo(tmp_path)
    _write_index(repo / ".git", b"NOPE" + struct.pack(">II", 2, 0) + b"\x00" * 20)

    with pytest.raises(IndexParseError):
        parse_index_stat(repo)


def test_unmerged_entry_never_reads_as_staged(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"name": "conflicted.txt", "mode": 0o100644, "size": 3, "mtime": 100, "stage": 2}]
    )
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexParseError):
        parse_index_identity(repo)

    with pytest.raises(IndexParseError):
        parse_index_identity(repo, wanted=["conflicted.txt"])


def test_unmerged_out_collects_instead_of_raising(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [
            {"name": "a.txt", "mode": 0o100644, "size": 3, "mtime": 100},
            {"name": "conflicted.txt", "mode": 0o100644, "size": 3, "mtime": 100, "stage": 1},
            {"name": "conflicted.txt", "mode": 0o100644, "size": 3, "mtime": 100, "stage": 2},
        ]
    )
    _write_index(repo / ".git", raw)

    seen: set = set()
    got = parse_index_identity(repo, wanted=["a.txt", "conflicted.txt"], unmerged_out=seen)
    assert seen == {"conflicted.txt"}
    assert list(got) == ["a.txt"]

    with pytest.raises(IndexParseError):
        parse_index_identity(repo, wanted=["conflicted.txt"])


_PREFIX_NAMES = [
    "a.b",
    "a/one.txt",
    "a/sub/two.txt",
    "ab/three.txt",
    "b/four.txt",
    "c.txt",
    "d/five.txt",
    "z.txt",
]


def _prefix_repo(tmp_path, names=_PREFIX_NAMES):
    repo = _plain_repo(tmp_path)
    ordered = sorted(names, key=lambda n: n.encode())
    entries = [
        {"name": n, "mode": 0o100644, "size": i + 1, "mtime": 100}
        for i, n in enumerate(ordered)
    ]
    _write_index(repo / ".git", _build_index(entries))
    return repo


def _expected(full, prefixes):
    out = {}
    for name, ident in full.items():
        for p in prefixes:
            if p == "" or name == p or name.startswith(p.rstrip("/") + "/"):
                out[name] = ident
                break
    return out


@pytest.mark.parametrize(
    "prefixes",
    [
        ["a"],
        ["b"],
        ["a", "d"],
        ["a/sub"],
        ["ab"],
        ["c.txt"],
        ["z.txt"],
        ["nothere"],
        ["a/"],
        [""],
    ],
)
def test_prefixes_select_exactly_the_entries_at_or_under_each(tmp_path, prefixes):
    repo = _prefix_repo(tmp_path)
    full = parse_index_identity(repo)

    assert parse_index_identity(repo, prefixes=prefixes) == _expected(full, prefixes)


def test_prefix_does_not_select_sibling_names_sharing_the_leading_bytes(tmp_path):
    repo = _prefix_repo(tmp_path)

    got = parse_index_identity(repo, prefixes=["a"])

    assert set(got) == {"a/one.txt", "a/sub/two.txt"}


def test_prefixes_union_with_wanted(tmp_path):
    repo = _prefix_repo(tmp_path)

    got = parse_index_identity(repo, wanted=["z.txt"], prefixes=["b"])

    assert set(got) == {"b/four.txt", "z.txt"}


def test_empty_prefixes_and_empty_wanted_return_nothing(tmp_path):
    repo = _prefix_repo(tmp_path)

    assert parse_index_identity(repo, prefixes=[]) == {}


def test_prefix_walk_returns_past_the_last_range_without_reading_the_tail(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = bytearray(
        _build_index(
            [
                {"name": "a/x.txt", "mode": 0o100644, "size": 1, "mtime": 1},
                {"name": "b/y.txt", "mode": 0o100644, "size": 2, "mtime": 2},
            ]
        )
    )
    raw[8:12] = struct.pack(">I", 3)
    _write_index(repo / ".git", bytes(raw))

    assert set(parse_index_identity(repo, prefixes=["a"])) == {"a/x.txt"}
    with pytest.raises(IndexParseError):
        parse_index_identity(repo)


def test_default_values_are_plain_identities_without_flag_fields(tmp_path):
    repo = _prefix_repo(tmp_path)

    for ident in parse_index_identity(repo, prefixes=["a"]).values():
        assert type(ident) is IndexIdentity
        assert len(ident) == 5


def test_with_flags_surfaces_assume_valid_skip_worktree_and_intent_to_add(tmp_path):
    repo = _plain_repo(tmp_path)
    entries = [
        {"name": "av.txt", "mode": 0o100644, "size": 1, "mtime": 1, "assume_valid": True},
        {"name": "clean.txt", "mode": 0o100644, "size": 1, "mtime": 1},
        {"name": "ita.txt", "mode": 0o100644, "size": 1, "mtime": 1, "extended": True, "xflags": 0x2000},
        {"name": "sw.txt", "mode": 0o100644, "size": 1, "mtime": 1, "extended": True, "xflags": 0x4000},
    ]
    _write_index(repo / ".git", _build_index(entries, version=3))

    got = parse_index_identity(repo, prefixes=[""], with_flags=True)

    assert got["av.txt"].assume_valid and not got["av.txt"].skip_worktree
    assert not any(
        (got["clean.txt"].assume_valid, got["clean.txt"].skip_worktree, got["clean.txt"].intent_to_add)
    )
    assert got["ita.txt"].intent_to_add and not got["ita.txt"].skip_worktree
    assert got["sw.txt"].skip_worktree and not got["sw.txt"].assume_valid
    assert all(type(v) is IndexFlaggedIdentity for v in got.values())


def test_with_flags_without_scope_keeps_the_wanted_early_exit(tmp_path):
    repo = _prefix_repo(tmp_path)

    got = parse_index_identity(repo, wanted=["a.b"], with_flags=True)

    assert set(got) == {"a.b"}


def test_unmerged_entry_in_a_prefix_scope_raises(tmp_path):
    repo = _plain_repo(tmp_path)
    raw = _build_index(
        [{"name": "d/c.txt", "mode": 0o100644, "size": 3, "mtime": 100, "stage": 2}]
    )
    _write_index(repo / ".git", raw)

    with pytest.raises(IndexParseError):
        parse_index_identity(repo, prefixes=["d"])
    assert parse_index_identity(repo, prefixes=["other"]) == {}
