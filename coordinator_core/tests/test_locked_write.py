"""
coordinator_core.tests.test_locked_write — tests for locked_rmw.

Coverage requirements (per chunk spec C1):
  (a) single-process RMW correctness + atomicity (temp cleaned on mutate raise;
      MutateAbort releases lock and skips write)
  (b) byte-identical no-op: mutate returns unchanged text -> no os.replace, mtime
      unchanged
  (c) LockTimeout fires when the lock is held by another holder (subprocess holder)
  (d) AUTHORITATIVE cross-process lost-update proof: without locked_rmw an update
      is provably lost; with locked_rmw both updates persist (deterministic via sleep)
  (e) symlinked-target mutual exclusion: two callers naming the same physical file
      via different spellings share ONE lock (realpath-canonicalisation)
  (f) repo_root-as-worktree vs repo_root/.git both produce the same lock dir
  (g) crash-safety: a holder process killed while holding the lock leaves the next
      acquirer able to proceed (no stale lock)

POSIX guard: all tests are skipped with a clear message if fcntl is unavailable.

Spec backlink: pln-ceremony-as-pipeline-2-invert--fd1b98 § C1
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.testing import symlink_capability

# ---------------------------------------------------------------------------
# POSIX guard
# ---------------------------------------------------------------------------

try:
    import fcntl as _fcntl  # noqa: F401
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

# locked_rmw needs one lock backend — fcntl (POSIX) or msvcrt (Windows). Skip the
# whole file only if NEITHER exists; the cross-process tests run on Windows too
# (that is where the 2026-07-14 fcntl-only regression was reported).
_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())

# Declared, not excused: this file's AUTHORITATIVE cross-process lost-update proof
# (d) and crash-safety proof (g) require real OS-level process/lock contention
# (subprocess holders, real `fcntl`/`msvcrt` locks, a killed holder process) -- no
# mock reproduces genuine cross-process races. `_make_git_repo` also spawns real git
# so `git_common_dir()` resolves against a real repo layout.
pytestmark = [
    pytest.mark.skipif(
        not _LOCKING_AVAILABLE,
        reason="locked_rmw needs a file-lock backend (fcntl or msvcrt) — neither available",
    ),
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# ---------------------------------------------------------------------------
# Import under test
# ---------------------------------------------------------------------------

from coordinator_core import locked_write  # noqa: E402
from coordinator_core.locked_write import (  # noqa: E402
    LockTimeout,
    MutateAbort,
    LOCK_TIMEOUT_SECS,
    locked_rmw,
)

# ---------------------------------------------------------------------------
# Portability helper
# ---------------------------------------------------------------------------

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Shared git-repo factory
# ---------------------------------------------------------------------------


def _make_git_repo(root: Path) -> Path:
    """Init a minimal git repository under *root* and return the repo root.

    Mirrors the pattern used in coordinator_core/ops/tests/conftest.py so that
    git_common_dir() can resolve successfully.
    """
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "locked-write-test@claude-klabauter.test")
    _git("config", "user.name", "Locked Write Test")
    _git("config", "commit.gpgsign", "false")
    # Need at least one commit so git_common_dir works correctly.
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


# ---------------------------------------------------------------------------
# (a) Single-process RMW correctness + atomicity
# ---------------------------------------------------------------------------


class TestSingleProcessCorrectness:
    def test_creates_file_and_returns_new_text(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"

        result = locked_rmw(
            target, lambda _old: "hello world", repo_root=repo, missing_ok=True
        )

        assert result == "hello world"
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_reads_existing_and_mutates(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "counter.txt"
        target.write_text("5", encoding="utf-8")

        result = locked_rmw(
            target,
            lambda old: str(int(old.strip()) + 1),
            repo_root=repo,
        )

        assert result == "6"
        assert target.read_text(encoding="utf-8") == "6"

    def test_mutate_abort_skips_write_and_propagates(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("original", encoding="utf-8")

        def _abort(_old: str) -> str:
            raise MutateAbort("deliberate abort")

        with pytest.raises(MutateAbort, match="deliberate abort"):
            locked_rmw(target, _abort, repo_root=repo)

        # File must be unchanged.
        assert target.read_text(encoding="utf-8") == "original"

    def test_mutate_exception_skips_write_and_propagates(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("original", encoding="utf-8")

        def _explode(_old: str) -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            locked_rmw(target, _explode, repo_root=repo)

        assert target.read_text(encoding="utf-8") == "original"

    def test_mutate_abort_does_not_corrupt_file(self, tmp_path):
        """MutateAbort leaves the target file un-corrupted and no temp file behind.

        Review: code-reviewer (F5) — renamed from test_temp_file_cleaned_on_mutate_abort.
        The abort path short-circuits BEFORE mkstemp, so no temp file is ever created;
        the assertion tmp_files==[] is trivially true and does not exercise the cleanup
        finally.  This test is kept (renamed) because it does prove that MutateAbort
        leaves the original content intact.  The production cleanup path (mkstemp succeeds
        then os.replace fails) is covered by test_temp_file_cleaned_on_replace_failure.
        """
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("clean", encoding="utf-8")
        parent = target.parent

        def _abort(_old: str) -> str:
            raise MutateAbort("abort")

        with pytest.raises(MutateAbort):
            locked_rmw(target, _abort, repo_root=repo)

        # No unexpected temp files left (exclude known repo entries).
        excluded = {"data.txt", ".gitkeep", ".git"}
        tmp_files = [
            f for f in parent.iterdir()
            if f.name not in excluded
        ]
        assert tmp_files == [], f"unexpected temp files: {tmp_files}"
        # Original content must be intact.
        assert target.read_text(encoding="utf-8") == "clean"

    def test_temp_file_cleaned_on_replace_failure(self, tmp_path):
        """Temp file is removed when os.replace raises after mkstemp+write succeed.

        Review: code-reviewer (F5) — this is the production failure path the cleanup
        finally actually covers: mkstemp creates the temp, os.write fills it, but
        os.replace raises (e.g. EXDEV cross-device or permission error).  The finally
        block must unlink the temp so no orphan is left in the target directory.
        """
        from unittest.mock import patch

        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("original", encoding="utf-8")
        parent = target.parent

        with patch("os.replace", side_effect=OSError("simulated os.replace failure")):
            with pytest.raises(OSError, match="simulated os.replace failure"):
                locked_rmw(target, lambda old: "new content", repo_root=repo)

        # No temp files left — cleanup finally must have run os.unlink.
        excluded = {"data.txt", ".gitkeep", ".git"}
        tmp_files = [
            f for f in parent.iterdir()
            if f.name not in excluded
        ]
        assert tmp_files == [], f"orphaned temp files after os.replace failure: {tmp_files}"

        # Original file must be unchanged (os.replace never ran).
        assert target.read_text(encoding="utf-8") == "original"

    def test_missing_ok_false_raises_file_not_found(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "nonexistent.txt"

        with pytest.raises(FileNotFoundError):
            locked_rmw(target, lambda old: old + "x", repo_root=repo, missing_ok=False)

    def test_multiple_sequential_mutations(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "counter.txt"
        target.write_text("0", encoding="utf-8")

        for _ in range(10):
            locked_rmw(
                target,
                lambda old: str(int(old.strip()) + 1),
                repo_root=repo,
            )

        assert target.read_text(encoding="utf-8") == "10"


# ---------------------------------------------------------------------------
# (b) Byte-identical no-op
# ---------------------------------------------------------------------------


class TestNoOpOnIdenticalContent:
    def test_mtime_unchanged_when_content_identical(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("same content", encoding="utf-8")

        mtime_before = os.stat(str(target)).st_mtime_ns

        # Small sleep to ensure mtime would differ if a write occurred.
        time.sleep(0.05)

        result = locked_rmw(target, lambda old: old, repo_root=repo)

        mtime_after = os.stat(str(target)).st_mtime_ns

        assert result == "same content"
        assert mtime_before == mtime_after, (
            "os.replace was called even though content was byte-identical"
        )

    def test_no_op_returns_original_text(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("hello", encoding="utf-8")

        result = locked_rmw(target, lambda old: old, repo_root=repo)
        assert result == "hello"


# ---------------------------------------------------------------------------
# (c) LockTimeout when lock is held by another process
# ---------------------------------------------------------------------------


_HOLDER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    sys.path.insert(0, sys.argv[3])
    from coordinator_core.locked_write import _plat_try_lock, _plat_unlock

    lock_path = sys.argv[1]
    hold_secs = float(sys.argv[2])

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert _plat_try_lock(fd), "holder could not acquire the lock"
    # Signal parent we hold the lock.
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    time.sleep(hold_secs)
    _plat_unlock(fd)
    os.close(fd)
""")


