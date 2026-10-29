"""Pack lookup answers identically with no MIDX, a stale/torn MIDX, and packs newer than the MIDX,
and never walks the packs a MIDX covers."""

from __future__ import annotations

import hashlib
import struct
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
def _clean():
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


def test_no_midx_small_and_large_uncovered_sets(tmp_path):
    common = tmp_path / "small"
    _assert_all(common, _packs(common, 3))
    common = tmp_path / "large"
    _assert_all(common, _packs(common, go._UNCOVERED_LAZY_MAX + 12))


def test_midx_plus_newer_packs_answer_identically(tmp_path):
    want = _packs(tmp_path, 12)
    _write_midx(tmp_path, _idx_names(tmp_path))
    want.update(_packs(tmp_path, 3, start=100))
    _assert_all(tmp_path, want)
    want.update(_packs(tmp_path, go._UNCOVERED_LAZY_MAX + 5, start=200))
    _assert_all(tmp_path, want)


def test_covered_packs_are_never_parsed(tmp_path, monkeypatch):
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


def test_merged_table_is_built_once_for_many_uncovered_packs(tmp_path, monkeypatch):
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


def test_torn_midx_falls_back(tmp_path):
    want = _packs(tmp_path, 6)
    _write_midx(tmp_path, _idx_names(tmp_path))
    midx = tmp_path / "objects" / "pack" / "multi-pack-index"
    full = midx.read_bytes()
    for torn in (full[: len(full) // 2], full[:10], b"", b"MIDX" + b"\x00" * 60, full[:-1] + b"\x00" + b"x"):
        midx.write_bytes(torn)
        _assert_all(tmp_path, want)


def test_stale_midx_naming_a_vanished_pack(tmp_path):
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


def test_midx_large_offset_chunk(tmp_path):
    want = _packs(tmp_path, 4)
    sha = next(iter(want))
    _write_midx(tmp_path, _idx_names(tmp_path), large_for=sha)
    _assert_all(tmp_path, want)


def test_midx_hit_in_pack_without_parsing_idx_reads_bytes(tmp_path):
    want = _packs(tmp_path, 5)
    _write_midx(tmp_path, _idx_names(tmp_path))
    midx = go._parse_midx(tmp_path)
    assert midx is not None and len(midx.names) == 5
    sha = next(iter(want))
    assert go._midx_find(midx, bytes.fromhex(sha)) is not None
    assert go._midx_find(midx, bytes.fromhex("ab" * 20)) is None
