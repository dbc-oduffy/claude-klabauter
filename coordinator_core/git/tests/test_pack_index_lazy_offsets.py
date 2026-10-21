"""`_PackIndex.offsets` decodes lazily: equal to an eager decode, large-offset
table included, and one `_pack_index_find` decodes at most one offset."""

from __future__ import annotations

import hashlib
import struct
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_objects import _pack_index_find, _parse_pack_index_uncached
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _eager_offsets(data: bytes) -> list[int]:
    count = struct.unpack(">256I", data[8:1032])[255]
    pos = 1032 + count * 24
    raw = struct.unpack(f">{count}I", data[pos : pos + count * 4])
    pos += count * 4
    large_n = sum(1 for o in raw if o & 0x80000000)
    large = struct.unpack(f">{large_n}Q", data[pos : pos + large_n * 8]) if large_n else ()
    return [large[o & 0x7FFFFFFF] if o & 0x80000000 else o for o in raw]


def _real_idx(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for n in range(40):
        (repo / f"f{n}.txt").write_text(f"content {n}\n" * (n + 1), encoding="utf-8")
    run = lambda *a, **k: subprocess.run(  # noqa: E731
        ["git", *a], cwd=repo, check=True, capture_output=True, stdin=subprocess.DEVNULL,
        **no_console_creationflags(), **k,
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-qm", "seed")
    run("repack", "-ad", "-q")
    return next((repo / ".git" / "objects" / "pack").glob("*.idx"))


def _synthetic_idx(path: Path, offsets: list[int]) -> Path:
    n = len(offsets)
    shas = sorted(hashlib.sha1(str(i).encode()).digest() for i in range(n))
    fanout = [sum(1 for s in shas if s[0] <= b) for b in range(256)]
    small, large = [], []
    for o in offsets:
        if o >= 0x80000000:
            small.append(0x80000000 | len(large))
            large.append(o)
        else:
            small.append(o)
    body = (
        b"\xfftOc" + struct.pack(">I", 2) + struct.pack(">256I", *fanout) + b"".join(shas)
        + b"\x00" * (4 * n) + struct.pack(f">{n}I", *small) + struct.pack(f">{len(large)}Q", *large)
        + b"\x00" * 40
    )
    path.write_bytes(body)
    return path


def test_lazy_offsets_equal_eager_decode_on_real_pack(tmp_path: Path) -> None:
    idx = _real_idx(tmp_path)
    pidx = _parse_pack_index_uncached(idx)
    assert pidx is not None
    eager = _eager_offsets(idx.read_bytes())
    assert len(pidx.offsets) == len(eager)
    assert [pidx.offsets[i] for i in range(len(eager))] == eager
    assert min(pidx.offsets) == min(eager)
    assert list(pidx.offsets) == eager
    with pytest.raises(IndexError):
        pidx.offsets[len(eager)]


def test_large_offset_table_is_decoded(tmp_path: Path) -> None:
    wanted = [5, 0x80000000 + 7, 123, 0x1_0000_0000 + 9, 0x7FFFFFFF]
    idx = _synthetic_idx(tmp_path / "x.idx", wanted)
    pidx = _parse_pack_index_uncached(idx)
    assert pidx is not None
    assert list(pidx.offsets) == wanted == _eager_offsets(idx.read_bytes())
    assert len(pidx.offsets) == 5
    assert pidx.offsets[-1] == 0x7FFFFFFF


def test_find_decodes_at_most_one_offset(tmp_path: Path, monkeypatch) -> None:
    idx = _real_idx(tmp_path)
    pidx = _parse_pack_index_uncached(idx)
    assert pidx is not None
    calls: list[int] = []
    real = git_objects._LazyOffsets.__getitem__

    def counting(self, i):
        calls.append(i)
        return real(self, i)

    monkeypatch.setattr(git_objects._LazyOffsets, "__getitem__", counting)
    sha = pidx.shas[17 * 20 : 17 * 20 + 20].hex()
    assert _pack_index_find(pidx, sha) == _eager_offsets(idx.read_bytes())[17]
    assert len(calls) == 1
    calls.clear()
    assert _pack_index_find(pidx, "00" * 20) is None
    assert calls == []