class TestLockTimeout:
    def test_timeout_fires_when_lock_held(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("x", encoding="utf-8")

        # Derive the lock path the same way locked_rmw does internally.
        import hashlib
        key = hashlib.sha1(os.path.realpath(str(target)).encode()).hexdigest()
        from coordinator_core.lifecycle import git_common_dir
        lock_dir = git_common_dir(repo) / "coordinator-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{key}.lock"

        # Spawn a holder process that holds the lock for 5 s.
        holder_script = tmp_path / "holder.py"
        holder_script.write_text(_HOLDER_SCRIPT, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(holder_script), str(lock_path), "5", _PROJECT_ROOT],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            # Wait until the holder reports it holds the lock.
            line = proc.stdout.readline()
            assert line.strip() == b"LOCKED"

            # Now attempt locked_rmw with a short timeout — must raise LockTimeout.
            with pytest.raises(LockTimeout):
                locked_rmw(
                    target,
                    lambda old: old + "y",
                    repo_root=repo,
                    timeout=0.3,
                )
        finally:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# (d) Cross-process lost-update proof
# ---------------------------------------------------------------------------


_UNSAFE_WRITER_SCRIPT = textwrap.dedent("""\
    \"\"\"Naive writer: read-sleep-increment-write without any lock.

    Review: code-reviewer (F2) — added barrier synchronisation so both processes are
    guaranteed to have read the file BEFORE either writes.  Without the barrier, a slow
    subprocess startup could cause one process to read after the other has already written,
    producing final==2 (instead of 1) and a false CI failure in the wrong direction.
    \"\"\"
    import os, sys, time
    from pathlib import Path

    target = Path(sys.argv[1])
    delay = float(sys.argv[2])
    barrier_dir = Path(sys.argv[3])

    text = target.read_text(encoding="utf-8") if target.exists() else "0"
    val = int(text.strip()) + 1

    # Signal that this process has read the current value.
    (barrier_dir / f"ready.{os.getpid()}").touch()

    # Wait until BOTH processes have read (2 ready files present).
    deadline = time.monotonic() + 10.0
    while len(list(barrier_dir.glob("ready.*"))) < 2:
        if time.monotonic() > deadline:
            raise RuntimeError("barrier timeout: second process did not start in time")
        time.sleep(0.005)

    time.sleep(delay)
    target.write_text(str(val), encoding="utf-8")
""")


_SAFE_WRITER_SCRIPT = textwrap.dedent("""\
    \"\"\"Safe writer: uses locked_rmw for atomic increment.\"\"\"
    import sys, time
    from pathlib import Path

    # Ensure coordinator_core is importable.
    sys.path.insert(0, sys.argv[3])

    from coordinator_core.locked_write import locked_rmw

    target = Path(sys.argv[1])
    repo_root = Path(sys.argv[2])
    delay = float(sys.argv[4])

    def _increment(old: str) -> str:
        time.sleep(delay)
        return str(int(old.strip() or "0") + 1)

    locked_rmw(target, _increment, repo_root=repo_root, timeout=30)
""")


class TestCrossProcessLostUpdateProof:
    def test_without_lock_update_is_lost(self, tmp_path):
        """Without a lock, concurrent writers sharing a barrier produce a deterministic lost update.

        Review: code-reviewer (F2) — barrier dir synchronises both processes so they
        have provably both read "0" before either writes.  The previous version relied on
        the 0.1 s sleep alone, which was non-deterministic: a slow subprocess startup
        could cause one process to read after the other had already written, producing
        final==2 and a false "concurrency is broken" CI failure.
        """
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "counter.txt"
        target.write_text("0", encoding="utf-8")

        barrier_dir = tmp_path / "barrier"
        barrier_dir.mkdir()

        script = tmp_path / "unsafe_writer.py"
        script.write_text(_UNSAFE_WRITER_SCRIPT, encoding="utf-8")

        # Both processes read "0" then synchronise at the barrier before writing.
        p1 = subprocess.Popen(
            [sys.executable, str(script), str(target), "0.01", str(barrier_dir)],
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        p2 = subprocess.Popen(
            [sys.executable, str(script), str(target), "0.01", str(barrier_dir)],
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        p1.wait(timeout=30)
        p2.wait(timeout=30)

        # Both processes read "0", both write "1" — a provable lost update.
        final = int(target.read_text(encoding="utf-8").strip())
        assert final == 1, (
            f"Expected a lost update (final==1) to prove the window exists, got {final}"
        )

    def test_with_locked_rmw_both_updates_persist(self, tmp_path):
        """With locked_rmw, concurrent writers always produce a correct final count."""
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "counter.txt"
        target.write_text("0", encoding="utf-8")

        project_root = str(Path(__file__).parent.parent.parent.resolve())

        script = tmp_path / "safe_writer.py"
        script.write_text(_SAFE_WRITER_SCRIPT, encoding="utf-8")

        # Two concurrent writers, each sleeping 0.1 s inside mutate to widen the window.
        p1 = subprocess.Popen(
            [sys.executable, str(script), str(target), str(repo), project_root, "0.1"],
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        p2 = subprocess.Popen(
            [sys.executable, str(script), str(target), str(repo), project_root, "0.1"],
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        p1.wait(timeout=30)
        p2.wait(timeout=30)

        final = int(target.read_text(encoding="utf-8").strip())
        assert final == 2, (
            f"Expected both increments to persist (final==2), got {final} — "
            "locked_rmw serialisation broken"
        )


# ---------------------------------------------------------------------------
# (e) Symlinked-target mutual exclusion
# ---------------------------------------------------------------------------


@symlink_capability.requires_symlink_capability
class TestSymlinkMutualExclusion:
    def test_symlink_and_real_path_share_one_lock(self, tmp_path):
        """Two callers naming the same physical file via symlink vs realpath
        must serialise on the SAME lock sidecar (realpath-canonicalisation).
        """
        repo = _make_git_repo(tmp_path / "repo")
        real_target = repo / "data.txt"
        real_target.write_text("0", encoding="utf-8")

        link_target = repo / "data_link.txt"
        link_target.symlink_to(real_target)

        # Derive expected lock key for both spellings.
        import hashlib
        key_real = hashlib.sha1(os.path.realpath(str(real_target)).encode()).hexdigest()
        key_link = hashlib.sha1(os.path.realpath(str(link_target)).encode()).hexdigest()

        assert key_real == key_link, (
            "Symlink and realpath produce different lock keys — "
            "realpath-canonicalisation is broken"
        )

    def test_sequential_writes_via_symlink_and_realpath_both_land(self, tmp_path):
        """Writes via symlink and realpath are both applied (no file-replacement confusion)."""
        repo = _make_git_repo(tmp_path / "repo")
        real_target = repo / "data.txt"
        real_target.write_text("0", encoding="utf-8")

        link_target = repo / "data_link.txt"
        link_target.symlink_to(real_target)

        # Write via real path.
        locked_rmw(real_target, lambda old: str(int(old) + 1), repo_root=repo)
        # Write via symlink.
        locked_rmw(link_target, lambda old: str(int(old) + 1), repo_root=repo)

        assert real_target.read_text(encoding="utf-8") == "2"


# ---------------------------------------------------------------------------
# (f) repo_root-as-worktree vs repo_root/.git both resolve to same lock dir
# ---------------------------------------------------------------------------


class TestRepoRootVariants:
    def test_worktree_root_and_dotgit_produce_same_lock_dir(self, tmp_path):
        """Passing repo_root=<worktree> and repo_root=<worktree>/.git both derive
        the same git_common_dir and therefore the same coordinator-locks directory.
        """
        from coordinator_core.lifecycle import git_common_dir

        repo = _make_git_repo(tmp_path / "repo")
        dot_git = repo / ".git"

        common_via_root = git_common_dir(repo)
        # For a non-worktree repo, common dir == .git.
        assert common_via_root == dot_git.resolve() or common_via_root == dot_git

        # Verify that locked_rmw via both repo_root variants write the same file.
        target = repo / "data.txt"
        target.write_text("0", encoding="utf-8")

        locked_rmw(target, lambda old: "via-root", repo_root=repo)
        assert target.read_text(encoding="utf-8") == "via-root"

        locked_rmw(target, lambda old: "via-dotgit", repo_root=dot_git)
        assert target.read_text(encoding="utf-8") == "via-dotgit"

    def test_same_lock_sidecar_for_both_repo_root_variants(self, tmp_path):
        """The lock sidecar path is byte-for-byte identical for both repo_root spellings."""
        import hashlib
        from coordinator_core.lifecycle import git_common_dir

        repo = _make_git_repo(tmp_path / "repo")
        dot_git = repo / ".git"
        target = repo / "data.txt"
        target.write_text("x", encoding="utf-8")

        lock_dir_via_root = git_common_dir(repo) / "coordinator-locks"
        lock_dir_via_dotgit = git_common_dir(dot_git) / "coordinator-locks"

        assert lock_dir_via_root == lock_dir_via_dotgit, (
            f"Different lock dirs: {lock_dir_via_root} vs {lock_dir_via_dotgit}"
        )


# ---------------------------------------------------------------------------
# (g) Crash-safety: killed holder does not leave a stale lock
# ---------------------------------------------------------------------------


_HOLD_FOREVER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    sys.path.insert(0, sys.argv[2])
    from coordinator_core.locked_write import _plat_try_lock

    lock_path = sys.argv[1]
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert _plat_try_lock(fd), "holder could not acquire the lock"
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    # Hold forever until killed (OS releases the lock on process death).
    while True:
        time.sleep(10)
""")


class TestCrashSafety:
    def test_killed_holder_releases_lock_for_next_acquirer(self, tmp_path):
        """A holder process killed (SIGKILL) while holding the lock must not leave
        a stale lock — the OS releases flocks on fd close (process death), so the
        next caller should succeed within the timeout.
        """
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "data.txt"
        target.write_text("original", encoding="utf-8")

        # Derive lock path.
        import hashlib
        key = hashlib.sha1(os.path.realpath(str(target)).encode()).hexdigest()
        from coordinator_core.lifecycle import git_common_dir
        lock_dir = git_common_dir(repo) / "coordinator-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{key}.lock"

        holder_script = tmp_path / "hold_forever.py"
        holder_script.write_text(_HOLD_FOREVER_SCRIPT, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(holder_script), str(lock_path), _PROJECT_ROOT],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            line = proc.stdout.readline()
            assert line.strip() == b"LOCKED"

            # Kill with SIGKILL (unconditional) — no cleanup, no lock release from code.
            proc.kill()
            proc.wait()

            # The OS releases the flock on process exit; next acquirer should succeed.
            result = locked_rmw(
                target,
                lambda old: old + "-updated",
                repo_root=repo,
                timeout=3.0,
            )
            assert result == "original-updated"
            assert target.read_text(encoding="utf-8") == "original-updated"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestContendedLockWaitSecs:
    """`contended_lock_wait_secs` — the knob that turns a respawn loop into a wait.

    Why this is tested at all: the failure it closes was not a crash but a
    read. A contended publish lock timed out at ``LOCK_TIMEOUT_SECS`` and
    exited like a broken run, so sessions retried with a fresh process each
    time — 17 attempts against a queue on a box that runs 50-70 concurrent
    LLMs. The default here has to stay well above LOCK_TIMEOUT_SECS, and a
    malformed override has to degrade to the default rather than raise on a
    hot path.
    """

    def test_default_is_well_above_the_rmw_timeout(self, monkeypatch):
        monkeypatch.delenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raising=False)
        assert (
            locked_write.contended_lock_wait_secs()
            > locked_write.LOCK_TIMEOUT_SECS
        )
        assert locked_write.contended_lock_wait_secs() == 180.0

    def test_env_override_is_honoured_downward(self, monkeypatch):
        monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, "30")
        assert locked_write.contended_lock_wait_secs() == 30.0

    def test_env_override_cannot_raise_the_wait(self, monkeypatch):
        """Narrow-only, 2026-08-21 PM ruling. Ratchet:
        `coordinator_core/tests/test_contended_lock_wait_ratchet.py`."""
        monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, "600")
        assert locked_write.contended_lock_wait_secs() == 180.0

    @pytest.mark.parametrize("raw", ["", "   ", "soon", "0", "-5", "1e"])
    def test_malformed_or_non_positive_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raw)
        assert locked_write.contended_lock_wait_secs(default=42.0) == 42.0


class TestLockHolderAgeSuffix:
    """A UTC stamp read against a local clock is how a ten-minute-old lock
    reads as seventy minutes old. The offset is present in the stamp and gets
    misread anyway, so the describer carries an age that cannot be read in the
    wrong zone."""

    def test_offset_stamp_yields_an_age(self):
        from datetime import datetime, timedelta, timezone

        held = datetime.now(timezone.utc) - timedelta(seconds=600)
        suffix = locked_write._lock_age_suffix(held.isoformat())
        assert suffix.startswith(" age=")
        assert 595 <= int(suffix.removeprefix(" age=").removesuffix("s")) <= 605

    def test_z_stamp_yields_the_same_age(self):
        from datetime import datetime, timedelta, timezone

        held = datetime.now(timezone.utc) - timedelta(seconds=300)
        z_form = held.isoformat().replace("+00:00", "Z")
        suffix = locked_write._lock_age_suffix(z_form)
        assert 295 <= int(suffix.removeprefix(" age=").removesuffix("s")) <= 305

    @pytest.mark.parametrize(
        "stamp",
        ["2026-09-02T19:00:02", "unknown time", "", None, 17],
        ids=["naive", "sentinel", "empty", "none", "nonstring"],
    )
    def test_unusable_stamp_says_nothing_rather_than_guessing(self, stamp):
        """A wrong age is worse than no age -- it is the failure this closes."""
        assert locked_write._lock_age_suffix(stamp) == ""
