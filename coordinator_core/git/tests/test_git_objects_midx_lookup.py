"""Pack lookup answers identically with no MIDX, a stale/torn MIDX, and packs newer than the MIDX,
and never walks the packs a MIDX covers."""

from __future__ import annotations

import contextlib
import hashlib
import struct
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go


def _reset() -> None:
    for d in (
        go._PACK_LISTING_CACHE,
        go._PACK_INDEXES_BY_LISTING,
        go._LOOKUP_SOURCES,
        go._LAST_HIT_PACK,
        go._MIDX_CACHE,
        go._OBJECT_CACHE,
    ):
        d.clear()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    # The fixtures' "common dir" is a bare tmp dir, not a repo; the writer lock resolves a repo.
    monkeypatch.setattr(go.object_store_lock, "hold", lambda *a, **k: contextlib.nullcontext())
    _reset()
    yield
    _reset()


def _make_pack(common: Path, n: int) -> tuple[str, bytes]:
    payload = f"blob-{n}".encode()
    sha = hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()
    go._write_pack(common, {sha: (b"blob", payload)})
    return sha, payload


def _packs(common: Path, count: int, start: int = 0) -> dict[str, bytes]:
    return dict(_make_pack(common, n) for n in range(start, start + count))


def _idx_names(common: Path) -> list[str]:
    return sorted(p.name for p in (common / "objects" / "pack").glob("*.idx"))


