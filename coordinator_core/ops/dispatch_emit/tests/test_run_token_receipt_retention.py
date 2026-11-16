"""Receipt retention: the default staged-twin sweep unlinks spent/halted/retired receipts past RETENTION_SECONDS."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ops.dispatch_emit import launcher_staged_twin, run_token
from pathlib import Path

_DAY = 24 * 3600


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write(path, age_seconds, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": path.stem, "minted_at": now}), encoding="utf-8")
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def test_default_twin_sweep_unlinks_only_receipts_past_retention(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    now = time.time()
    old = {k: _write(run_token._record_path(repo, f"old-{k}", **{k: True}), 8 * _DAY, now)
           for k in ("spent", "halted", "retired")}
    young = {k: _write(run_token._record_path(repo, f"young-{k}", **{k: True}), 2 * _DAY, now)
             for k in ("spent", "halted", "retired")}
    live = _write(run_token._record_path(repo, "live-1"), 30 * _DAY, now)
    claimed = _write(run_token._record_path(repo, "claimed-1", claimed=True), 30 * _DAY, now)
    escrow = _write(run_token._record_path(repo, "esc-1", escrow=True), 0, now)
    index = live.parent / "coordinator-warp-live-run-index.json"
    _write(index, 30 * _DAY, now)

    launcher_staged_twin.sweep_expired_twins(repo)

    assert not any(p.exists() for p in old.values())
    assert all(p.exists() for p in young.values())
    assert live.exists() and claimed.exists() and escrow.exists() and index.exists()


def test_receipt_age_is_mtime_not_minted_at(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    now = time.time()
    path = run_token._record_path(repo, "late-settle", spent=True)
    _write(path, 0, now)
    path.write_text(json.dumps({"run_id": "late-settle", "minted_at": now - 30 * _DAY}), encoding="utf-8")
    os.utime(path, (now, now))
    assert run_token.sweep_expired_receipts(repo, now=now) == 0
    assert path.exists()


def test_sweep_counts_removed_and_tolerates_absent_home(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    now = time.time()
    _write(run_token._record_path(repo, "a", spent=True), 9 * _DAY, now)
    assert run_token.sweep_expired_receipts(repo, now=now) == 1
    assert run_token.sweep_expired_receipts(repo, now=now) == 0
    assert run_token.sweep_expired_receipts(tmp_path / "nowhere") == 0


@pytest.mark.parametrize("age_days,expected", [(6, True), (8, False)])
def test_retention_boundary_is_seven_days(tmp_path: Path, age_days, expected) -> None:
    repo = _repo(tmp_path)
    now = time.time()
    path = _write(run_token._record_path(repo, "edge", spent=True), age_days * _DAY, now)
    run_token.sweep_expired_receipts(repo, now=now)
    assert path.exists() is expected
