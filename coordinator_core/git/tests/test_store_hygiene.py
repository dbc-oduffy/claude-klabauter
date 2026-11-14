"""Pins `store_hygiene.status`, the owed/orphan/MIDX probes, the pack batch and the daily child on tmp_path fixtures."""

import json
import os
import stat
import struct
import threading
import time

import pytest

from coordinator_core.git import pack_deletion_pause
from coordinator_core.git import store_hygiene as sh


@pytest.fixture(autouse=True)
def _unpaused(monkeypatch):
    """These tests pin the unlink paths themselves; the paused behaviour is pinned in test_pack_deletion_pause."""
    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)


pytestmark = pytest.mark.complete


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


def test_no_commit_route_launcher_remains():
    assert not hasattr(sh, "maybe_refresh") and not hasattr(sh, "_fire_daily")


def test_healthy_store_owes_nothing(tmp_path):
    repo, common = _repo(tmp_path, packs=3)
    assert sh._owes_maintenance(repo, common) is False


def test_midx_lag_is_owed(tmp_path):
    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    assert sh._owes_maintenance(repo, common) is True


def test_loose_growth_alone_is_owed(tmp_path):
    repo, common = _repo(tmp_path, packs=1)
    fan = common / "objects" / "00"
    fan.mkdir()
    for i in range(3):
        (fan / f"{i:038x}").write_bytes(b"x")
    assert sh._owes_maintenance(repo, common) is True


def test_stale_liveness_is_owed(tmp_path):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    repo, common = _repo(tmp_path, packs=1)
    live = hl.liveness_path(str(repo))
    live.write_text(json.dumps({hl.GIT_MAINTENANCE: "2020-01-01T00:00:00Z"}), encoding="utf-8")
    assert sh._owes_maintenance(repo, common) is True


def test_daily_child_exits_early_when_nothing_is_owed(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path, packs=3)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    monkeypatch.setattr(gm, "run_tier", lambda r, t: pytest.fail("tier ran with nothing owed"))
    assert sh._run_daily(repo) == 0


def test_daily_child_backs_off_on_a_busy_box_and_keeps_the_claim(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 1)
    claim = common / "coordinator" / "store-hygiene.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_bytes(b"")
    monkeypatch.setattr(sh, "_box_busy", lambda: True)
    monkeypatch.setattr(gm, "run_tier", lambda r, t: pytest.fail("tier ran on a busy box"))
    assert sh._run_daily(repo) == 0
    assert claim.exists()


def test_box_busy_reads_cpu_load(monkeypatch):
    from coordinator_core.telemetry import host_sampler

    monkeypatch.setattr(host_sampler, "read_load", lambda: {"cpu_load": sh.BUSY_CPU_LOAD})
    assert sh._box_busy() is True
    monkeypatch.setattr(host_sampler, "read_load", lambda: {"cpu_load": 0.1})
    assert sh._box_busy() is False
    monkeypatch.setattr(host_sampler, "read_load", lambda: {"cpu_load": None})
    assert sh._box_busy() is False


def test_old_claim_is_reclaimed_and_young_claim_refuses(tmp_path):
    repo, common = _repo(tmp_path, packs=3)
    assert sh._claim(common) is True
    claim = common / "coordinator" / "store-hygiene.claim"
    old = time.time() - sh.CLAIM_INTERVAL_S - 5
    os.utime(claim, (old, old))
    assert sh._claim(common) is True
    assert sh._claim(common) is False


def test_concurrent_claims_produce_one_winner(tmp_path):
    repo, common = _repo(tmp_path, packs=3)
    barrier = threading.Barrier(8)
    wins = []

    def go():
        barrier.wait()
        wins.append(sh._claim(common))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_release_claim_lets_the_next_claim_win(tmp_path):
    repo, common = _repo(tmp_path, packs=3)
    assert sh._claim(common) is True
    sh._release_claim(common)
    assert sh._claim(common) is True


