"""The submit hook's whole emit stays under the 500ms process-time bar on the real `dispatch.emit`."""

from __future__ import annotations

import time

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

pytestmark = [pytest.mark.cadence]

BAR_S = 0.5
repo = real.repo


def test_listing_scope_lists_each_launch_dir_once(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    (tmp_path / "workflow-run-sess-a.json").write_text("{}", encoding="utf-8")
    calls = []
    real_listdir = warp_live.os.listdir
    monkeypatch.setattr(warp_live.os, "listdir", lambda p: calls.append(p) or real_listdir(p))
    dirs = [str(tmp_path)]
    with warp_live.listing_scope():
        for session in ("sess", "other", "sess"):
            warp_live.LaunchSnapshot(session, dirs)
    assert len(calls) == 1
    warp_live.LaunchSnapshot("sess", dirs)
    warp_live.LaunchSnapshot("sess", dirs)
    assert len(calls) == 3  # outside a scope every snapshot lists afresh


def test_sizing_submission_emit_process_time_is_under_the_bar(repo):
    real._put_sizing(repo)
    payload = real._payload(repo, {"sizing": real.REL})
    mod.evaluate(payload)  # warm the imports; the bar is the steady-state emit
    (run_dir,) = real._run_dirs(repo)
    for p in sorted(run_dir.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    run_dir.rmdir()

    start = time.process_time()
    env = mod.evaluate(payload)
    spent = time.process_time() - start

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert spent < BAR_S, f"submit hook emit took {spent:.3f}s process time (bar {BAR_S}s)"
