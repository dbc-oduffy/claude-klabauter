"""
coordinator_core.ops.tests.test_completion_ops_concurrency — cross-process
lost-update proof for the completion ops themselves (D2d).

D2b adopted ``locked_write.locked_rmw`` inside ``reconcile_completion_commits``
and ``append_plan_session`` (see their "Locking" docstring sections). Neither op
had a single concurrency test before this module — every one of the ~60
pre-existing tests in ``test_completion_ops_reconcile.py`` is single-process and
sequential. This module proves the OP-level property, not the primitive: two
concurrent op invocations against the SAME ``docs/plans``/``archive/completed``
noun must not lose an update, and a genuinely-unavailable lock must surface as
``LockTimeout``, not a swallowed/generic traceback.

Technique mirrors ``coordinator_core/tests/test_locked_write.py::
TestCrossProcessLostUpdateProof`` (barrier-synchronised subprocesses), lifted one
layer up to the op functions (``reconcile_completion_commits``,
``append_plan_session``) instead of the bare ``locked_rmw`` primitive:

  - ``TestCrossProcessBothOpsPersist.test_without_lock_one_update_is_lost`` —
    the regression guard. It monkeypatches ``coordinator_core.locked_write.
    locked_rmw`` to a naive, unlocked read-modify-write (barrier-synchronised so
    both processes provably read the SAME pre-race text before either writes),
    then runs ``completion.reconcile_commits`` and ``plan.append_session``
    concurrently against one file. This is the exact pairing D2b's docstring
    calls out as racing in production today. It fails to lose an update only if
    the mocked-out locking is bypassed — i.e. it is discriminating BECAUSE it
    proves the window exists, not because it always passes.
  - ``test_with_locked_rmw_both_updates_persist`` — same pairing, same file,
    real (unpatched) ``locked_rmw``, both mutate closures artificially slowed
    (via a monkeypatched ``_apply_commits_fold``/``_apply_session_append``) to
    widen the critical section deterministically instead of relying on a bare
    sleep-and-hope race. Both mutations must persist.
  - ``TestLockTimeoutSurfaces`` — an external holder process pins the file's
    lock sidecar; the op call (with a monkeypatched short timeout) must raise
    ``coordinator_core.locked_write.LockTimeout``, not hang or generic-except.

Spec backlink: chunk D2d of the D2 wave (see docs/plans D2b's "Locking" docstring
sections in coordinator_core/ops/completion_ops.py for the property under test).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.completion_ops import (
    append_plan_session,
    reconcile_completion_commits,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tmp git repo with identity configured — the noun for the locking key."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "concurrency-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Concurrency Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: seed repo")
    return root


def _write_entry(root: Path, rel_path: str) -> Path:
    """A minimal completion entry: empty ``commits:`` list, no ``agent_sessions:``
    block yet — the two ops under test each own a disjoint region of this file
    (``commits:`` vs an ``agent_sessions:`` block inserted before the closing
    fence), so a correct concurrent run must leave BOTH regions populated.
    """
    content = "---\nstatus: pending-release\ncommits: []\n---\nbody\n"
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Subprocess race scripts
# ---------------------------------------------------------------------------

# UNSAFE: monkeypatches locked_write.locked_rmw itself to a naive, unlocked
# read-modify-write, barrier-synchronised so both processes provably read the
# SAME pre-race content before either writes -- deterministic lost update,
# not sleep-and-hope. This is the regression guard: it demonstrates the exact
# hazard D2b's lock closes.
_UNSAFE_RACE_SCRIPT = textwrap.dedent("""\
    import os, sys, time
    from pathlib import Path

    op, target, repo_root, project_root, barrier_dir, delay = sys.argv[1:7]
    sys.path.insert(0, project_root)
    barrier_dir = Path(barrier_dir)
    delay = float(delay)

    import coordinator_core.locked_write as lw

    def _naive_locked_rmw(target_path, mutate, *, repo_root=None, timeout=None,
                           missing_ok=False):
        p = Path(target_path)
        if p.exists():
            old_text = p.read_text(encoding="utf-8")
        elif missing_ok:
            old_text = ""
        else:
            raise FileNotFoundError(str(p))

        new_text = mutate(old_text)

        (barrier_dir / f"ready.{os.getpid()}").touch()
        deadline = time.monotonic() + 10.0
        while len(list(barrier_dir.glob("ready.*"))) < 2:
            if time.monotonic() > deadline:
                raise RuntimeError("barrier timeout: peer did not start in time")
            time.sleep(0.005)

        time.sleep(delay)
        if new_text != old_text:
            p.write_text(new_text, encoding="utf-8")
        return new_text

    lw.locked_rmw = _naive_locked_rmw

    if op == "reconcile":
        from coordinator_core.ops.completion_ops import reconcile_completion_commits
        reconcile_completion_commits(
            target, commits=["deadbeef"], worktree_root=repo_root
        )
    else:
        from coordinator_core.ops.completion_ops import append_plan_session
        append_plan_session(
            target, session_id="sess-race", status="working",
            created_at="2026-08-06T00:00:00Z",
        )