def _common(tmp_path):
    """`<tmp>/.git` with a HEAD, enough for the object-store lock to find its repo."""
    common = tmp_path / ".git"
    common.mkdir(parents=True, exist_ok=True)
    if not (common / "HEAD").exists():
        (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return common


def _git_fixture(tmp_path, n, real_git=False):
    import hashlib
    import subprocess

    from coordinator_core.git import git_objects as go

    if real_git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    common = _common(tmp_path)
    shas = []
    for i in range(n):
        body = f"blob-{i}".encode()
        sha = hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()
        go._write_pack(common, {sha: (b"blob", body)})
        shas.append((sha, body))
    return tmp_path, common, shas


def _forget_pack_caches():
    from coordinator_core.git import git_objects as go

    go._PACK_LISTING_CACHE.clear()
    go._PACK_INDEXES_BY_LISTING.clear()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_pack_batches_consolidate_200_engine_packs_and_keep_every_object(tmp_path):
    from coordinator_core.git import git_objects as go

    _, common, shas = _git_fixture(tmp_path, 200, real_git=True)
    assert sh.status(common).pack_count == 200
    for _ in range(10):
        before = sh.status(common).pack_count
        removed = sh.consolidate_pack_batch(common)
        assert removed <= sh.CONSOLIDATE_BATCH_PACKS
        if removed == 0:
            break
        assert sh.status(common).pack_count == before - removed + 1
    assert sh.status(common).pack_count <= sh.CONSOLIDATE_MAX_PACKS
    _forget_pack_caches()
    for sha, body in shas:
        assert go.read_object(common, sha) == ("blob", body)


def test_consolidate_leaves_a_store_at_the_bound_alone(tmp_path):
    _, common, _ = _git_fixture(tmp_path, sh.CONSOLIDATE_MAX_PACKS)
    assert sh.consolidate_pack_batch(common) == 0
    assert sh.status(common).pack_count == sh.CONSOLIDATE_MAX_PACKS


def _sized_packs(common, n, nbytes):
    import hashlib

    from coordinator_core.git import git_objects as go

    shas = []
    for i in range(n):
        body = os.urandom(nbytes)
        sha = hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()
        go._write_pack(common, {sha: (b"blob", body)})
        shas.append((sha, body))
    return shas


@pytest.mark.spawns_process
def test_pack_batch_merges_packs_in_the_64kb_to_1mb_band_and_is_bounded(tmp_path):
    from coordinator_core.git import git_objects as go

    _, common, _ = _git_fixture(tmp_path, 0, real_git=True)
    n = sh.CONSOLIDATE_MAX_PACKS + 48
    shas = _sized_packs(common, n, 100 * 1024)
    removed = sh.consolidate_pack_batch(common)
    assert removed == sh.CONSOLIDATE_BATCH_PACKS
    assert sh.status(common).pack_count == n - removed + 1
    assert sh.consolidate_pack_batch(common) > 0
    _forget_pack_caches()
    for sha, body in shas:
        assert go.read_object(common, sha) == ("blob", body)


def test_pack_batch_leaves_packs_at_or_over_the_floor_alone(tmp_path):
    common = _common(tmp_path)
    _sized_packs(common, sh.CONSOLIDATE_MAX_PACKS + 3, sh.CONSOLIDATE_SIZE_FLOOR)
    assert sh.consolidate_pack_batch(common) == 0
    assert sh.status(common).pack_count == sh.CONSOLIDATE_MAX_PACKS + 3


@pytest.mark.spawns_process
def test_pack_batch_stops_at_the_byte_bound(tmp_path, monkeypatch):
    _, common, _ = _git_fixture(tmp_path, 0, real_git=True)
    n = sh.CONSOLIDATE_MAX_PACKS + 20
    _sized_packs(common, n, 200 * 1024)
    monkeypatch.setattr(sh, "CONSOLIDATE_GROUP_BYTES", 10 * 200 * 1024)
    removed = sh.consolidate_pack_batch(common)
    assert removed == 9
    assert sh.status(common).pack_count == n - 9 + 1


@pytest.mark.spawns_process
def test_pack_batch_skips_keep_packs_and_drops_the_stale_midx(tmp_path):
    _, common, _ = _git_fixture(tmp_path, sh.CONSOLIDATE_MAX_PACKS + 5, real_git=True)
    pack_dir = common / "objects" / "pack"
    kept = sorted(pack_dir.glob("*.idx"))[0]
    kept.with_suffix(".keep").write_bytes(b"")
    from coordinator_core.git.run import run_git

    assert run_git(["multi-pack-index", "write"], cwd=str(common)).returncode == 0
    assert (pack_dir / "multi-pack-index").exists()
    assert sh.consolidate_pack_batch(common) > 0
    assert kept.exists() and kept.with_suffix(".pack").exists()
    assert not (pack_dir / "multi-pack-index").exists()


def test_deferred_tier_releases_the_claim_after_one_call(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path)
    claim = common / "coordinator" / "store-hygiene.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_bytes(b"")
    calls = []

    def deferred(r, tier):
        calls.append(tier)
        return gm.MaintenanceResult(tier=tier, deferred="object-store lock is held")

    monkeypatch.setattr(gm, "run_tier", deferred)
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    assert sh._run_daily_outcome(repo) == (0, True)
    assert calls == ["daily"]
    assert not claim.exists()
    assert not hasattr(sh, "DEFER_RETRIES") and not hasattr(sh, "DEFER_RETRY_S")


def test_ran_tier_keeps_the_claim(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path)
    claim = common / "coordinator" / "store-hygiene.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_bytes(b"")
    monkeypatch.setattr(gm, "run_tier", lambda r, t: gm.MaintenanceResult(tier=t, ran=True))
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    assert sh._run_daily(repo) == 0
    assert claim.exists()


_HOLDER_CHILD = """
import sys
from pathlib import Path
from coordinator_core.git.object_store_lock import hold
with hold(Path(sys.argv[1]), wait_s=30.0, holder_label="test-holder"):
    Path(sys.argv[2]).write_text("ready")
    sys.stdin.read()
"""


@pytest.fixture()
def foreign_holder(tmp_path):
    """Start a real process holding the object-store lock of a common dir."""
    import subprocess
    import sys
    from pathlib import Path

    procs = []

    def start(common) -> None:
        ready = tmp_path / "holder-ready"
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[3])
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_CHILD, str(common), str(ready)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        procs.append(proc)
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.02)

    yield start
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=60)


