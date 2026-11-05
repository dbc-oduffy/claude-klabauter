"""sweep_warp_runs: a closed, untracked, uncited run dir past retention is deleted whole."""
from __future__ import annotations

import os
import subprocess
import time

from coordinator_core.ops import cruft_sweep
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_OUTCOME_FILE
from coordinator_core.win_portability import no_console_creationflags

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


def _sweep(root, dry_run=False):
    return cruft_sweep.sweep_warp_runs(root, now=time.time(), dry_run=dry_run, quiet=True)


def test_old_closed_uncited_run_deleted_open_and_fresh_kept(tmp_path):
    old = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=8)
    fresh = _run(tmp_path, "ask-20260102T000000-bbbbbb", outcome_age_days=6)
    open_ = _run(tmp_path, "ask-20260103T000000-cccccc")
    b, n = _sweep(tmp_path)
    assert n == 1 and b >= 10
    assert not old.exists()
    assert fresh.exists() and open_.exists()


def test_cited_run_kept_from_each_home_uncited_sibling_deleted(tmp_path):
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


def test_citation_in_unnamed_home_does_not_protect(tmp_path):
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "n.md").write_text("ask-20260101T000000-aaaaaa")
    _, n = _sweep(tmp_path)
    assert n == 1 and not run.exists()


def test_tracked_run_dir_kept(tmp_path):
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


def test_git_failure_deletes_nothing(tmp_path, monkeypatch):
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    monkeypatch.setattr(cruft_sweep, "_tracked_warp_run_names", lambda root: None)
    assert _sweep(tmp_path) == (0, 0)
    assert run.exists()


def test_dry_run_counts_without_deleting(tmp_path):
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    _, n = _sweep(tmp_path, dry_run=True)
    assert n == 1 and run.exists()


def test_no_git_spawn_per_run(tmp_path, monkeypatch):
    for i in range(5):
        _run(tmp_path, f"ask-20260101T00000{i}-aaaaa{i}", outcome_age_days=30)
    calls = []
    real = cruft_sweep.run_git
    monkeypatch.setattr(cruft_sweep, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    _, n = _sweep(tmp_path)
    assert n == 5 and len(calls) <= 1


def test_missing_warp_root_is_noop(tmp_path):
    assert _sweep(tmp_path) == (0, 0)


def test_expired_watchdog_deletes_nothing(tmp_path, monkeypatch):
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    monkeypatch.setattr(cruft_sweep, "_cited_warp_run_ids", lambda *a, **k: None)
    assert _sweep(tmp_path) == (0, 0)
    assert run.exists()


def test_citation_followed_by_hyphenated_word_still_protects(tmp_path):
    run = _run(tmp_path, "ask-20260101T000000-aaaaaa", outcome_age_days=30)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "n.md").write_text("see ask-20260101T000000-aaaaaa-notes here")
    assert _sweep(tmp_path) == (0, 0)
    assert run.exists()
