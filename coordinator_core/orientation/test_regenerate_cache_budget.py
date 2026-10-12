"""
coordinator_core.orientation.test_regenerate_cache_budget -- pins the 500ms
process-time bar for ``build_cache``: a git spawn ceiling, a no-tree-walk
assertion, and a tree-accounted process-time measurement. Process time and
spawn count only, never wall clock.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.op_census.timing import PROCESS_TIME_BAR_MS
from coordinator_core.orientation import regenerate_cache as mod

# Declared, not excused: the fixture repo is built with real git, and the
# process-time leg spawns a child interpreter under the process-tree accountant.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

#: Docs/research/2026-10-12-orientation-cache-regeneration-cost-spike.md verdict.
GIT_SPAWN_CEILING = 3

_DOCUMENTED_WINDOWS_TICK_MS = 15.625
_K = 3

_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@example.com",
)


def _run(args: list, cwd: Path) -> None:
    subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        env=_ENV,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """A work branch with an ``origin`` and unpushed commits."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run(["git", "init", "-q", "--bare", "-b", "main"], origin)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tasks").mkdir()
    (repo / "state").mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "seed"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "origin", "main"], repo)
    _run(["git", "checkout", "-q", "-b", "work/tester/2026-10-12"], repo)
    for i in range(3):
        (repo / f"unpushed-{i}.txt").write_text(str(i), encoding="utf-8")
        _run(["git", "add", "."], repo)
        _run(["git", "commit", "-q", "-m", f"unpushed {i}"], repo)
    return repo


def test_build_cache_stays_under_the_git_spawn_ceiling(tmp_path, monkeypatch):
    repo = _fixture_repo(tmp_path)
    with _count_spawns_attributed(monkeypatch) as spawns:
        result = mod.build_cache("workday-start", repo)
    assert result["skipped"] is False
    argv0s = [Path(s.argv[0]).stem.lower() for s in spawns]
    others = [s.argv for s in spawns if Path(s.argv[0]).stem.lower() != "git"]
    assert not others, f"build_cache started a non-git process: {others!r}"
    assert argv0s.count("git") <= GIT_SPAWN_CEILING, (
        f"{argv0s.count('git')} git spawns exceed the ceiling of {GIT_SPAWN_CEILING}: "
        f"{[s.argv for s in spawns]!r}"
    )


def test_build_cache_never_enumerates_an_unlisted_directory(tmp_path, monkeypatch):
    repo = _fixture_repo(tmp_path)
    big = repo / "vendor" / "big"
    nested = big / "deep" / "deeper"
    nested.mkdir(parents=True)
    (nested / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    (nested / "Planted.uproject").write_text("{}", encoding="utf-8")
    for i in range(3000):
        (big / f"f{i}.txt").write_text("", encoding="utf-8")

    guarded = big.resolve()

    def _under(path) -> bool:
        try:
            p = Path(os.fspath(path)).resolve()
        except (OSError, TypeError, ValueError):
            return False
        return p == guarded or guarded in p.parents

    # Scoped to the call: another fixture's teardown may enumerate tmp_path legitimately.
    with monkeypatch.context() as m:
        for name in ("walk", "scandir", "listdir"):
            real = getattr(os, name)

            def _guard(path=".", *a, _real=real, _name=name, **kw):
                if _under(path):
                    raise AssertionError(f"os.{_name} enumerated {path}")
                return _real(path, *a, **kw)

            m.setattr(os, name, _guard)

        result = mod.build_cache("workday-start", repo)
    assert result["skipped"] is False


_DRIVER = textwrap.dedent(
    """\
    import sys
    sys.path.insert(0, {root!r})
    from pathlib import Path
    {body}
    """
)


def _write_driver(path: Path, body: str) -> None:
    root = str(Path(__file__).resolve().parents[2])
    path.write_text(_DRIVER.format(root=root, body=body), encoding="utf-8")


def test_build_cache_process_time_is_under_the_bar(tmp_path):
    repo = _fixture_repo(tmp_path)

    floor_driver = tmp_path / "floor_driver.py"
    _write_driver(
        floor_driver,
        "from coordinator_core.orientation import regenerate_cache",
    )
    op_driver = tmp_path / "op_driver.py"
    _write_driver(
        op_driver,
        "from coordinator_core.orientation import regenerate_cache as m\n"
        f"m.build_cache('workday-start', Path({str(repo)!r}))",
    )

    floor = batched_process_time_ms([sys.executable, str(floor_driver)], k=_K)
    assert floor["rc"] == 0, f"floor driver failed: {floor!r}"
    measured = batched_process_time_ms([sys.executable, str(op_driver)], k=_K)
    assert measured["rc"] == 0, f"build_cache driver failed: {measured!r}"

    tick = max(
        time.get_clock_info("process_time").resolution * 1000.0,
        _DOCUMENTED_WINDOWS_TICK_MS if sys.platform == "win32" else 0.0,
    )
    net = measured["process_time_ms"] - floor["process_time_ms"]
    print(
        f"build_cache net process time {net:.1f}ms "
        f"(total {measured['process_time_ms']:.1f}ms, floor {floor['process_time_ms']:.1f}ms, "
        f"procs/call {measured['procs_per_call']:.1f} vs floor {floor['procs_per_call']:.1f})"
    )
    assert measured["process_time_ms"] >= tick, (
        f"total {measured['process_time_ms']}ms is below the {tick}ms tick: "
        "a sub-tick reading cannot pass the bar vacuously"
    )
    assert net <= PROCESS_TIME_BAR_MS, (
        f"build_cache net process time {net:.1f}ms exceeds {PROCESS_TIME_BAR_MS}ms "
        f"(children included)"
    )
