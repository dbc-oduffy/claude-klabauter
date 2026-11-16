"""Sustained busy load must not starve object-store hygiene: pack count and MIDX freshness stay bounded.

Drives `store_hygiene._run_daily_outcome` through simulated ticks on a real git repo with
`_box_busy` pinned True while one small pack is minted per tick. Pause ON is the shipped
state (MIDX bounds only); pause OFF adds the pack-count bound, with the force triggers
lowered so they are reachable in a few ticks.
"""

import time
from pathlib import Path

import pytest

from coordinator_core.git import pack_deletion_pause
from coordinator_core.git import store_hygiene as sh
from coordinator_core.git.run import run_git

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]

TICK_CPU_BAR_S = 0.5


def _git(repo: Path, args: list[str], data: bytes | None = None) -> str:
    res = run_git(args, cwd=str(repo), input=data)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res.stdout.strip()


def _mint_pack(repo: Path, seq: int) -> None:
    sha = _git(repo, ["hash-object", "-w", "--stdin"], f"blob {seq}\n".encode())
    # Engine-minted packs carry no .rev; a .rev sibling marks a pack as git's and never a consolidation source.
    _git(
        repo,
        ["-c", "pack.writeReverseIndex=false", "pack-objects", "-q", str(repo / ".git" / "objects" / "pack" / "pack")],
        (sha + "\n").encode(),
    )


def _children_cpu() -> float:
    """Trap: psutil reports children CPU as 0.0 on Windows and macOS, so there the tick bar below
    bounds in-process CPU only; spawn cost is pinned by `batched_process_time_ms` in test_git_maintenance."""
    t = pytest.importorskip("psutil").Process().cpu_times()
    return t.children_user + t.children_system


def _drop_one_pack(pack_dir: Path) -> None:
    idx = sorted(pack_dir.glob("pack-*.idx"))[0]
    for suffix in (".idx", ".pack", ".rev"):
        sh._unlink_quiet(idx.with_suffix(suffix))


@pytest.mark.parametrize("paused", [True, False], ids=["pause-on", "pause-off"])
def test_sustained_busy_load_keeps_pack_count_and_midx_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, ["init", "-q"])
    common = repo / ".git"
    pack_dir = common / "objects" / "pack"
    hl.stamp_liveness(str(repo), hl.GIT_MAINTENANCE)

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", paused)
    monkeypatch.setattr(sh, "_box_busy", lambda: True)
    ticks = 40
    if not paused:
        monkeypatch.setattr(sh, "CONSOLIDATE_MAX_PACKS", 4)
        monkeypatch.setattr(sh, "PACK_COUNT_FORCE", 12)
        monkeypatch.setattr(sh, "CONSOLIDATE_BATCH_PACKS", 6)
        monkeypatch.setattr(sh, "CONSOLIDATE_MAX_DEFER_S", 0)
        ticks = 20
        for i in range(sh.PACK_COUNT_FORCE + 2):
            _mint_pack(repo, 10_000 + i)
        assert sh.status(common).pack_count > sh.PACK_COUNT_FORCE

    seq = 0
    worst_s = 0.0
    for tick in range(ticks):
        _mint_pack(repo, seq := seq + 1)
        if tick == 5:
            _git(repo, ["multi-pack-index", "write"])
            _drop_one_pack(pack_dir)
            assert sh.midx_names_missing_pack(common)
        cpu0, kids0 = time.process_time(), _children_cpu()
        sh._run_daily_outcome(repo)
        spent = time.process_time() - cpu0 + _children_cpu() - kids0
        worst_s = max(worst_s, spent)

    s = sh.status(common)
    state = f"packs={s.pack_count} midx={s.midx_pack_count} worst_tick={worst_s * 1000:.0f}ms"
    print(state)
    assert not sh.midx_names_missing_pack(common), state
    assert s.midx_pack_count is not None, state
    assert s.pack_count - s.midx_pack_count <= sh.STALE_PACK_SLACK
    if not paused:
        assert s.pack_count <= sh.PACK_COUNT_FORCE + sh.CONSOLIDATE_BATCH_PACKS
    assert worst_s < TICK_CPU_BAR_S, f"worst tick {worst_s * 1000:.0f}ms"


def test_midx_upkeep_owed_is_stat_only_and_tracks_the_two_triggers(tmp_path: Path) -> None:
    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    assert sh.midx_upkeep_owed(tmp_path) is False
    for i in range(sh.STALE_PACK_SLACK + 1):
        (pack_dir / f"pack-{i:040x}.idx").write_bytes(b"x")
    assert sh.midx_upkeep_owed(tmp_path) is True
