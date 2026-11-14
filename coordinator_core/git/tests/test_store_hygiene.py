"""Pins `store_hygiene.status` on tmp_path fixtures and the module's CLI."""

import json
import os
import struct
import threading
import time

import pytest

from coordinator_core.git import store_hygiene as sh


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
    monkeypatch.setattr(sh, "consolidate_small_packs", lambda c: pytest.fail("consolidated"))
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


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_daily_consolidates_200_engine_packs_and_keeps_every_object(tmp_path, monkeypatch):
    from coordinator_core.git import git_objects as go

    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    repo, common, shas = _git_fixture(tmp_path, 200, real_git=True)
    assert sh.status(common).pack_count == 200
    assert sh.main(["--repo", str(repo), "--daily"]) == 0
    s = sh.status(common)
    assert s.pack_count <= sh.CONSOLIDATE_MAX_PACKS
    assert s.pack_count - (s.midx_pack_count or 0) <= sh.STALE_PACK_SLACK
    go._PACK_LISTING_CACHE.clear()
    go._PACK_INDEXES_BY_LISTING.clear()
    for sha, body in shas:
        assert go.read_object(common, sha) == ("blob", body)


def test_consolidate_leaves_a_store_at_the_bound_alone(tmp_path):
    _, common, _ = _git_fixture(tmp_path, sh.CONSOLIDATE_MAX_PACKS)
    assert sh.consolidate_small_packs(common) == 0
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


def test_consolidate_merges_packs_in_the_64kb_to_1mb_band(tmp_path):
    from coordinator_core.git import git_objects as go

    common = _common(tmp_path)
    shas = _sized_packs(common, sh.CONSOLIDATE_MAX_PACKS + 8, 200 * 1024)
    assert sh.consolidate_small_packs(common) > 0
    assert sh.status(common).pack_count <= sh.CONSOLIDATE_MAX_PACKS
    go._PACK_LISTING_CACHE.clear()
    go._PACK_INDEXES_BY_LISTING.clear()
    for sha, body in shas:
        assert go.read_object(common, sha) == ("blob", body)


def test_consolidate_leaves_packs_at_or_over_the_floor_alone(tmp_path):
    common = _common(tmp_path)
    _sized_packs(common, sh.CONSOLIDATE_MAX_PACKS + 3, sh.CONSOLIDATE_SIZE_FLOOR)
    assert sh.consolidate_small_packs(common) == 0
    assert sh.status(common).pack_count == sh.CONSOLIDATE_MAX_PACKS + 3


def test_one_pass_stops_at_the_pass_byte_bound(tmp_path, monkeypatch):
    common = _common(tmp_path)
    n = sh.CONSOLIDATE_MAX_PACKS + 20
    _sized_packs(common, n, 200 * 1024)
    monkeypatch.setattr(sh, "CONSOLIDATE_PASS_BYTES", 10 * 200 * 1024)
    removed = sh.consolidate_small_packs(common)
    assert removed == 9
    assert sh.status(common).pack_count == n - 9 + 1


def test_consolidate_skips_keep_and_unreadable_packs(tmp_path):
    _, common, _ = _git_fixture(tmp_path, sh.CONSOLIDATE_MAX_PACKS + 5)
    pack_dir = common / "objects" / "pack"
    kept = sorted(pack_dir.glob("*.idx"))[0]
    kept.with_suffix(".keep").write_bytes(b"")
    (pack_dir / ("pack-" + "f" * 40 + ".idx")).write_bytes(b"torn")
    (pack_dir / ("pack-" + "f" * 40 + ".pack")).write_bytes(b"torn")
    sh.consolidate_small_packs(common)
    assert kept.exists() and kept.with_suffix(".pack").exists()
    assert (pack_dir / ("pack-" + "f" * 40 + ".idx")).exists()


def test_deferred_tier_retries_then_releases_the_claim(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path)
    claim = common / "coordinator" / "store-hygiene.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_bytes(b"")
    calls = []

    def deferred(r, tier):
        calls.append(tier)
        return gm.MaintenanceResult(tier=tier, deferred="index.lock is held")

    monkeypatch.setattr(gm, "run_tier", deferred)
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    monkeypatch.setattr(sh, "DEFER_RETRY_S", 0)
    assert sh._run_daily(repo) == 0
    assert len(calls) == sh.DEFER_RETRIES + 1
    assert not claim.exists()


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


def test_consolidate_removes_nothing_while_another_process_holds_the_lock(tmp_path, foreign_holder):
    common = _common(tmp_path)
    _sized_packs(common, sh.CONSOLIDATE_MAX_PACKS + 8, 200 * 1024)
    before = sorted(p.name for p in (common / "objects" / "pack").iterdir())
    foreign_holder(common)
    assert sh.consolidate_small_packs(common) == 0
    assert sorted(p.name for p in (common / "objects" / "pack").iterdir()) == before


def test_consolidate_runs_again_once_the_lock_is_free(tmp_path):
    common = _common(tmp_path)
    _sized_packs(common, sh.CONSOLIDATE_MAX_PACKS + 8, 200 * 1024)
    assert sh.consolidate_small_packs(common) > 0