def _tmp_pack_files(common, names, age_s):
    pack_dir = common / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    when = time.time() - age_s
    paths = []
    for name in names:
        p = pack_dir / name
        p.write_bytes(b"x")
        os.utime(p, (when, when))
        paths.append(p)
    return paths


_STALE_NAMES = [
    ".tmp-26264-pack-5d93aa.pack",
    ".tmp-26264-pack-5d93aa.idx",
    ".tmp-26264-pack-5d93aa.rev",
    "pack-abc.pack.tmp123",
    "pack-abc.idx.tmp123",
]


_YOUNG_S = 20 * 60
_AGED_S = sh.ORPHAN_PACK_AGE_S + 600
_TRIPLET = ["pack-aaa.pack", "pack-aaa.idx", "pack-aaa.rev"]
_UNKNOWN = ["claude-klabauter-tail", "claude-klabauter-tail", ".keepme", "pack-aaa.keep", "multi-pack-index.tmp"]


def test_sweep_removes_each_aged_orphan_class(tmp_path):
    common = _common(tmp_path)
    live = _tmp_pack_files(common, _TRIPLET, 48 * 3600)
    unknown = _tmp_pack_files(common, _UNKNOWN, 48 * 3600)
    orphans = _tmp_pack_files(
        common, ["pack-bbb.rev", "pack-ccc.pack", "pack-ccc.rev", "tmp_pack_Ab12Cd", *_STALE_NAMES], _AGED_S
    )
    assert sh.sweep_orphan_pack_files(common) == len(orphans)
    assert not any(p.exists() for p in orphans)
    assert all(p.exists() for p in live + unknown)


