"""sweep_warp_runs: a closed, untracked, uncited run dir past retention is deleted whole."""
from __future__ import annotations

import os
import subprocess
import time

from coordinator_core.ops import cruft_sweep
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path
import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DAY = 86400


def _run(root, name, *, outcome_age_days=None):
    run = root / "state" / "warp" / name
    (run / "prompts").mkdir(parents=True)
    (run / "prompts" / "p.md").write_text("x" * 10)
    if outcome_age_days is not None:
        f = run / RUN_OUTCOME_FILE
        f.write_text("{}")
        t = time.time() - outcome_age_days * DAY
        os.utime(f, (t, t))
    return run


def _sweep(root, dry_run: bool = False) -> tuple[int, int]:
    return cruft_sweep.sweep_warp_runs(root, now=time.time(), dry_run=dry_run, quiet=True)


def test_tracked_run_dir_kept(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    tracked =_run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    loose = _run(tmp_path, "ask-20260102T000000-bbbbbb", outcome_age_days=30)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", "state/warp/ask-20260101T000000-aaaaaa/" + RUN_OUTCOME_FILE],
        check=True,
        **no_console_creationflags(),
    )
    _, n = _sweep(tmp_path)
    assert n == 1
    assert tracked.exists() and not loose.exists()