def test_daily_releases_its_claim_and_runs_nothing_while_the_lock_is_held(
    tmp_path, monkeypatch, foreign_holder
):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    claim = common / "coordinator" / "store-hygiene.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_bytes(b"")
    foreign_holder(common)
    monkeypatch.setattr(gm, "run_tier", lambda r, t: pytest.fail("tier ran under a held lock"))
    monkeypatch.setattr(sh, "consolidate_small_packs", lambda c: pytest.fail("consolidated"))
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    assert sh._run_daily(repo) == 0
    assert not claim.exists()


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


def test_sweep_removes_stale_tmp_packs_with_or_without_siblings(tmp_path):
    common = _common(tmp_path)
    stale = _tmp_pack_files(common, _STALE_NAMES, 25 * 3600)
    assert sh.sweep_stale_tmp_packs(common) == len(_STALE_NAMES)
    assert not any(p.exists() for p in stale)


def test_sweep_keeps_young_tmp_packs_and_real_packs(tmp_path):
    common = _common(tmp_path)
    young = _tmp_pack_files(common, _STALE_NAMES, 23 * 3600)
    real = _tmp_pack_files(common, ["pack-abc.pack", "pack-abc.idx", ".keepme", "pack-abc.keep"], 48 * 3600)
    assert sh.sweep_stale_tmp_packs(common) == 0
    assert all(p.exists() for p in young + real)


def test_sweep_on_a_missing_pack_dir_is_zero(tmp_path):
    assert sh.sweep_stale_tmp_packs(_common(tmp_path)) == 0


def test_daily_sweeps_stale_tmp_packs_under_the_lock(tmp_path, monkeypatch):
    from coordinator_core.git import object_store_lock as osl
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    stale = _tmp_pack_files(common, _STALE_NAMES, 25 * 3600)
    depths = []
    real_sweep = sh.sweep_stale_tmp_packs

    def spy(c):
        depths.append(osl._depth.__dict__.get("depths", {}).get(str(common), 0))
        return real_sweep(c)

    monkeypatch.setattr(sh, "sweep_stale_tmp_packs", spy)
    monkeypatch.setattr(sh, "consolidate_small_packs", lambda c: 0)
    monkeypatch.setattr(gm, "run_tier", lambda r, t: gm.MaintenanceResult(tier=t, ran=True))
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    assert sh._run_daily(repo) == 0
    assert depths == [1]
    assert not any(p.exists() for p in stale)


def test_daily_sweeps_nothing_while_another_process_holds_the_lock(tmp_path, monkeypatch, foreign_holder):
    repo, common = _repo(tmp_path)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    stale = _tmp_pack_files(common, _STALE_NAMES, 25 * 3600)
    foreign_holder(common)
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    assert sh._run_daily(repo) == 0
    assert all(p.exists() for p in stale)


def test_daily_holds_the_lock_for_consolidate_but_not_across_the_tier_retry_loop(tmp_path, monkeypatch):
    from coordinator_core.git import object_store_lock as osl
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    seen = []

    def probe(tag):
        depths = osl._depth.__dict__.get("depths", {})
        seen.append((tag, depths.get(str(common), 0)))

    monkeypatch.setattr(sh, "consolidate_small_packs", lambda c: probe("consolidate") or 0)
    monkeypatch.setattr(
        gm, "run_tier", lambda r, t: probe("tier") or gm.MaintenanceResult(tier=t, ran=True)
    )
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    assert sh._run_daily(repo) == 0
    assert [tag for tag, _ in seen] == ["consolidate", "tier"]
    assert dict(seen) == {"consolidate": 1, "tier": 0}


def test_quiet_store_with_a_stale_tmp_pack_is_still_reaped(tmp_path, monkeypatch):
    from coordinator_core.ops import git_maintenance as gm

    repo, common = _repo(tmp_path, packs=1)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    stale = _tmp_pack_files(common, _STALE_NAMES, 25 * 3600)
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: False)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    monkeypatch.setattr(sh, "consolidate_small_packs", lambda c: pytest.fail("consolidated"))
    monkeypatch.setattr(gm, "run_tier", lambda r, t: pytest.fail("tier ran with nothing owed"))
    assert sh._run_daily_outcome(repo) == (0, False)
    assert not any(p.exists() for p in stale)


def test_quiet_store_with_only_young_tmp_packs_does_nothing(tmp_path, monkeypatch):
    repo, common = _repo(tmp_path, packs=1)
    young = _tmp_pack_files(common, _STALE_NAMES, 2 * 3600)
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: False)
    monkeypatch.setattr(sh, "_box_busy", lambda: pytest.fail("sampled load with nothing owed"))
    assert sh._run_daily_outcome(repo) == (0, False)
    assert all(p.exists() for p in young)


def test_daily_outcome_reports_deferral_for_busy_box_and_held_lock(tmp_path, monkeypatch, foreign_holder):
    repo, common = _repo(tmp_path)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(sh, "_owes_maintenance", lambda r, c: True)
    monkeypatch.setattr(sh, "_box_busy", lambda: True)
    assert sh._run_daily_outcome(repo) == (0, True)
    monkeypatch.setattr(sh, "_box_busy", lambda: False)
    foreign_holder(common)
    assert sh._run_daily_outcome(repo) == (0, True)
