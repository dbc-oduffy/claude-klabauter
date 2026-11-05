"""sweep_warp_prompts: a closed run's prompts/ is deleted after the retention window."""
from __future__ import annotations

import os
import time

from coordinator_core.ops import cruft_sweep
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE

DAY = 86400


def _run(tmp_path, name, *, outcome_age_days=None):
    run = tmp_path / "state" / "warp" / name
    (run / "prompts").mkdir(parents=True)
    (run / "prompts" / "p.md").write_text("x" * 10)
    if outcome_age_days is not None:
        f = run / RUN_OUTCOME_FILE
        f.write_text("{}")
        t = time.time() - outcome_age_days * DAY
        os.utime(f, (t, t))
    return run


def test_old_closed_run_prompts_deleted_others_kept(tmp_path):
    old = _run(tmp_path, "old", outcome_age_days=8)
    fresh = _run(tmp_path, "fresh", outcome_age_days=6)
    open_ = _run(tmp_path, "open")
    b, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True)
    assert n == 1 and b >= 10
    assert not (old / "prompts").exists()
    assert (old / RUN_OUTCOME_FILE).exists()
    assert (fresh / "prompts" / "p.md").exists()
    assert (open_ / "prompts" / "p.md").exists()


def test_dry_run_counts_without_deleting(tmp_path):
    old = _run(tmp_path, "old", outcome_age_days=30)
    _, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=True, quiet=True)
    assert n == 1
    assert (old / "prompts" / "p.md").exists()


def _rm_spawns(tmp_path, monkeypatch, n_due):
    for i in range(n_due):
        _run(tmp_path, f"old{i}", outcome_age_days=30)
    real = cruft_sweep.subprocess.run
    calls = []

    def counting(argv, *a, **kw):
        if argv and argv[0] == "rm":
            calls.append(argv)
        return real(argv, *a, **kw)

    monkeypatch.setattr(cruft_sweep.subprocess, "run", counting)
    _, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True)
    assert n == n_due
    return len(calls)


def test_rm_spawn_count_independent_of_due_runs(tmp_path, monkeypatch):
    one = _rm_spawns(tmp_path / "a", monkeypatch, 1)
    five = _rm_spawns(tmp_path / "b", monkeypatch, 5)
    assert one == five


def test_surviving_target_not_counted(tmp_path, monkeypatch):
    keep = _run(tmp_path, "keep", outcome_age_days=30)
    gone = _run(tmp_path, "gone", outcome_age_days=30)
    real = cruft_sweep._delete_paths_batch

    def batch(targets, **kw):
        res = real([t for t in targets if t != keep / "prompts"], **kw)
        res[str(keep / "prompts")] = False
        return res

    monkeypatch.setattr(cruft_sweep, "_delete_paths_batch", batch)
    _, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True)
    assert n == 1
    assert not (gone / "prompts").exists()


def test_missing_warp_root_is_noop(tmp_path):
    assert cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True) == (0, 0)
