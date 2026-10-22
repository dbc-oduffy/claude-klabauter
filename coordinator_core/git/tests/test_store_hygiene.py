"""Pins `store_hygiene.status` on tmp_path fixtures and the module's CLI."""

import json
import os
import struct
import threading
import time

import pytest

from coordinator_core.git import store_hygiene as sh


def _store(tmp_path, packs=0):
    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    for i in range(packs):
        (pack_dir / f"pack-{i:040x}.idx").write_bytes(b"x")
        (pack_dir / f"pack-{i:040x}.pack").write_bytes(b"x")
    return pack_dir


def _midx(pack_dir, n):
    header = b"MIDX" + bytes([1, 1, 0, 0]) + struct.pack(">I", n)
    (pack_dir / "multi-pack-index").write_bytes(header + b"\0" * 32)


def test_no_midx_is_none(tmp_path):
    _store(tmp_path, packs=3)
    s = sh.status(tmp_path)
    assert (s.pack_count, s.midx_pack_count) == (3, None)


def test_midx_covering_k_of_n(tmp_path):
    pack_dir = _store(tmp_path, packs=5)
    _midx(pack_dir, 2)
    s = sh.status(tmp_path)
    assert (s.pack_count, s.midx_pack_count) == (5, 2)


def test_torn_or_foreign_header_is_none(tmp_path):
    pack_dir = _store(tmp_path, packs=2)
    (pack_dir / "multi-pack-index").write_bytes(b"MIDX\x01\x01")
    assert sh.status(tmp_path).midx_pack_count is None
    (pack_dir / "multi-pack-index").write_bytes(b"NOPE" + b"\0" * 20)
    assert sh.status(tmp_path).midx_pack_count is None


def test_loose_estimate_scales_one_sampled_fanout(tmp_path):
    _store(tmp_path)
    fan = tmp_path / "objects" / "00"
    fan.mkdir()
    for i in range(3):
        (fan / f"{i:038x}").write_bytes(b"x")
    (fan / f"{9:038x}.tmp123").write_bytes(b"x")
    s = sh.status(tmp_path)
    assert (s.loose_dirs_sampled, s.loose_estimate) == (1, 3 * 256)


def test_missing_store_is_zeroes(tmp_path):
    assert sh.status(tmp_path / "absent") == sh.StoreHygiene(0, None, 1, 0)


def test_cli_prints_tuple_as_json(tmp_path, capsys):
    pack_dir = _store(tmp_path / ".git", packs=2)
    _midx(pack_dir, 1)
    assert sh.main(["--repo", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pack_count"] == 2 and out["midx_pack_count"] == 1
    assert sh.main([]) == 2


def _repo(tmp_path, packs=0, fresh_liveness=True):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    repo = tmp_path / "repo"
    common = repo / ".git"
    pack_dir = _store(common, packs=packs)
    if packs:
        _midx(pack_dir, packs)
    if fresh_liveness:
        hl.stamp_liveness(str(repo), hl.GIT_MAINTENANCE)
    return repo, common


@pytest.fixture
def spawns(monkeypatch):
    from coordinator_core.ops.ceremony import detached_spawn

    calls = []
    monkeypatch.setattr(
        detached_spawn, "spawn_detached", lambda *a, **k: calls.append((a, k)) or True
    )
    return calls


def test_not_stale_does_not_spawn(tmp_path, spawns):
    repo, common = _repo(tmp_path, packs=3)
    assert sh.maybe_refresh(repo, common) is False
    assert spawns == []
    assert not (common / "coordinator" / "store-hygiene.claim").exists()


def test_midx_lag_fires_once_per_claim_interval(tmp_path, spawns):
    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    assert sh.maybe_refresh(repo, common) is True
    assert sh.maybe_refresh(repo, common) is False
    assert len(spawns) == 1
    assert spawns[0][0][2] == ["--repo", str(repo), "--daily"]


def test_loose_growth_alone_fires(tmp_path, spawns):
    repo, common = _repo(tmp_path, packs=1)
    fan = common / "objects" / "00"
    fan.mkdir()
    for i in range(3):
        (fan / f"{i:038x}").write_bytes(b"x")
    assert sh.maybe_refresh(repo, common) is True
    assert len(spawns) == 1


def test_stale_liveness_fires(tmp_path, spawns):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    repo, common = _repo(tmp_path, packs=1)
    live = hl.liveness_path(str(repo))
    live.write_text(json.dumps({hl.GIT_MAINTENANCE: "2020-01-01T00:00:00Z"}), encoding="utf-8")
    assert sh.maybe_refresh(repo, common) is True


def test_old_claim_is_reclaimed_and_young_claim_refuses(tmp_path, spawns):
    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    assert sh.maybe_refresh(repo, common) is True
    claim = common / "coordinator" / "store-hygiene.claim"
    old = time.time() - sh.CLAIM_INTERVAL_S - 5
    os.utime(claim, (old, old))
    assert sh.maybe_refresh(repo, common) is True
    assert sh.maybe_refresh(repo, common) is False
    assert len(spawns) == 2


def test_concurrent_calls_produce_one_firing(tmp_path, spawns):
    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        sh.maybe_refresh(repo, common)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(spawns) == 1


def test_declined_spawn_releases_the_claim(tmp_path, monkeypatch):
    from coordinator_core.ops.ceremony import detached_spawn

    monkeypatch.setattr(detached_spawn, "spawn_detached", lambda *a, **k: False)
    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    assert sh.maybe_refresh(repo, common) is False
    assert not (common / "coordinator" / "store-hygiene.claim").exists()


def test_spawn_exception_never_raises(tmp_path, monkeypatch):
    from coordinator_core.ops.ceremony import detached_spawn

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(detached_spawn, "spawn_detached", boom)
    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    assert sh.maybe_refresh(repo, common) is False
    assert "CHILD FAILED" in (repo / "state" / "housekeeping-failures.log").read_text()
