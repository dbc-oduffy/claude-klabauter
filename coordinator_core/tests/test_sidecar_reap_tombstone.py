"""sidecar_reap tombstones every sidecar it deletes, in both share homes, and spares live sessions."""

from __future__ import annotations

import io
import os
import time

from coordinator_core.review_trail.sweep_tombstone import swept_sidecars
from coordinator_core.sidecar_reap import reap_stale_sidecars

OLD = time.time() - 40 * 86400
HOMES = (".coordinator-local/subagent-share", "state/subagent-share")


def _seed(repo, sid):
    paths = []
    for home in HOMES:
        d = repo / home / sid
        d.mkdir(parents=True)
        f = d / "review.md"
        f.write_text("---\nstatus: complete\n---\n")
        os.utime(f, (OLD, OLD))
        paths.append(f)
    return paths


def test_dead_tombstoned_live_spared(tmp_path):
    repo = str(tmp_path)
    dead = _seed(tmp_path, "dead-sid")
    live = _seed(tmp_path, "live-sid")
    rc = reap_stale_sidecars(repo, lambda sid, cwd=None: sid == "live-sid", out=io.StringIO())
    assert rc == 0
    assert not any(p.exists() for p in dead)
    assert all(p.exists() for p in live)
    recs = swept_sidecars(repo, "dead-sid")
    assert {r["path"] for r in recs} == {f"{h}/dead-sid/review.md" for h in HOMES}
    assert all(r["sweeper"] == "sidecar_reap" for r in recs)
    assert swept_sidecars(repo, "live-sid") == ()


def test_dry_run_writes_no_tombstone(tmp_path):
    repo = str(tmp_path)
    dead = _seed(tmp_path, "dead-sid")
    reap_stale_sidecars(repo, lambda sid, cwd=None: False, dry_run=True, out=io.StringIO())
    assert all(p.exists() for p in dead)
    assert swept_sidecars(repo, "dead-sid") == ()


def test_cadence_reap_clears_dead_untracked_without_a_sweep_and_without_git(tmp_path, monkeypatch):
    from coordinator_core import sidecar_reap

    def _no_git(*a, **k):
        raise AssertionError("cadence reap must not spawn git")

    monkeypatch.setattr(sidecar_reap, "run_git", _no_git)
    repo = str(tmp_path)
    dead = _seed(tmp_path, "dead-sid")
    live = _seed(tmp_path, "live-sid")
    n = sidecar_reap.reap_untracked_if_due(repo, lambda sid, cwd=None: sid == "live-sid")
    assert n == 1
    assert not dead[0].exists()
    assert dead[1].exists()  # tracked-capable legacy root is left to the full sweep
    assert all(p.exists() for p in live)
    assert {r["path"] for r in swept_sidecars(repo, "dead-sid")} == {f"{HOMES[0]}/dead-sid/review.md"}


def test_cadence_reap_is_throttled_then_due_again(tmp_path):
    from coordinator_core import sidecar_reap

    repo = str(tmp_path)
    first = _seed(tmp_path, "dead-a")
    assert sidecar_reap.reap_untracked_if_due(repo, lambda sid, cwd=None: False) == 1
    second = _seed(tmp_path, "dead-b")
    assert sidecar_reap.reap_untracked_if_due(repo, lambda sid, cwd=None: False) == 0
    assert second[0].exists()
    later = time.time() + sidecar_reap.REAP_INTERVAL_S + 1
    assert sidecar_reap.reap_untracked_if_due(repo, lambda sid, cwd=None: False, now=later) == 1
    assert not second[0].exists()
    assert not first[0].exists()


def test_cadence_reap_spares_young_and_blocked_and_honours_batch_cap(tmp_path):
    from coordinator_core import sidecar_reap

    repo = str(tmp_path)
    d = tmp_path / HOMES[0] / "dead-sid"
    d.mkdir(parents=True)
    young = d / "young.md"
    young.write_text("---\nstatus: complete\n---\n")
    blocked = d / "blocked.md"
    blocked.write_text("---\nstatus: blocked\n---\n")
    os.utime(blocked, (OLD, OLD))
    old = []
    for i in range(3):
        f = d / f"old{i}.md"
        f.write_text("---\nstatus: complete\n---\n")
        os.utime(f, (OLD, OLD))
        old.append(f)
    assert sidecar_reap.reap_untracked_if_due(repo, lambda sid, cwd=None: False, batch_cap=2) == 2
    assert young.exists() and blocked.exists()
    assert sum(f.exists() for f in old) == 1


def test_cadence_reap_with_no_share_root_is_a_noop(tmp_path):
    from coordinator_core import sidecar_reap

    assert sidecar_reap.reap_untracked_if_due(str(tmp_path), lambda sid, cwd=None: False) == 0


def test_subagent_start_hook_kicks_the_cadence_reap_detached_and_only_when_due(tmp_path, monkeypatch):
    import asyncio

    from coordinator_core import sidecar_reap
    from coordinator_core.hooks import cater_subagent_start as hook
    from coordinator_core.session import liveness

    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: False)
    (tmp_path / ".git").mkdir()
    dead = _seed(tmp_path, "dead-sid")

    async def _kick_and_drain():
        hook._kick_untracked_reap(str(tmp_path))
        for _ in range(200):
            if not hook._REAP_INFLIGHT:
                return
            await asyncio.sleep(0.01)

    asyncio.run(_kick_and_drain())
    assert not dead[0].exists()
    assert dead[1].exists()
    assert sidecar_reap.reap_is_due(str(tmp_path)) is False

    again = _seed(tmp_path, "dead-b")
    asyncio.run(_kick_and_drain())
    assert again[0].exists()  # stamped within the interval: not due, nothing started


def test_subagent_start_hook_without_cwd_never_reaps(monkeypatch):
    from coordinator_core.hooks import cater_subagent_start as hook

    monkeypatch.setattr(hook, "_show_toplevel_no_spawn", lambda cwd: (_ for _ in ()).throw(AssertionError("walked")))
    hook._kick_untracked_reap(None)
    assert not hook._REAP_INFLIGHT