def test_sweep_keeps_young_orphans(tmp_path):
    common = _common(tmp_path)
    young = _tmp_pack_files(common, ["pack-bbb.rev", "pack-ccc.pack", *_STALE_NAMES], _YOUNG_S)
    assert sh.sweep_orphan_pack_files(common) == 0
    assert all(p.exists() for p in young)


def test_sweep_clears_a_read_only_orphan(tmp_path):
    common = _common(tmp_path)
    (orphan,) = _tmp_pack_files(common, ["pack-bbb.rev"], _AGED_S)
    os.chmod(orphan, stat.S_IREAD)
    assert sh.sweep_orphan_pack_files(common) == 1
    assert not orphan.exists()


def test_sweep_on_a_missing_pack_dir_is_zero(tmp_path):
    assert sh.sweep_orphan_pack_files(_common(tmp_path)) == 0


def _plant_midx(pack_dir, names):
    """A MIDX with a PNAM chunk naming `names`, in git's on-disk shape."""
    pnam = b"".join(n.encode() + b"\0" for n in names)
    pnam += b"\0" * (-len(pnam) % 4)
    table_len = 12 * 3
    header = b"MIDX" + bytes([1, 1, 2, 0]) + struct.pack(">I", len(names))
    start = len(header) + table_len
    table = b"PNAM" + struct.pack(">Q", start) + b"OIDF" + struct.pack(">Q", start + len(pnam))
    table += b"\0\0\0\0" + struct.pack(">Q", start + len(pnam) + 8)
    (pack_dir / "multi-pack-index").write_bytes(header + table + pnam + b"\0" * 8)


def test_midx_names_missing_pack_reads_the_pnam_chunk(tmp_path):
    pack_dir = _store(tmp_path, packs=3)
    present = [f"pack-{i:040x}.idx" for i in range(3)]
    _plant_midx(pack_dir, present)
    assert sh.midx_names_missing_pack(tmp_path) is False
    _plant_midx(pack_dir, [*present, "pack-" + "d" * 40 + ".idx"])
    assert sh.midx_names_missing_pack(tmp_path) is True
    (pack_dir / present[0]).unlink()
    _plant_midx(pack_dir, present)
    assert sh.midx_names_missing_pack(tmp_path) is True


def test_midx_names_missing_pack_is_false_without_a_usable_midx(tmp_path):
    pack_dir = _store(tmp_path, packs=2)
    assert sh.midx_names_missing_pack(tmp_path) is False
    _midx(pack_dir, 9)
    assert sh.midx_names_missing_pack(tmp_path) is False
    (pack_dir / "multi-pack-index").write_bytes(b"MIDX\x01\x01\x01\x00")
    assert sh.midx_names_missing_pack(tmp_path) is False
    assert sh.midx_names_missing_pack(tmp_path / "absent") is False


@pytest.fixture()
def no_spawn_no_liveness(monkeypatch):
    import subprocess

    from coordinator_core.git import run
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    def refuse(*a, **k):
        raise AssertionError("owed_by_threshold spawned or read liveness")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(run, "run_git", refuse)
    monkeypatch.setattr(hl, "liveness_status", refuse)


def test_owed_by_threshold_is_false_under_both_slacks(tmp_path, no_spawn_no_liveness):
    _, common = _repo(tmp_path, packs=3)
    fan = common / "objects" / "00"
    fan.mkdir()
    assert sh.owed_by_threshold(common) is False


def test_owed_by_threshold_is_true_past_the_uncovered_pack_slack(tmp_path, no_spawn_no_liveness):
    _, common = _repo(tmp_path, packs=sh.STALE_PACK_SLACK + 5)
    _midx(common / "objects" / "pack", 4)
    assert sh.owed_by_threshold(common) is True


