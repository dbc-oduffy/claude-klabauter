"""`read_object_header` returns (type, size) from the entry header, never the payload."""

from __future__ import annotations

import zlib

import pytest

from coordinator_core.benchmarks import object_read_stall_profile as bench
from coordinator_core.git import git_objects as go


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return bench.build_fixtures(go, tmp_path_factory.mktemp("read-object-header"))


@pytest.fixture(autouse=True)
def _cold():
    bench.clear_caches(go)
    yield
    bench.clear_caches(go)


def _ofs_offset(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        n -= 1
        out.insert(0, 0x80 | (n & 0x7F))
        n >>= 7
    return bytes(out)


@pytest.fixture()
def ofs_repo(tmp_path):
    """A pack whose second entry is an OFS_DELTA on the first (base sorts first by sha)."""
    filler = bytes((i * 11 + 5) & 0xFF for i in range(50_000))
    tail = b"ofs-tail\n"
    for n in range(256):
        base_payload = filler + bytes([n])
        delta_payload = base_payload + tail
        base_sha = bench.blob_sha(base_payload)
        delta_sha = bench.blob_sha(delta_payload)
        if base_sha < delta_sha:
            break
    else:
        raise AssertionError("no ordered sha pair found")
    base_entry = go._pack_entry(b"blob", base_payload)
    delta = bench._copy_all_then_insert(len(base_payload), tail)
    delta_entry = bench._entry_header(6, len(delta)) + _ofs_offset(len(base_entry)) + zlib.compress(delta)
    repo = tmp_path / "ofs.git"
    bench._place(
        go,
        repo,
        [
            (bytes.fromhex(base_sha), base_entry, zlib.crc32(base_entry)),
            (bytes.fromhex(delta_sha), delta_entry, zlib.crc32(delta_entry)),
        ],
    )
    return repo, base_sha, delta_sha, base_payload, delta_payload


def test_loose_header_without_inflating_the_payload(fx) -> None:
    with bench.InflateSpy() as spy:
        got = go.read_object_header(fx.common_dir, fx.big_sha)
    assert got == ("blob", fx.big_size)
    assert spy.bytes_out <= 64


def test_non_delta_pack_header_inflates_nothing(fx) -> None:
    with bench.InflateSpy() as spy:
        got = go.read_object_header(fx.common_dir, fx.pack_sha)
    assert got == ("blob", len(fx.pack_payload))
    assert spy.bytes_out == 0


def test_ref_delta_header_reads_neither_delta_nor_base_payload(fx) -> None:
    with bench.InflateSpy() as spy:
        got = go.read_object_header(fx.common_dir, fx.delta_sha)
    assert got == ("blob", len(fx.delta_payload))
    assert spy.bytes_out <= 64 * 2


def test_ofs_delta_header_reads_neither_delta_nor_base_payload(ofs_repo) -> None:
    repo, _base_sha, delta_sha, base_payload, delta_payload = ofs_repo
    with bench.InflateSpy() as spy:
        got = go.read_object_header(repo, delta_sha)
    assert got == ("blob", len(delta_payload))
    assert spy.bytes_out <= 64
    assert go.read_object(repo, delta_sha) == ("blob", delta_payload)


def test_header_matches_read_object_for_every_fixture_object(fx) -> None:
    for sha in (fx.pack_sha, fx.delta_sha, fx.big_sha, fx.base_sha):
        kind, payload = go.read_object(fx.common_dir, sha)
        bench.clear_caches(go)
        assert go.read_object_header(fx.common_dir, sha) == (kind, len(payload))
        bench.clear_caches(go)


def test_absent_object_is_none(fx) -> None:
    assert go.read_object_header(fx.common_dir, "0" * 40) is None


def test_uppercase_sha_is_accepted(fx) -> None:
    assert go.read_object_header(fx.common_dir, fx.pack_sha.upper()) == ("blob", len(fx.pack_payload))


def test_object_cache_hit_answers_without_touching_disk(fx, monkeypatch) -> None:
    go.read_object(fx.common_dir, fx.pack_sha)
    monkeypatch.setattr(go, "_open_packed", lambda *a, **k: pytest.fail("cache hit reread the pack"))
    assert go.read_object_header(fx.common_dir, fx.pack_sha) == ("blob", len(fx.pack_payload))


def test_stalled_header_read_raises_deadline_not_none(fx, monkeypatch) -> None:
    monkeypatch.setattr(go, "PACK_READ_DEADLINE_SECS", 0.3)
    with bench.Stall(go, hold=3.0, pack_path=fx.pack_path) as stall:
        with pytest.raises(go.PackReadDeadline):
            go.read_object_header(fx.common_dir, fx.pack_sha)
        assert stall.entered.is_set()
