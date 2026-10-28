"""klab-59 end-to-end: sidecar sweeps spare live sessions, tombstone what they delete,
and the close receipt gate reports a swept review as swept (still blocking).

Drives the real entry points at default arguments: `reap_stale_sidecars` and
`prune` (roots limited to the tmp repo's own spaces so no real Temp is touched).
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest

from coordinator_core import sidecar_reap
from coordinator_core.ops import prune_claude_temp
from coordinator_core.review_trail.sweep_tombstone import swept_sidecars
from coordinator_core.session import liveness
from coordinator_core.workstream_complete import _compute_review_receipt_gate

LIVE = "11111111-live-peer"
DEAD = "22222222-dead-peer"
CALLER = "33333333-caller"
OLD_DAYS = 40


def _homes(repo: Path) -> dict:
    return {
        "legacy": repo / "state" / "subagent-share",
        "current": repo / ".coordinator-local" / "subagent-share",
    }


def _seed(repo: Path, sid: str, name: str, age_days: float) -> Path:
    paths = []
    for home in _homes(repo).values():
        d = home / sid
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(f"---\nagent_type: reviewer\nstatus: complete\n---\nbody {sid}\n", encoding="utf-8")
        t = time.time() - age_days * 86400
        os.utime(f, (t, t))
        os.utime(d, (t, t))
        paths.append(f)
    return paths


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".gitignore").write_text(".coordinator-local/subagent-share\n", encoding="utf-8")
    (r / ".coordinator-local").mkdir()
    (r / ".git").mkdir()
    (r / ".git" / "index").write_bytes(b"DIRC")
    live_ids = {LIVE, CALLER}
    pred = lambda sid, cwd=None, **_: sid in live_ids  # noqa: E731
    monkeypatch.setattr(liveness, "session_live", pred)
    return r


def _prune(repo: Path) -> int:
    roots = [t for t in prune_claude_temp.default_roots(str(repo)) if str(t[0]).startswith(str(repo))]
    assert any(str(t[0]).endswith("subagent-share") for t in roots), "share home not swept: gitignore predicate"
    return prune_claude_temp.prune(session_id=CALLER, roots=roots, repo_root=str(repo), drain=True)


def _reap(repo: Path) -> str:
    out = io.StringIO()
    assert sidecar_reap.reap_stale_sidecars(str(repo), liveness.session_live, out=out) == 0
    return out.getvalue()


def _gate(repo: Path, sid: str):
    return _compute_review_receipt_gate(repo, sid, "implemented", None, None)


def test_reap_then_prune_spare_live_and_tombstone_dead(repo):
    live = _seed(repo, LIVE, "review.md", OLD_DAYS)
    dead = _seed(repo, DEAD, "review.md", OLD_DAYS)
    fresh_live = _seed(repo, LIVE, "fresh.md", 0.01)
    fresh_dead = _seed(repo, DEAD, "fresh-dead.md", 0.01)

    _reap(repo)
    _prune(repo)

    for f in live + fresh_live:
        assert f.exists(), f"live peer sidecar swept: {f}"
    assert not any(f.exists() for f in dead), "dead peer's old sidecars survived"
    # A fresh sidecar is younger than both age floors, so neither sweeper may take it.
    assert all(f.exists() for f in fresh_dead)

    swept = {r["path"] for r in swept_sidecars(str(repo), DEAD)}
    for home in ("state/subagent-share", ".coordinator-local/subagent-share"):
        assert f"{home}/{DEAD}/review.md" in swept
    assert swept_sidecars(str(repo), LIVE) == ()


def test_prune_alone_spares_live_idle_dir_and_tombstones_dead(repo):
    live = _seed(repo, LIVE, "review.md", OLD_DAYS)
    dead = _seed(repo, DEAD, "review.md", OLD_DAYS)

    _prune(repo)

    cur_live = repo / ".coordinator-local" / "subagent-share" / LIVE / "review.md"
    cur_dead = repo / ".coordinator-local" / "subagent-share" / DEAD
    assert cur_live.exists()
    assert not cur_dead.exists()
    assert (repo / "state" / "subagent-share" / LIVE / "review.md") in live
    recs = swept_sidecars(str(repo), DEAD)
    assert recs and {r["sweeper"] for r in recs} == {"prune_claude_temp"}
    assert swept_sidecars(str(repo), LIVE) == ()
    # Legacy-home copy is not prune's remit; reap owns it.
    assert all(f.exists() for f in dead if "state" in f.parts and "subagent-share" in f.parts)


def test_gate_reports_swept_for_dead_and_unrun_for_never_seen(repo):
    _seed(repo, LIVE, "review.md", OLD_DAYS)
    _seed(repo, DEAD, "review.md", OLD_DAYS)

    _reap(repo)
    _prune(repo)

    gate = _gate(repo, DEAD)
    assert gate.blocks is True, "a swept review must never credit (DR-421)"
    assert "swept by" in gate.detail

    unrun = _gate(repo, "44444444-never-ran")
    assert unrun.blocks is True
    assert "swept by" not in unrun.detail
    assert "no counting review receipt" in unrun.detail


def test_review_reverify_revert_never_takes_a_share_home_sidecar(repo, monkeypatch):
    """Revert candidates skip both machinery homes, so a fresh live sidecar is not a created path."""
    from coordinator_core.ops import review_reverify
    from coordinator_core.ops.dispatch_emit import review_edits

    fresh = _seed(repo, LIVE, "fresh.md", 0.001)
    rels = [str(p.relative_to(repo)) for p in fresh]
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {r: "agent-x" for r in rels})
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *a, **k: {})
    share = repo / ".coordinator-local" / "subagent-share" / LIVE / "snap.pre-review"
    manifest = {"files": [], "recorded_at": None, "base": None}

    created, foreign = review_reverify._created(repo, LIVE, manifest, share)

    assert created == [] and foreign == []
    assert all(p.exists() for p in fresh)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_git_restore_checkout_leave_ignored_legacy_sidecar_alone(tmp_path):
    """The legacy home is gitignored and untracked, so restore/checkout/reset cannot delete from it."""
    import subprocess

    r = tmp_path / "g"
    r.mkdir()

    def git(*a):
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                       check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    git("init", "-q")
    (r / ".gitignore").write_text("state/subagent-share/\n", encoding="utf-8")
    (r / "tracked.txt").write_text("x", encoding="utf-8")
    git("add", ".gitignore", "tracked.txt")
    git("commit", "-q", "-m", "base")
    f = _seed(r, LIVE, "fresh.md", 0.001)[0]
    assert f.parts[-4:-2] == ("state", "subagent-share")
    git("checkout", "-q", "--", ".")
    git("restore", "--source=HEAD", "--staged", "--worktree", ".")
    git("checkout", "-q", "-f", "HEAD")
    git("reset", "-q", "--hard")
    assert f.exists()
