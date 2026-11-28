"""sweep_warp_runs: a closed, untracked, uncited run dir past retention is deleted whole."""
from __future__ import annotations

import os
import time

from coordinator_core.ops import cruft_sweep
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE
from pathlib import Path
import pytest

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


def test_old_closed_uncited_run_deleted_open_and_fresh_kept(tmp_path: Path) -> None:
    old = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=8)
    fresh = _run(tmp_path, "ask-20260102T000000-bbbbbb", outcome_age_days=6)
    open_ = _run(tmp_path, "ask-20260103T000000-cccccc")
    b, n = _sweep(tmp_path)
    assert n == 1 and b >= 10
    assert not old.exists()
    assert fresh.exists() and open_.exists()


def test_cited_run_kept_from_each_home_uncited_sibling_deleted(tmp_path: Path) -> None:
    names = {
        ".coordinator-local/ledgers": "ask-20260101T000000-aaaaaa",
        "docs": "warp-2026-01-02-some-slug-aaaaaa-20260102T000000-bbbbbb",
        "state/handoffs": "ask-20260103T000000-cccccc",
    }
    runs = {h: _run(tmp_path, n, outcome_age_days=30) for h, n in names.items()}
    for h, n in names.items():
        d = tmp_path / h
        d.mkdir(parents=True)
        (d / "note.md").write_text(f"see state/warp/{n}/outcome.json for evidence\n")
    gone = _run(tmp_path, "ask-20260104T000000-dddddd", outcome_age_days=30)
    _, n = _sweep(tmp_path)
    assert n == 1
    assert not gone.exists()
    assert all(r.exists() for r in runs.values())


def test_citation_in_unnamed_home_does_not_protect(tmp_path: Path) -> None:
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "n.md").write_text("ask-20260101T000000-aaaaaa")
    _, n = _sweep(tmp_path)
    assert n == 1 and not run.exists()


def test_git_failure_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    monkeypatch.setattr(cruft_sweep, "_tracked_warp_run_names", lambda root: None)
    assert _sweep(tmp_path) == (0, 0)
    assert run.exists()


def test_dry_run_counts_without_deleting(tmp_path: Path) -> None:
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    _, n = _sweep(tmp_path, dry_run=True)
    assert n == 1 and run.exists()


def test_no_git_spawn_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        _run(tmp_path, f"ask-20260101T00000{i}-aaaaa{i}", outcome_age_days=30)
    calls = []
    real = cruft_sweep.run_git
    monkeypatch.setattr(cruft_sweep, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    _, n = _sweep(tmp_path)
    assert n == 5 and len(calls) <= 1


def test_missing_warp_root_is_noop(tmp_path: Path) -> None:
    assert _sweep(tmp_path) == (0, 0)


def test_expired_watchdog_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    monkeypatch.setattr(cruft_sweep, "_cited_warp_run_ids", lambda *a, **k: None)
    assert _sweep(tmp_path) == (0, 0)
    assert run.exists()


def test_citation_followed_by_hyphenated_word_still_protects(tmp_path: Path) -> None:
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "n.md").write_text("see ask-20260101T000000-aaaaaa-notes here")
    assert _sweep(tmp_path) == (0, 0)
    assert run.exists()


def _blobs(run: Path) -> Path:
    blobs = run / "withheld-blobs"
    blobs.mkdir()
    (blobs / "a4cc").write_text("y" * 20)
    (run / "manifest.json").write_text("{}")
    return blobs


def test_terminal_run_drops_prompts_and_blobs_keeps_manifest_and_outcome(tmp_path: Path) -> None:
    old = _run(tmp_path, "old", outcome_age_days=8)
    blobs = _blobs(old)
    fresh = _run(tmp_path, "fresh", outcome_age_days=6)
    fresh_blobs = _blobs(fresh)
    b, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True)
    assert n == 2 and b >= 30
    assert not (old / "prompts").exists() and not blobs.exists()
    assert (old / "manifest.json").exists() and (old / RUN_OUTCOME_FILE).exists()
    assert (fresh / "prompts").exists() and fresh_blobs.exists()


def test_open_run_blobs_kept(tmp_path: Path) -> None:
    open_ = _run(tmp_path, "open")
    blobs = _blobs(open_)
    assert cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True) == (0, 0)
    assert blobs.exists() and (open_ / "prompts").exists()


def test_tracked_subdir_is_kept_untracked_sibling_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _run(tmp_path, "old", outcome_age_days=30)
    blobs = _blobs(old)
    monkeypatch.setattr(cruft_sweep, "_tracked_warp_subdirs", lambda root: {("old", "withheld-blobs")})
    _, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True)
    assert n == 1
    assert blobs.exists() and not (old / "prompts").exists()


def test_unreadable_index_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _run(tmp_path, "old", outcome_age_days=30)
    monkeypatch.setattr(cruft_sweep, "_tracked_warp_subdirs", lambda root: None)
    assert cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True) == (0, 0)
    assert (old / "prompts").exists()


def test_tracked_subdirs_come_from_the_index_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import git_state

    run = _run(tmp_path, "old", outcome_age_days=30)
    blobs = _blobs(run)
    monkeypatch.setattr(cruft_sweep, "git_dir", lambda root: str(tmp_path / ".git"))
    monkeypatch.setattr(
        git_state,
        "read_index_paths",
        lambda root: frozenset({"state/warp/old/withheld-blobs/a4cc", "state/warp/old/manifest.json", "src/x.py"}),
    )
    assert cruft_sweep._tracked_warp_subdirs(tmp_path) == {("old", "withheld-blobs")}
    _, n = cruft_sweep.sweep_warp_prompts(tmp_path, now=time.time(), dry_run=False, quiet=True)
    assert n == 1 and blobs.exists() and not (run / "prompts").exists()
