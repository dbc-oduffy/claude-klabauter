"""``_rm_peer_claim_of``'s uncommitted-delta gate (2026-09-20).

THE DEFECT. A touch record SURVIVES the commit that lands the edit --
``touch_record.project_live_claims`` answers "has this session ever touched
this path" (last-verb-wins TOUCH from a still-live session), never "does it
still have an uncommitted delta here right now". Unfiltered, a live peer
session goes on contesting every path it EVER touched, including one it
already committed cleanly -- and a DIRECTORY target compounds it: one dirty
sibling file is enough to reach the peer-claim scan, whose containment match
then treats every OTHER already-committed claimed path under that same
directory as still contested too. That makes "ask the holder to release,
then commit" a non-terminating loop while the holder stays alive: the held
set never shrinks on its own, even after the holder's own commits land.

THE FIX. ``_rm_peer_claim_of`` now additionally requires the matched claimed
path to carry a CURRENT uncommitted delta (``_new_dirty_paths_memo`` -- one
whole-tree ``git status --porcelain`` per root, resolved once per guard
invocation and shared across every target/candidate a caller's loop checks,
never spawned per item). A claim on a path with no outstanding delta right
now no longer contests a peer's `rm`/`git clean`/`git reset --hard` etc.

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py
(``_rm_peer_claim_of``, ``_new_dirty_paths_memo``, ``_dirty_paths_from_porcelain``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external `git` process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags()
    )


def _init_repo(tmp_path: Path) -> str:
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _claim(root: str, sid: str, path: str) -> None:
    """Record ``path`` as TOUCHed by ``sid`` through the canonical writer,
    mirroring ``test_check5_foreign_hunk.py``'s own ``_claim`` helper."""
    from coordinator_core.session import touch_record

    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sdir),
        session_id=sid,
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path=path,
    )


class TestUncommittedDeltaGate:
    def test_touched_then_committed_path_is_not_a_live_claim(self, tmp_path, monkeypatch):
        """A peer TOUCHed and then committed ``a.py`` cleanly; ``b.py`` in the
        same directory is uncommitted (so `rm -r work/` still reaches the
        dirty-work branch and the peer-claim scan). ``a.py`` must no longer
        contest the removal -- it carries no delta right now."""
        root = _init_repo(tmp_path)
        peer_sid = "peer-sess"
        assert core.init(peer_sid, cwd=root)

        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("peer's committed content\n", encoding="utf-8")
        (work / "b.py").write_text("still dirty\n", encoding="utf-8")
        _git(root, "add", "work/a.py")
        _git(root, "commit", "-q", "-m", "peer lands a.py")
        _claim(root, peer_sid, "work/a.py")

        monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")
        monkeypatch.chdir(root)
        result = dc.check_destructive_rm(
            "rm -r work", session_id="my-sess", payload={}
        )
        assert result is None, (
            "an already-committed claim must not block a peer: %r" % (result,)
        )

    def test_touched_and_still_dirty_path_is_a_live_claim(self, tmp_path, monkeypatch):
        """Same shape, except the peer's claimed path is STILL uncommitted --
        this MUST still deny, unconditionally (not overridable via
        ``COORDINATOR_ALLOW_RM``): a genuinely live claim must keep blocking."""
        root = _init_repo(tmp_path)
        peer_sid = "peer-sess"
        assert core.init(peer_sid, cwd=root)

        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("peer's uncommitted content\n", encoding="utf-8")
        _claim(root, peer_sid, "work/a.py")

        monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")
        monkeypatch.chdir(root)
        result = dc.check_destructive_rm(
            "rm -r work", session_id="my-sess", payload={}
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert peer_sid in out["permissionDecisionReason"]

    def test_degraded_status_probe_still_blocks(self, tmp_path, monkeypatch):
        """FAIL SAFE: the whole-tree ``git status --porcelain`` probe this
        gate depends on fails outright -- the pre-existing (unfiltered)
        contested verdict must stand, never read an unreadable tree as
        clean. Only the exact no-pathspec status call is broken; every other
        `_run_git` call in the guard's ladder (root resolution, the
        per-target dirty-work probe) still succeeds."""
        root = _init_repo(tmp_path)
        peer_sid = "peer-sess"
        assert core.init(peer_sid, cwd=root)

        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("peer's committed content\n", encoding="utf-8")
        (work / "b.py").write_text("still dirty\n", encoding="utf-8")
        _git(root, "add", "work/a.py")
        _git(root, "commit", "-q", "-m", "peer lands a.py")
        _claim(root, peer_sid, "work/a.py")

        original = dc._run_git

        def _flaky(args, *a, **k):
            if list(args) == ["--no-optional-locks", "status", "--porcelain", "--untracked-files=all"]:
                return 1, ""
            return original(args, *a, **k)

        monkeypatch.setattr(dc, "_run_git", _flaky)
        monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")
        monkeypatch.chdir(root)
        result = dc.check_destructive_rm(
            "rm -r work", session_id="my-sess", payload={}
        )
        assert result is not None, (
            "a failed uncommitted-delta probe must fail SAFE (still contested), "
            "not fail open"
        )
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert peer_sid in out["permissionDecisionReason"]

    def test_dirty_paths_lookup_is_shared_across_targets_one_status_spawn(
        self, tmp_path, monkeypatch
    ):
        """Amplification gate (DR-344 / `test_no_unbatched_per_item_git_spawn.py`):
        the no-pathspec `git status --porcelain` probe this gate adds must cost
        ONE spawn for the whole `rm` command, never one per target."""
        root = _init_repo(tmp_path)
        peer_sid = "peer-sess"
        assert core.init(peer_sid, cwd=root)

        work = tmp_path / "work"
        work.mkdir()
        targets = []
        for i in range(3):
            f = work / f"f{i}.py"
            f.write_text("dirty %d\n" % i, encoding="utf-8")
            targets.append(str(f))
            _claim(root, peer_sid, "work/f%d.py" % i)

        calls = []
        original = dc._run_git

        def _counting(args, *a, **k):
            if list(args) == ["--no-optional-locks", "status", "--porcelain", "--untracked-files=all"]:
                calls.append(tuple(args))
            return original(args, *a, **k)

        monkeypatch.setattr(dc, "_run_git", _counting)
        monkeypatch.chdir(root)
        dc.check_destructive_rm("rm " + " ".join(targets), session_id="my-sess", payload={})

        assert len(calls) <= 1, (
            "the uncommitted-delta probe spawned once per target instead of "
            "once for the whole call: %r" % (calls,)
        )