def _write_midx(common: Path, names: list[str], *, large_for: str | None = None) -> None:
    pack_dir = common / "objects" / "pack"
    entries = []
    for pid, name in enumerate(names):
        pidx = go._parse_pack_index_uncached(pack_dir / name)
        for j in range(len(pidx.shas) // 20):
            entries.append((pidx.shas[j * 20 : j * 20 + 20], pid, pidx.offsets[j]))
    entries.sort(key=lambda e: (e[0], e[1]))
    pnam = b"".join(n.encode() + b"\x00" for n in names)
    pnam += b"\x00" * (-len(pnam) % 4)
    fan = [0] * 256
    for sha, _, _ in entries:
        fan[sha[0]] += 1
    run, oidf = 0, b""
    for c in fan:
        run += c
        oidf += struct.pack(">I", run)
    oidl = b"".join(e[0] for e in entries)
    ooff, loff = b"", b""
    for sha, pid, off in entries:
        if large_for is not None and sha.hex() == large_for:
            ooff += struct.pack(">II", pid, 0x80000000 | (len(loff) // 8))
            loff += struct.pack(">Q", off)
        else:
            ooff += struct.pack(">II", pid, off)
    chunks = [(b"PNAM", pnam), (b"OIDF", oidf), (b"OIDL", oidl), (b"OOFF", ooff)]
    if loff:
        chunks.append((b"LOFF", loff))
    pos = 12 + 12 * (len(chunks) + 1)
    table = b""
    for cid, body in chunks:
        table += struct.pack(">4sQ", cid, pos)
        pos += len(body)
    table += struct.pack(">4sQ", b"\x00\x00\x00\x00", pos)
    body = b"MIDX" + bytes([1, 1, len(chunks), 0]) + struct.pack(">I", len(names)) + table
    body += b"".join(c[1] for c in chunks)
    body += hashlib.sha1(body).digest()
    (pack_dir / "multi-pack-index").write_bytes(body)


def _assert_all(common: Path, want: dict[str, bytes]) -> None:
    _reset()
    for sha, payload in want.items():
        assert go.packed_contains(common, sha)
        assert go.read_object(common, sha) == ("blob", payload)
    assert not go.packed_contains(common, "ab" * 20)
    assert go.read_object(common, "cd" * 20) is None
    assert not go.packed_contains(common, "not-hex")


def test_no_midx_small_and_large_uncovered_sets(tmp_path: Path) -> None:
    common = tmp_path / "small"
    _assert_all(common, _packs(common, 3))
    common = tmp_path / "large"
    _assert_all(common, _packs(common, go._UNCOVERED_LAZY_MAX + 12))


def test_midx_plus_newer_packs_answer_identically(tmp_path: Path) -> None:
    want = _packs(tmp_path, 12)
    _write_midx(tmp_path, _idx_names(tmp_path))
    want.update(_packs(tmp_path, 3, start=100))
    _assert_all(tmp_path, want)
    want.update(_packs(tmp_path, go._UNCOVERED_LAZY_MAX + 5, start=200))
    _assert_all(tmp_path, want)


def test_covered_packs_are_never_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    want = _packs(tmp_path, 10)
    _write_midx(tmp_path, _idx_names(tmp_path))
    _reset()
    parsed: list[str] = []
    real = go._parse_pack_index
    monkeypatch.setattr(go, "_parse_pack_index", lambda p: parsed.append(p.name) or real(p))
    for sha, payload in want.items():
        assert go.read_object(tmp_path, sha) == ("blob", payload)
    assert parsed == []
    assert not go.packed_contains(tmp_path, "ab" * 20)
    assert parsed == []


def test_merged_table_is_built_once_for_many_uncovered_packs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    n = go._UNCOVERED_LAZY_MAX + 10
    want = _packs(tmp_path, n)
    _reset()
    parsed: list[str] = []
    real = go._parse_pack_index
    monkeypatch.setattr(go, "_parse_pack_index", lambda p: parsed.append(p.name) or real(p))
    assert not go.packed_contains(tmp_path, "ab" * 20)
    assert len(parsed) == n
    for sha in want:
        assert go.packed_contains(tmp_path, sha)
    assert not go.packed_contains(tmp_path, "cd" * 20)
    assert len(parsed) == n


def test_torn_midx_falls_back(tmp_path: Path) -> None:
    want = _packs(tmp_path, 6)
    _write_midx(tmp_path, _idx_names(tmp_path))
    midx = tmp_path / "objects" / "pack" / "multi-pack-index"
    full = midx.read_bytes()
    for torn in (full[: len(full) // 2], full[:10], b"", b"MIDX" + b"\x00" * 60, full[:-1] + b"\x00" + b"x"):
        midx.write_bytes(torn)
        _assert_all(tmp_path, want)


def test_stale_midx_naming_a_vanished_pack(tmp_path: Path) -> None:
    want = _packs(tmp_path, 6)
    names = _idx_names(tmp_path)
    _write_midx(tmp_path, names)
    gone = names[2]
    gone_sha = None
    pack_dir = tmp_path / "objects" / "pack"
    pidx = go._parse_pack_index_uncached(pack_dir / gone)
    gone_sha = pidx.shas[:20].hex()
    (pack_dir / gone).unlink()
    (pack_dir / gone.replace(".idx", ".pack")).unlink()
    del want[gone_sha]
    _assert_all(tmp_path, want)
    assert not go.packed_contains(tmp_path, gone_sha)


def test_vanished_midx_pack_is_absent_for_that_pack_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    want = _packs(tmp_path, 8)
    names = _idx_names(tmp_path)
    _write_midx(tmp_path, names)
    pack_dir = tmp_path / "objects" / "pack"
    gone = names[3]
    gone_sha = go._parse_pack_index_uncached(pack_dir / gone).shas[:20].hex()
    (pack_dir / gone).unlink()
    (pack_dir / gone.replace(".idx", ".pack")).unlink()
    del want[gone_sha]
    newer = _packs(tmp_path, 3, start=100)
    want.update(newer)
    covered_survivors = set(names) - {gone}
    _reset()
    parsed: list[str] = []
    real = go._parse_pack_index
    monkeypatch.setattr(go, "_parse_pack_index", lambda p: parsed.append(p.name) or real(p))
    for sha, payload in want.items():
        assert go.read_object(tmp_path, sha) == ("blob", payload)
    assert not covered_survivors & set(parsed)
    assert set(parsed) <= set(_idx_names(tmp_path)) - covered_survivors
    src = go._LOOKUP_SOURCES[str(tmp_path)]
    assert len(src.uncovered) == len(newer) <= go._UNCOVERED_LAZY_MAX
    assert src._merged is None
    assert not go.packed_contains(tmp_path, gone_sha)
    assert not go.packed_contains(tmp_path, "ab" * 20)
    assert go.read_object(tmp_path, gone_sha) is None
    assert src._merged is None


def _blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()


def _gone_pack_store(common: Path) -> tuple[str, str, str, bytes, int]:
    """5 plain packs, pack G (holds X and Z) and pack S (holds X and Y), all under one MIDX; G is
    then deleted. Returns `(x_sha, z_sha, y_sha, y_payload, G's pack id in the MIDX)`."""
    _packs(common, 5)
    x, z, y = b"shared-x", b"only-in-gone-z", b"only-in-survivor-y"
    plain = set(_idx_names(common))
    go._write_pack(common, {_blob_sha(x): (b"blob", x), _blob_sha(z): (b"blob", z)})
    (gone_idx,) = set(_idx_names(common)) - plain
    go._write_pack(common, {_blob_sha(x): (b"blob", x), _blob_sha(y): (b"blob", y)})
    names = _idx_names(common)
    _write_midx(common, names)
    pack_dir = common / "objects" / "pack"
    (pack_dir / gone_idx).unlink()
    (pack_dir / gone_idx.replace(".idx", ".pack")).unlink()
    return _blob_sha(x), _blob_sha(z), _blob_sha(y), y, names.index(gone_idx)


def test_gone_pack_midx_hit_resolves_from_a_surviving_covered_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x_sha, _, y_sha, y_payload, gone_pid = _gone_pack_store(tmp_path)
    real_find = go._midx_find
    monkeypatch.setattr(
        go, "_midx_find", lambda midx, target: (gone_pid, 0) if target.hex() == x_sha else real_find(midx, target)
    )
    _reset()
    got = go.read_object(tmp_path, x_sha)
    assert got == ("blob", b"shared-x")
    assert go.read_object(tmp_path, y_sha) == ("blob", y_payload)
    assert go._LOOKUP_SOURCES[str(tmp_path)]._merged is None


def test_sha_only_the_gone_pack_held_is_absent_and_survivors_parse_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, z_sha, _, _, _ = _gone_pack_store(tmp_path)
    survivors = set(_idx_names(tmp_path))
    _reset()
    parsed: list[str] = []
    real = go._parse_pack_index
    monkeypatch.setattr(go, "_parse_pack_index", lambda p: parsed.append(p.name) or real(p))
    for _ in range(5):
        assert go.read_object(tmp_path, z_sha) is None
        assert not go.packed_contains(tmp_path, z_sha)
    assert len(parsed) == len(set(parsed))
    assert set(parsed) <= survivors
    assert go._LOOKUP_SOURCES[str(tmp_path)]._merged is None


def _fold_store(common: Path, *, midx_names: "list[str] | None" = None) -> tuple[dict[str, bytes], Path, str, str, bytes]:
    """3 packs under one MIDX, the first marked as the fold tail. Returns
    `(want, pack_dir, tail, new_sha, new_payload)`."""
    want = _packs(common, 3)
    names = _idx_names(common)
    _write_midx(common, names if midx_names is None else midx_names)
    pack_dir = common / "objects" / "pack"
    tail = names[0][len("pack-"):-len(".idx")]
    go._write_tail_marker(pack_dir, tail, [])
    new_payload = b"fresh-after-fold"
    return want, pack_dir, tail, _blob_sha(new_payload), new_payload


def test_fold_of_a_midx_covered_pack_drops_the_midx_and_lookups_stay_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    want, pack_dir, tail, new_sha, new_payload = _fold_store(tmp_path)
    (pack_dir / "multi-pack-index-abc.bitmap").write_bytes(b"x")
    (pack_dir / "multi-pack-index-abc.rev").write_bytes(b"x")
    _reset()
    assert go.read_object(tmp_path, next(iter(want))) is not None
    spawned: list = []

    def _no_spawn(*a, **k):
        spawned.append(a)
        raise AssertionError("git spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    go._flush_pack_locked(tmp_path, {new_sha: (b"blob", new_payload)}, True)

    assert spawned == []
    assert not (pack_dir / f"pack-{tail}.idx").exists()
    assert not (pack_dir / "multi-pack-index").exists()
    assert not list(pack_dir.glob("multi-pack-index-*"))
    from coordinator_core.git import store_hygiene

    assert not store_hygiene.midx_names_missing_pack(tmp_path)
    want[new_sha] = new_payload
    opened: list[str] = []
    real = go._parse_pack_index
    monkeypatch.setattr(go, "_parse_pack_index", lambda p: opened.append(p.name) or real(p))
    _assert_all(tmp_path, want)
    assert f"pack-{tail}.idx" not in opened


def test_fold_keeps_a_midx_that_names_no_doomed_pack(tmp_path: Path) -> None:
    _, pack_dir, tail, new_sha, new_payload = _fold_store(tmp_path, midx_names=[])
    other = [n for n in _idx_names(tmp_path) if tail not in n]
    _write_midx(tmp_path, other)

    go._flush_pack_locked(tmp_path, {new_sha: (b"blob", new_payload)}, True)

    assert (pack_dir / "multi-pack-index").exists()
    assert not (pack_dir / f"pack-{tail}.idx").exists()


def test_fold_keeps_every_doomed_pack_when_the_midx_cannot_be_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    want, pack_dir, tail, new_sha, new_payload = _fold_store(tmp_path)
    monkeypatch.setattr(go, "_unlink_midx_file", lambda path: False)

    go._flush_pack_locked(tmp_path, {new_sha: (b"blob", new_payload)}, True)

    assert (pack_dir / "multi-pack-index").exists()
    assert (pack_dir / f"pack-{tail}.idx").exists()
    assert (pack_dir / f"pack-{tail}.pack").exists()
    assert go._read_tail_marker(pack_dir)[1] == [tail]
    want[new_sha] = new_payload
    _assert_all(tmp_path, want)


def test_midx_large_offset_chunk(tmp_path: Path) -> None:
    want = _packs(tmp_path, 4)
    sha = next(iter(want))
    _write_midx(tmp_path, _idx_names(tmp_path), large_for=sha)
    _assert_all(tmp_path, want)


def test_midx_hit_in_pack_without_parsing_idx_reads_bytes(tmp_path: Path) -> None:
    want = _packs(tmp_path, 5)
    _write_midx(tmp_path, _idx_names(tmp_path))
    midx = go._parse_midx(tmp_path)
    assert midx is not None and len(midx.names) == 5
    sha = next(iter(want))
    assert go._midx_find(midx, bytes.fromhex(sha)) is not None
    assert go._midx_find(midx, bytes.fromhex("ab" * 20)) is None
