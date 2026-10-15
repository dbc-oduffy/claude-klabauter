"""Process-time gate: the local-ahead reader stays under the 500ms brightline with zero spawns."""
import statistics
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.workday_start.local_ahead import local_ahead

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]
BAR_MS = 500.0
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BRANCH = "work/test/2026-10-15"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, creationflags=_FLAGS,
    )


def test_local_ahead_median_under_bar_and_spawns_nothing(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init")
    _git(work, "checkout", "-b", BRANCH)
    _git(work, "remote", "add", "origin", str(origin))
    for i in range(3):
        (work / "f.txt").write_text(str(i))
        _git(work, "add", "f.txt")
        _git(work, "commit", "-m", f"c{i}")
    _git(work, "push", "-u", "origin", BRANCH)
    for i in range(10):
        (work / "f.txt").write_text(f"a{i}")
        _git(work, "commit", "-am", f"a{i}")

    spawns: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a))
    code, text = local_ahead(work)
    assert code == 0 and "10 unpushed commits" in text
    samples = [
        in_process_time_ms(lambda: local_ahead(work))["process_time_ms"] for _ in range(5)
    ]
    median = statistics.median(samples)
    print(f"budget: local_ahead median {median}ms of {samples} (bar {BAR_MS}ms), spawns {len(spawns)}")
    assert median < BAR_MS
    assert spawns == []