""")

# SAFE: real (unpatched) locked_rmw. Widens the critical section deterministically
# by slowing the mutate transform itself -- still inside the lock -- instead of a
# bare sleep race, mirroring test_locked_write.py's sleep-inside-mutate technique.
_SAFE_RACE_SCRIPT = textwrap.dedent("""\
    import sys, time

    op, target, repo_root, project_root, delay = sys.argv[1:6]
    sys.path.insert(0, project_root)
    delay = float(delay)

    import coordinator_core.ops.completion_ops as co

    if op == "reconcile":
        _orig_fold = co._apply_commits_fold
        def _slow_fold(old_text, new_shas):
            time.sleep(delay)
            return _orig_fold(old_text, new_shas)
        co._apply_commits_fold = _slow_fold
        co.reconcile_completion_commits(
            target, commits=["deadbeef"], worktree_root=repo_root
        )
    else:
        _orig_append = co._apply_session_append
        def _slow_append(content, session_id, status, created_at):
            time.sleep(delay)
            return _orig_append(content, session_id, status, created_at)
        co._apply_session_append = _slow_append
        co.append_plan_session(
            target, session_id="sess-race", status="working",
            created_at="2026-08-06T00:00:00Z",
        )
""")


class TestCrossProcessBothOpsPersist:
    """completion.reconcile_commits and plan.append_session racing on the SAME
    docs/plans-shaped noun -- the real-world pairing D2b's docstring names.
    """

    def test_without_lock_one_update_is_lost(self, repo: Path, tmp_path: Path) -> None:
        """Regression guard: with locking bypassed, both processes read the SAME
        pre-race text (barrier-enforced) and whichever writes last wins wholesale
        -- the OTHER op's disjoint-region edit is silently discarded. This must
        FAIL (i.e. show only one mutation survives) precisely because it exercises
        the pre-D2b hazard; a test that passed regardless of locking would prove
        nothing.
        """
        entry = _write_entry(repo, "archive/completed/entry.md")
        barrier_dir = tmp_path / "barrier"
        barrier_dir.mkdir()

        script = tmp_path / "unsafe_race.py"
        script.write_text(_UNSAFE_RACE_SCRIPT, encoding="utf-8")

        p1 = subprocess.Popen(
            [sys.executable, str(script), "reconcile", str(entry), str(repo),
             _PROJECT_ROOT, str(barrier_dir), "0.05"],
            creationflags=_NO_WINDOW,
        )
        p2 = subprocess.Popen(
            [sys.executable, str(script), "append", str(entry), str(repo),
             _PROJECT_ROOT, str(barrier_dir), "0.05"],
            creationflags=_NO_WINDOW,
        )
        p1.wait(timeout=30)
        p2.wait(timeout=30)

        final_text = entry.read_text(encoding="utf-8")
        commit_landed = "deadbeef" in final_text
        session_landed = "sess-race" in final_text

        assert commit_landed != session_landed, (
            "Expected exactly one mutation to survive the unlocked race "
            f"(deterministic lost update) -- commit_landed={commit_landed}, "
            f"session_landed={session_landed}, final_text={final_text!r}"
        )

    def test_with_locked_rmw_both_updates_persist(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Real locked_rmw: both racing mutations must persist -- proves D2b's
        lock adoption actually closes the window the previous test opens.
        """
        entry = _write_entry(repo, "archive/completed/entry.md")

        script = tmp_path / "safe_race.py"
        script.write_text(_SAFE_RACE_SCRIPT, encoding="utf-8")

        p1 = subprocess.Popen(
            [sys.executable, str(script), "reconcile", str(entry), str(repo),
             _PROJECT_ROOT, "0.2"],
            creationflags=_NO_WINDOW,
        )
        p2 = subprocess.Popen(
            [sys.executable, str(script), "append", str(entry), str(repo),
             _PROJECT_ROOT, "0.2"],
            creationflags=_NO_WINDOW,
        )
        p1.wait(timeout=30)
        p2.wait(timeout=30)

        final_text = entry.read_text(encoding="utf-8")
        assert "deadbeef" in final_text, (
            f"reconcile_completion_commits's fold was lost under concurrency: "
            f"{final_text!r}"
        )
        assert "sess-race" in final_text, (
            f"append_plan_session's entry was lost under concurrency: {final_text!r}"
        )


# ---------------------------------------------------------------------------
# LockTimeout surfaces as the op's own diagnostic
# ---------------------------------------------------------------------------

_HOLDER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    sys.path.insert(0, sys.argv[3])
    from coordinator_core.locked_write import _plat_try_lock, _plat_unlock

    lock_path = sys.argv[1]
    hold_secs = float(sys.argv[2])

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert _plat_try_lock(fd), "holder could not acquire the lock"
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    time.sleep(hold_secs)
    _plat_unlock(fd)
    os.close(fd)
""")


class TestLockTimeoutSurfaces:
    def test_reconcile_raises_lock_timeout_when_lock_held(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A holder process pins the file's lock sidecar; the op call must raise
        the named LockTimeout diagnostic (D2b's own type), not hang for the
        default 10 s or surface a generic/unhandled traceback.
        """
        entry = _write_entry(repo, "archive/completed/entry.md")

        import hashlib
        import os

        from coordinator_core.lifecycle import git_common_dir

        key = hashlib.sha1(os.path.realpath(str(entry)).encode()).hexdigest()
        lock_dir = git_common_dir(repo) / "coordinator-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{key}.lock"

        holder_script = tmp_path / "holder.py"
        holder_script.write_text(_HOLDER_SCRIPT, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(holder_script), str(lock_path), "5", _PROJECT_ROOT],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        try:
            line = proc.stdout.readline()
            assert line.strip() == b"LOCKED"

            import coordinator_core.locked_write as lw

            _real_locked_rmw = lw.locked_rmw

            def _short_timeout_locked_rmw(*args, **kwargs):
                kwargs.setdefault("timeout", 0.3)
                return _real_locked_rmw(*args, **kwargs)

            monkeypatch.setattr(lw, "locked_rmw", _short_timeout_locked_rmw)

            with pytest.raises(LockTimeout):
                reconcile_completion_commits(
                    str(entry), commits=["deadbeef"], worktree_root=str(repo)
                )
        finally:
            proc.kill()
            proc.wait()
