"""Falsifier tests for the cache pruner (disk_budget.prune_cache) and Policy/roots.

uv is stubbed through the injected runner; fixture trees live under tmp_path only.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_budget  # noqa: E402
from disk_budget import GiB, Policy, resolve_roots  # noqa: E402

MiB = 1024 * 1024


def _file(path: Path, size: int, mtime: float = 1_700_000_000.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    os.utime(path, (mtime, mtime))
    return path


class FakeUv:
    """Runner stub: `prune` frees `prune_frees` bytes, `clean` empties the cache."""

    def __init__(self, uv_dir: Path, prune_frees: int = 0):
        self.uv_dir = uv_dir
        self.prune_frees = prune_frees
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append(tuple(argv))
        sub = argv[-1]
        blobs = sorted(self.uv_dir.rglob("blob*"))
        if sub == "clean":
            for b in blobs:
                b.unlink()
        elif sub == "prune" and self.prune_frees:
            freed = 0
            for b in blobs:
                if freed >= self.prune_frees:
                    break
                freed += b.stat().st_size
                b.unlink()
        return 0


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def _prune(roots, runner, **policy):
    return disk_budget.prune_cache(
        Policy(**policy), time.monotonic() + 30, roots=roots, runner=runner
    )


def test_policy_defaults_and_env_overrides():
    p = Policy()
    assert (p.projects_cap, p.per_session_cap) == (4 * GiB, 1 * GiB)
    assert (p.min_age_hours, p.subagent_max_age_hours, p.transcript_max_age_days) == (24, 24, 7)
    assert (p.cache_total_cap, p.uv_cap, p.hf_cap, p.codex_keep) == (8 * GiB, 3 * GiB, 6 * GiB, 2)
    assert (p.throttle_hours, p.deadline_s) == (6, 25)
    q = Policy.from_env({"COORDINATOR_DISK_BUDGET_UV_CAP": "1024", "COORDINATOR_DISK_BUDGET_CODEX_KEEP": "5",
                         "COORDINATOR_DISK_BUDGET_HF_CAP": "junk"})
    assert q.uv_cap == 1024 and q.codex_keep == 5 and q.hf_cap == 6 * GiB


def test_resolve_roots_honours_uv_cache_dir(home, tmp_path):
    r = resolve_roots(home=home, environ={"UV_CACHE_DIR": str(tmp_path / "uvx")})
    assert r.uv_cache == tmp_path / "uvx"
    assert r.projects == home / ".claude" / "projects"
    d = resolve_roots(home=home, environ={})
    assert d.uv_cache == home / ".cache" / "uv"
    assert d.codex_runtimes == home / ".cache" / "codex-runtimes"


def test_uv_over_cap_prunes_then_cleans_only_if_still_over(home):
    roots = resolve_roots(home=home, environ={})
    for i in range(4):
        _file(roots.uv_cache / f"blob{i}", 2 * MiB)
    runner = FakeUv(roots.uv_cache, prune_frees=0)
    report = _prune(roots, runner, uv_cap=3 * MiB)
    assert [c[-1] for c in runner.calls] == ["prune", "clean"]
    assert not list(roots.uv_cache.rglob("blob*"))
    assert report.freed_bytes >= 8 * MiB


def test_uv_prune_sufficient_skips_clean(home):
    roots = resolve_roots(home=home, environ={})
    for i in range(4):
        _file(roots.uv_cache / f"blob{i}", 2 * MiB)
    runner = FakeUv(roots.uv_cache, prune_frees=6 * MiB)
    _prune(roots, runner, uv_cap=3 * MiB)
    assert [c[-1] for c in runner.calls] == ["prune"]


def test_uv_under_cap_not_touched(home):
    roots = resolve_roots(home=home, environ={})
    _file(roots.uv_cache / "blob0", MiB)
    runner = FakeUv(roots.uv_cache)
    _prune(roots, runner, uv_cap=3 * MiB)
    assert runner.calls == []


def test_uv_absent_is_logged_and_skipped(home):
    roots = resolve_roots(home=home, environ={})
    _file(roots.uv_cache / "blob0", 5 * MiB)

    def missing(argv, timeout_s):
        raise FileNotFoundError("uv")

    report = _prune(roots, missing, uv_cap=MiB)
    assert any("uv-absent" in line for line in report.log)
    assert (roots.uv_cache / "blob0").exists()


def test_huggingface_keeps_embedder_newest_snapshot(home):
    roots = resolve_roots(home=home, environ={})
    model = roots.huggingface / "models--org--embedder"
    old = _file(model / "snapshots" / "aaa" / "w.bin", 4 * MiB, 1_600_000_000.0)
    new = _file(model / "snapshots" / "bbb" / "w.bin", 4 * MiB, 1_700_000_000.0)
    other = _file(roots.huggingface / "models--org--other" / "snapshots" / "c" / "w.bin", 4 * MiB, 1_500_000_000.0)
    disk_budget.prune_cache(
        Policy(hf_cap=5 * MiB), time.monotonic() + 30, roots=roots, runner=FakeUv(roots.uv_cache),
        embedder_model="org/embedder",
    )
    assert new.exists()
    assert not old.exists()
    assert not other.exists()


def test_codex_runtimes_keeps_newest_two(home):
    roots = resolve_roots(home=home, environ={})
    dirs = []
    for i in range(4):
        d = roots.codex_runtimes / f"v{i}"
        _file(d / "bin", MiB, 1_700_000_000.0 + i)
        os.utime(d, (1_700_000_000.0 + i, 1_700_000_000.0 + i))
        dirs.append(d)
    _prune(roots, FakeUv(roots.uv_cache), codex_keep=2)
    assert [d.exists() for d in dirs] == [False, False, True, True]


def test_infinite_deadline_passes_no_timeout_to_runner(home):
    import math

    roots = resolve_roots(home=home, environ={})
    _file(roots.uv_cache / "blob0", 5 * MiB)
    seen = []

    def runner(argv, timeout_s):
        seen.append(timeout_s)
        return 0

    disk_budget.prune_cache(Policy(uv_cap=MiB), math.inf, roots=roots, runner=runner)
    assert seen and all(t is None for t in seen)


def test_finite_deadline_passes_bounded_timeout(home):
    roots = resolve_roots(home=home, environ={})
    _file(roots.uv_cache / "blob0", 5 * MiB)
    seen = []

    def runner(argv, timeout_s):
        seen.append(timeout_s)
        return 0

    _prune(roots, runner, uv_cap=MiB)
    assert seen and all(t is not None and t >= 1.0 for t in seen)


def test_total_cap_removes_oldest_unrecognised_dirs_first(home):
    roots = resolve_roots(home=home, environ={})
    _file(roots.uv_cache / "blob0", MiB)
    old = _file(roots.cache / "old-tool" / "a.bin", 4 * MiB, 1_500_000_000.0)
    mid = _file(roots.cache / "mid-tool" / "a.bin", 4 * MiB, 1_600_000_000.0)
    new = _file(roots.cache / "new-tool" / "a.bin", 4 * MiB, 1_700_000_000.0)
    report = _prune(roots, FakeUv(roots.uv_cache), cache_total_cap=6 * MiB)
    assert not old.exists() and not mid.exists()
    assert new.exists()
    assert (roots.uv_cache / "blob0").exists()
    assert report.per_root["cache-other"] == 8 * MiB
    assert report.still_over is False


def test_total_cap_spares_owned_elsewhere_and_embedder_snapshot(home):
    roots = resolve_roots(home=home, environ={})
    ladder = _file(roots.cache / "experiments-1d" / "l.bin", 20 * MiB, 1_400_000_000.0)
    retest = _file(roots.cache / "claude-klabauter-retest-exports" / "r.bin", 20 * MiB, 1_400_000_000.0)
    snap = _file(
        roots.huggingface / "models--org--embedder" / "snapshots" / "bbb" / "w.bin", 4 * MiB, 1_400_000_000.0
    )
    junk = _file(roots.cache / "junk" / "j.bin", 4 * MiB, 1_500_000_000.0)
    report = disk_budget.prune_cache(
        Policy(cache_total_cap=MiB), time.monotonic() + 30, roots=roots,
        runner=FakeUv(roots.uv_cache), embedder_model="org/embedder",
    )
    assert not junk.exists()
    assert ladder.exists() and retest.exists() and snap.exists()
    assert report.still_over is True


def test_total_cap_dry_run_deletes_nothing(home):
    roots = resolve_roots(home=home, environ={})
    junk = _file(roots.cache / "junk" / "j.bin", 4 * MiB)
    report = disk_budget.prune_cache(
        Policy(cache_total_cap=MiB), time.monotonic() + 30, roots=roots,
        runner=FakeUv(roots.uv_cache), apply=False,
    )
    assert junk.exists() and report.still_over is True


def test_paths_outside_owned_subtrees_untouched(home):
    roots = resolve_roots(home=home, environ={})
    ladder = _file(roots.cache / "experiments-1d" / "ladder" / "big.bin", 50 * MiB)
    _file(roots.uv_cache / "blob0", 5 * MiB)
    report = _prune(roots, FakeUv(roots.uv_cache), uv_cap=MiB, cache_total_cap=MiB)
    assert ladder.exists()
    assert report.still_over is True