def test_owed_by_threshold_is_true_past_the_loose_slack(tmp_path, no_spawn_no_liveness):
    _, common = _repo(tmp_path, packs=1)
    fan = common / "objects" / "00"
    fan.mkdir()
    for i in range(sh.STALE_LOOSE_SLACK // 256 + 1):
        (fan / f"{i:038x}").write_bytes(b"x")
    assert sh.owed_by_threshold(common) is True


def test_owed_by_threshold_is_true_for_a_midx_naming_a_missing_pack(tmp_path, no_spawn_no_liveness):
    _, common = _repo(tmp_path, packs=2)
    pack_dir = common / "objects" / "pack"
    _plant_midx(pack_dir, [f"pack-{i:040x}.idx" for i in range(2)])
    assert sh.owed_by_threshold(common) is False
    _plant_midx(pack_dir, [f"pack-{i:040x}.idx" for i in range(3)])
    assert sh.owed_by_threshold(common) is True


def test_owed_by_threshold_sees_aged_orphans_but_not_young_ones(tmp_path, no_spawn_no_liveness):
    _, common = _repo(tmp_path, packs=1)
    young = _tmp_pack_files(common, ["pack-bbb.rev"], _YOUNG_S)
    assert sh.owed_by_threshold(common) is False
    os.utime(young[0], (time.time() - _AGED_S,) * 2)
    assert sh.owed_by_threshold(common) is True


def test_owed_by_threshold_counts_small_packs_past_the_merge_bound(tmp_path, no_spawn_no_liveness):
    _, common = _repo(tmp_path, packs=sh.CONSOLIDATE_MAX_PACKS)
    assert sh.owed_by_threshold(common) is False
    pack_dir = common / "objects" / "pack"
    for i in range(sh.CONSOLIDATE_MAX_PACKS, sh.CONSOLIDATE_MAX_PACKS + 3):
        (pack_dir / f"pack-{i:040x}.idx").write_bytes(b"x")
        (pack_dir / f"pack-{i:040x}.pack").write_bytes(b"x")
    _midx(pack_dir, sh.CONSOLIDATE_MAX_PACKS + 3)
    assert sh.owed_by_threshold(common) is True


def test_owes_maintenance_is_the_threshold_or_stale_liveness(tmp_path, monkeypatch):
    repo, common = _repo(tmp_path, packs=1)
    monkeypatch.setattr(sh, "owed_by_threshold", lambda c: True)
    assert sh._owes_maintenance(repo, common) is True
    monkeypatch.setattr(sh, "owed_by_threshold", lambda c: False)
    assert sh._owes_maintenance(repo, common) is False


def test_daily_outcome_runs_the_tier_for_an_aged_orphan_on_an_otherwise_healthy_store(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path, packs=1)
    _tmp_pack_files(common, ["pack-bbb.rev"], _AGED_S)
    calls = []
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    monkeypatch.setattr(gm, "run_tier", lambda r, t: calls.append(t) or gm.MaintenanceResult(tier=t, ran=True))
    assert sh._run_daily_outcome(repo) == (0, False)
    assert calls == ["daily"]


def test_daily_outcome_does_nothing_for_young_orphans(tmp_path, monkeypatch):
    repo, common = _repo(tmp_path, packs=1)
    young = _tmp_pack_files(common, ["pack-bbb.rev", *_STALE_NAMES], _YOUNG_S)
    monkeypatch.setattr(sh, "_box_busy", lambda: pytest.fail("sampled load with nothing owed"))
    assert sh._run_daily_outcome(repo) == (0, False)
    assert all(p.exists() for p in young)


@pytest.mark.spawns_process
def test_daily_outcome_reports_deferral_for_busy_box_and_held_lock(tmp_path, monkeypatch, foreign_holder):
    import subprocess

    repo, common = _repo(tmp_path)
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: True)
    assert sh._run_daily_outcome(repo) == (0, True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    foreign_holder(common)
    rc, deferred = sh._run_daily_outcome(repo)
    assert deferred is True
