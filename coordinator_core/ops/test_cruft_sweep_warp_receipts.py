"""Prune of untracked state/warp run state keeps open-run and in-retention receipts and promotes nothing."""
from __future__ import annotations

import os
import time

from coordinator_core.ops import cruft_sweep
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE

DAY = 86400


def _run(root, name, *, outcome_age_days=None):
    run = root / "state" / "warp" / name
    (run / "receipts").mkdir(parents=True)
    (run / "receipts" / "C1.json").write_text("{}")
    (run / "prompts").mkdir()
    (run / "prompts" / "p.md").write_text("x")
    if outcome_age_days is not None:
        f = run / RUN_OUTCOME_FILE
        f.write_text("{}")
        t = time.time() - outcome_age_days * DAY
        os.utime(f, (t, t))
    return run


def _prune(root):
    now = time.time()
    cruft_sweep.sweep_warp_prompts(root, now=now, dry_run=False, quiet=True)
    return cruft_sweep.sweep_warp_runs(root, now=now, dry_run=False, quiet=True)


def _files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_open_and_in_retention_receipts_kept_past_retention_deleted(tmp_path):
    open_ = _run(tmp_path, "ask-20260101T000000-aaaaaa")
    fresh = _run(tmp_path, "ask-20260102T000000-bbbbbb", outcome_age_days=6)
    old = _run(tmp_path, "ask-20260103T000000-cccccc", outcome_age_days=30)
    _, n = _prune(tmp_path)
    assert n == 1
    assert (open_ / "receipts" / "C1.json").is_file()
    assert (fresh / "receipts" / "C1.json").is_file()
    assert not old.exists()


def test_prune_promotes_nothing_outside_state_warp(tmp_path):
    _run(tmp_path, "ask-20260101T000000-aaaaaa")
    _run(tmp_path, "ask-20260102T000000-bbbbbb", outcome_age_days=30)
    before = _files(tmp_path)
    _prune(tmp_path)
    created = _files(tmp_path) - before
    assert created == set()
    assert all(p.startswith("state/warp/") for p in _files(tmp_path))
