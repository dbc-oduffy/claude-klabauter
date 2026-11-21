"""live_run_writes: footprints of live runs from token records and staged manifests."""

from __future__ import annotations


import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import live_run_index
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT = "scratch/warp/x.mjs"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "locks"))
    return root


def _index(root):
    got = live_run_index.read(root, 2000.0)
    assert got is not live_run_index.MISSING and got is not live_run_index.STALLED
    return got


@pytest.mark.spawns_process
def test_concurrent_mints_in_separate_processes_all_land(repo) -> None:
    import subprocess
    import sys

    code = (
        "import sys; from pathlib import Path; from coordinator_core.ops.dispatch_emit import run_token;"
        "[run_token.mint(Path(sys.argv[1]), f'{sys.argv[2]}{i}', sys.argv[3], session_id=None) for i in range(4)]"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(repo), f"p{n}_", SCRIPT],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for n in range(4)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    assert set(_index(repo)) == {f"p{n}_{i}" for n in range(4) for i in range(4)}
