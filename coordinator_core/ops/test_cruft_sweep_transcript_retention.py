"""Transcript retention pass of cruft_sweep.sweep_harness: size or age bound, live sessions exempt."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from coordinator_core.ops import cruft_sweep

HOUR = 3600
LIMIT = 1000  # bytes; stands in for 250 MB, the boundary logic is identical
AGE = 48 * HOUR


def _file(path: Path, size: int, age_secs: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_secs
    os.utime(path, (t, t))
    return path


def _sweep(projects: Path, tmp: Path, live, *, apply=True, blocklist=()):
    return cruft_sweep.sweep_harness(
        projects, tmp / "fh", 14, blocklist,
        apply=apply, json_mode=False, quiet=True,
        transcript_live_ids=live,
        transcript_max_bytes=LIMIT, transcript_max_age_secs=AGE,
        size_cap_bytes=10**12,
    )


def test_ended_oversize_transcript_deleted_and_boundary_kept(tmp_path):
    repo = tmp_path / "projects" / "r"
    big, ok = str(uuid.uuid4()), str(uuid.uuid4())
    f_big = _file(repo / f"{big}.jsonl", LIMIT + 1, 60)
    f_ok = _file(repo / f"{ok}.jsonl", LIMIT, 60)
    _sweep(tmp_path / "projects", tmp_path, frozenset())
    assert not f_big.exists()
    assert f_ok.exists()


def test_ended_old_subagent_transcript_deleted_young_kept(tmp_path):
    repo = tmp_path / "projects" / "r"
    sid = str(uuid.uuid4())
    old = _file(repo / sid / "subagents" / "agent-old.jsonl", 10, 49 * HOUR)
    young = _file(repo / sid / "subagents" / "agent-young.jsonl", 10, 47 * HOUR)
    _sweep(tmp_path / "projects", tmp_path, frozenset())
    assert not old.exists()
    assert young.exists()


def test_live_session_files_kept_including_subagents(tmp_path):
    repo = tmp_path / "projects" / "r"
    sid = str(uuid.uuid4())
    main = _file(repo / f"{sid}.jsonl", LIMIT * 300, 100 * HOUR)
    sub = _file(repo / sid / "subagents" / "agent-a.jsonl", LIMIT * 5, 60 * HOUR)
    _sweep(tmp_path / "projects", tmp_path, frozenset({sid}))
    assert main.exists()
    assert sub.exists()


def test_unresolved_liveness_keeps_everything(tmp_path):
    repo = tmp_path / "projects" / "r"
    sid = str(uuid.uuid4())
    f = _file(repo / f"{sid}.jsonl", LIMIT * 5, 100 * HOUR)
    cruft_sweep.sweep_harness(
        tmp_path / "projects", tmp_path / "fh", 10**6, (), apply=True, json_mode=False,
        quiet=True, transcript_live_ids=None, size_cap_bytes=10**12,
    )
    assert f.exists()


def test_box_live_session_ids_exception_returns_none(monkeypatch):
    from coordinator_core.session import liveness

    def boom(*a, **k):
        raise RuntimeError("liveness down")

    monkeypatch.setattr(liveness, "live_session_ids", boom)
    assert cruft_sweep.box_live_session_ids() is None


def test_predecessor_blocklist_does_not_exempt(tmp_path):
    repo = tmp_path / "projects" / "r"
    sid = str(uuid.uuid4())
    f = _file(repo / f"{sid}.jsonl", LIMIT + 1, 1 * HOUR)
    _sweep(tmp_path / "projects", tmp_path, frozenset(), blocklist={sid})
    assert not f.exists()


def test_dry_run_deletes_nothing(tmp_path):
    repo = tmp_path / "projects" / "r"
    sid = str(uuid.uuid4())
    f = _file(repo / f"{sid}.jsonl", LIMIT + 1, 1 * HOUR)
    _sweep(tmp_path / "projects", tmp_path, frozenset(), apply=False)
    assert f.exists()


def test_defaults_are_250mb_and_48h():
    assert cruft_sweep._TRANSCRIPT_MAX_BYTES_DEFAULT == 250 * 1024**2
    assert cruft_sweep._TRANSCRIPT_MAX_AGE_SECS_DEFAULT == 48 * 3600
