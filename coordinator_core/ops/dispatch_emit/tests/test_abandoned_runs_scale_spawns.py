"""Scale falsifier for `dispatch.reap_abandoned_runs`: git spawn count is constant in the number
of abandoned run dirs, and the handler's process time at 2000 dirs stays under the 500ms bar."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import abandoned_runs as ar
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SMALL = 10
LARGE = 2000
PROCESS_BUDGET_MS = 500.0
AGE = ar.TOKENLESS_QUIET_SECONDS + 600


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _repo_with_abandoned_runs(tmp_path: Path, count: int) -> Path:
    root = tmp_path / f"repo{count}"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    (root / "archive").mkdir()  # onboarded: the reap commits only in a repo hosting coordinator state
    stamp = time.time() - AGE
    for i in range(count):
        rec = root / "state" / "warp" / f"ask-20261001T{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}-{i:06x}"
        rec.mkdir(parents=True)
        for name, body in (("ask.md", "ask\n"), ("run.json", json.dumps({}))):
            path = rec / name
            path.write_text(body, encoding="utf-8")
            os.utime(path, (stamp, stamp))
    return root


def _measure(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[dict, int, float]:
    cpu0 = time.process_time()
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = ar._handler({}, repo_root=root / ".git")
    cpu_ms = (time.process_time() - cpu0) * 1000.0
    return out, len(spawns), cpu_ms


@pytest.mark.pending_fix  # base is red; the reap rewrite turns it green
def test_reap_spawns_are_constant_and_under_budget_at_2000_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_out, small_spawns, small_ms = _measure(monkeypatch, _repo_with_abandoned_runs(tmp_path, SMALL))
    large_out, large_spawns, large_ms = _measure(monkeypatch, _repo_with_abandoned_runs(tmp_path, LARGE))
    print(f"reap base: {SMALL} dirs spawns={small_spawns} ms={small_ms:.0f}; {LARGE} dirs spawns={large_spawns} ms={large_ms:.0f}")
    assert len(small_out["reaped"]) == SMALL and small_out["committed_sha"], small_out
    assert len(large_out["reaped"]) == LARGE and large_out["committed_sha"], large_out
    assert large_spawns == small_spawns, (small_spawns, large_spawns)
    assert large_ms < PROCESS_BUDGET_MS, large_ms
