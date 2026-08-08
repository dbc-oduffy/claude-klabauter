"""
coordinator_core.tests.test_locked_write_held_lock — tests for held_lock.

held_lock is the scope-held wrapper around locked_write's acquire/release
primitive, added for Part B of the concurrent-publish guard: a caller needs
to hold an advisory lock across a whole multi-step operation (a publish run)
rather than a single read-modify-write.

Coverage requirements (state/handoffs/2026-08-07-percolate-performance-delta-sweep.md
Part B):
  (a) concurrent acquire — second caller's LockTimeout names the holder PID
  (b) released lock is re-acquirable
  (c) corrupt/absent sidecar degrades to "holder unknown" without crashing
  (d) killed-holder case does not wedge the next acquirer

Anchoring coverage (carried item `cf-held-lock-convention-7b2f30`): the
sidecar's directory decides the mutual-exclusion namespace, so an anchor the
caller picks is an anchor two callers can disagree on. held_lock now derives
it (`_default_anchor_root`) and validates any explicit override:
  (e) an anchor equal to / nested inside `target` is rejected, before the
      offending directory is created
  (f) the default namespace lies outside every checkout, and works for a
      `target` that does not exist and never will
  (g) two DIFFERENT installs of the engine locking one `target` are mutually
      excluded (the residual the anchor-to-my-own-checkout fix left open),
      and a SIGKILLed holder's lock is released by the OS for the next
      acquirer in that machine-scoped namespace — the acceptance bar that
      distinguishes this rendezvous from the retired ceremony-lock directory

POSIX guard: all tests are skipped with a clear message if neither fcntl nor
msvcrt is available.

Spec backlink: state/handoffs/2026-08-07-percolate-performance-delta-sweep.md Part B
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Real cross-process locking coverage: `_make_git_repo` shells out to `git`
# subprocesses, and several tests spawn separate `sys.executable` holder/
# hold-forever processes via `subprocess.Popen` to exercise held_lock's
# cross-process contention, kill, and stale-metadata paths for real.
pytestmark = [
    pytest.mark.skipif(
        not _LOCKING_AVAILABLE,
        reason="held_lock needs a file-lock backend (fcntl or msvcrt) — neither available",
    ),
    pytest.mark.spawns_process,
]

from coordinator_core.locked_write import (  # noqa: E402
    LockAnchorError,
    LockTimeout,
    held_lock,
)
from coordinator_core.lifecycle import git_common_dir  # noqa: E402


def _make_git_repo(root: Path) -> Path:
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
    _git("config", "user.email", "held-lock-test@claude-klabauter.test")
    _git("config", "user.name", "Held Lock Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


def _lock_path_for(target: Path, repo: Path) -> Path:
    key = hashlib.sha1(os.path.realpath(str(target)).encode()).hexdigest()
    lock_dir = git_common_dir(repo) / "coordinator-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{key}.lock"


_HOLDER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    sys.path.insert(0, sys.argv[3])
    from coordinator_core.locked_write import held_lock
    from pathlib import Path

    target = Path(sys.argv[1])
    repo = Path(sys.argv[2])
    hold_secs = float(sys.argv[4])
    label = sys.argv[5]

    with held_lock(target, anchor_root=repo, timeout=10, holder_label=label):
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        time.sleep(hold_secs)
""")

_HOLD_FOREVER_SCRIPT = textwrap.dedent("""\
    import os, sys, time

    sys.path.insert(0, sys.argv[2])
    from coordinator_core.locked_write import _plat_try_lock

    lock_path = sys.argv[1]
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert _plat_try_lock(fd), "holder could not acquire the lock"
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    while True:
        time.sleep(10)
""")


class TestConcurrentAcquire:
    def test_second_caller_times_out_naming_holder_pid(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"

        holder_script = tmp_path / "holder.py"
        holder_script.write_text(_HOLDER_SCRIPT, encoding="utf-8")

        proc = subprocess.Popen(
            [
                sys.executable, str(holder_script),
                str(target), str(repo), _PROJECT_ROOT, "5", "claude-klabauter",
            ],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            line = proc.stdout.readline()
            assert line.strip() == b"LOCKED"

            with pytest.raises(LockTimeout) as exc_info:
                with held_lock(target, anchor_root=repo, timeout=0.3):
                    pass  # pragma: no cover - must not be reached

            message = str(exc_info.value)
            assert f"pid={proc.pid}" in message
            assert "claude-klabauter" in message
        finally:
            proc.kill()
            proc.wait()


class TestReleaseAndReacquire:
    def test_released_lock_is_reacquirable(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"

        with held_lock(target, anchor_root=repo, timeout=2, holder_label="first"):
            pass

        with held_lock(target, anchor_root=repo, timeout=2, holder_label="second"):
            pass  # no LockTimeout raised — lock was fully released


class TestCorruptSidecarDegradesGracefully:
    def test_absent_sidecar_reports_holder_unknown(self, tmp_path):
        from coordinator_core.locked_write import _describe_holder

        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"
        lock_path = _lock_path_for(target, repo)

        assert not lock_path.exists()
        assert _describe_holder(lock_path) == "holder unknown"

    def test_corrupt_sidecar_reports_holder_unknown_without_crashing(self, tmp_path):
        from coordinator_core.locked_write import _describe_holder

        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"
        lock_path = _lock_path_for(target, repo)
        lock_path.write_bytes(b"\xff\xfe not json at all {{{")

        result = _describe_holder(lock_path)
        assert "holder unknown" in result

    def test_corrupt_sidecar_does_not_block_new_acquire(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"
        lock_path = _lock_path_for(target, repo)
        lock_path.write_bytes(b"garbage, not json")

        with held_lock(target, anchor_root=repo, timeout=2, holder_label="ok"):
            pass  # acquiring must succeed — corrupt content is not a lock holder


class TestCrashSafety:
    def test_killed_holder_does_not_wedge_next_acquirer(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"
        lock_path = _lock_path_for(target, repo)

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

            proc.kill()
            proc.wait()

            with held_lock(target, anchor_root=repo, timeout=3.0, holder_label="next"):
                pass  # OS released the flock on process death — must not time out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_killed_held_lock_holder_leaves_no_stale_pid_readable_by_next_contender(
        self, tmp_path
    ):
        """Cross-process metadata-staleness test (Review: code-reviewer P3).

        Unlike `test_killed_holder_does_not_wedge_next_acquirer` above (which
        bypasses `held_lock` via `_plat_try_lock` and so never writes real
        holder metadata), this test drives a real holder through `held_lock`
        so its PID lands in the sidecar, kills it without letting its
        release-side `_clear_holder_metadata` run, then drives a SECOND real
        holder through `held_lock` (exercising the acquire-side
        clear-then-write fix) and confirms that a third party contending
        against that second holder never sees the first, now-dead PID named
        as the current holder.
        """
        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"

        holder_script = tmp_path / "holder.py"
        holder_script.write_text(_HOLDER_SCRIPT, encoding="utf-8")

        victim = subprocess.Popen(
            [
                sys.executable, str(holder_script),
                str(target), str(repo), _PROJECT_ROOT, "30", "victim",
            ],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        next_holder = None
        try:
            line = victim.stdout.readline()
            assert line.strip() == b"LOCKED"
            victim_pid = victim.pid

            # SIGKILL — release-side _clear_holder_metadata never runs, so
            # the victim's PID is left on disk (stale-but-well-formed).
            victim.kill()
            victim.wait()

            next_holder = subprocess.Popen(
                [
                    sys.executable, str(holder_script),
                    str(target), str(repo), _PROJECT_ROOT, "5", "next",
                ],
                stdout=subprocess.PIPE,
                creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
            )
            line = next_holder.stdout.readline()
            assert line.strip() == b"LOCKED"

            with pytest.raises(LockTimeout) as exc_info:
                with held_lock(target, anchor_root=repo, timeout=0.3):
                    pass  # pragma: no cover - must not be reached

            message = str(exc_info.value)
            assert f"pid={victim_pid}" not in message
            assert "victim" not in message
        finally:
            victim.kill()
            victim.wait()
            if next_holder is not None:
                next_holder.kill()
                next_holder.wait()


class TestLockSidecarNotUnderRenamedTarget:
    """Pins the `.git`-rename incident's root cause and fix (state/subagent-
    share, 2026-08-07): the lock sidecar's DIRECTORY must derive from an
    anchor distinct from `target` whenever `target` names a tree the caller
    intends to rename/replace — passing the same path for both (as
    `coordinator/bin/publish.py` once did) puts the sidecar, with an open
    handle, INSIDE the tree being renamed.
    """

    def test_sidecar_lands_outside_target_when_anchor_differs(self, tmp_path):
        anchor_repo = _make_git_repo(tmp_path / "anchor")
        renamed_repo = _make_git_repo(tmp_path / "renamed")
        target = renamed_repo

        with held_lock(target, anchor_root=anchor_repo, holder_label="fix"):
            lock_path = _lock_path_for(target, anchor_repo)
            assert lock_path.exists()
            assert anchor_repo.resolve() in lock_path.resolve().parents
            assert renamed_repo.resolve() not in lock_path.resolve().parents

    def test_git_dir_rename_succeeds_when_lock_sidecar_is_outside_it(self, tmp_path):
        """Reproduces the exact incident: renaming `target`'s `.git` while a
        `held_lock` is open on `target`. Fails deterministically (Windows
        `PermissionError`) when the anchor is `target` itself (sidecar inside
        the renamed tree, now a `LockAnchorError` — see
        `test_anchor_equal_to_target_is_rejected`); succeeds when the anchor
        is a separate repo, which is what the default now guarantees."""
        anchor_repo = _make_git_repo(tmp_path / "anchor")
        renamed_repo = _make_git_repo(tmp_path / "renamed")
        staging = tmp_path / "staging"
        staging.mkdir()

        with held_lock(renamed_repo, anchor_root=anchor_repo, holder_label="fix"):
            os.rename(str(renamed_repo / ".git"), str(staging / ".git"))
            assert (staging / ".git").is_dir()
            assert not (renamed_repo / ".git").exists()

    def test_anchor_equal_to_target_is_rejected(self, tmp_path):
        """The ACTUAL pre-fix caller shape — `publish.py::main()` once called
        `held_lock(target, repo_root=target, ...)`, anchoring the sidecar to
        the very destination it was about to rename — is now refused at the
        seam rather than merely documented in prose.

        Acceptance criterion for carried item `cf-held-lock-convention-7b2f30`:
        this fails against the pre-guard module (the `with` body runs, nothing
        raises). The two tests above never set anchor == target, so they passed
        identically whether or not the defect was present; this one does not.
        Platform-agnostic — the guard is path arithmetic, so unlike the
        incident's Windows-only `PermissionError` symptom this asserts the
        same thing on POSIX.
        """
        repo = _make_git_repo(tmp_path / "repo")

        with pytest.raises(LockAnchorError) as exc_info:
            with held_lock(repo, anchor_root=repo, holder_label="buggy"):
                pass  # pragma: no cover - must not be reached

        assert "inside the very path being locked" in str(exc_info.value)

    def test_rejected_anchor_creates_no_sidecar_directory(self, tmp_path):
        """The guard runs BEFORE the lock directory is created.

        A rejection that still left `<target>/.git/coordinator-locks/` behind
        would have written into the tree it was refusing to write into — and
        that stray directory is exactly the debris the incident's diagnosis
        had to rule out on later, correctly-anchored runs.
        """
        repo = _make_git_repo(tmp_path / "repo")

        with pytest.raises(LockAnchorError):
            with held_lock(repo, anchor_root=repo):
                pass  # pragma: no cover - must not be reached

        assert not (repo / ".git" / "coordinator-locks").exists()

    def test_anchor_nested_inside_target_is_rejected(self, tmp_path):
        """Not just equality: any anchor UNDER `target` puts the sidecar under
        `target` too, with the same open-handle-in-a-renamed-tree hazard. A
        guard comparing the two paths only for equality would wave through a
        caller anchoring to a nested checkout of its own destination.
        """
        outer = _make_git_repo(tmp_path / "outer")
        nested = _make_git_repo(outer / "nested")

        with pytest.raises(LockAnchorError):
            with held_lock(outer, anchor_root=nested):
                pass  # pragma: no cover - must not be reached


class TestDefaultAnchor:
    """The by-construction half of the fix: omitting `anchor_root` is both the
    shortest call and the only one that cannot disagree with another caller.
    """

    def test_default_lock_dir_is_outside_every_checkout(self):
        """The default namespace must not be derived from any checkout.

        Anchoring to the checkout shipping the module (the intermediate fix)
        removed caller-vs-caller disagreement but left two INSTALLS with two
        namespaces. If this assertion ever fails — i.e. the default lock dir
        is back under a repo — that residual is back, silently.
        """
        from coordinator_core.locked_write import _machine_lock_dir

        lock_dir = Path(os.path.realpath(str(_machine_lock_dir())))
        project_root = Path(os.path.realpath(_PROJECT_ROOT))
        assert project_root not in lock_dir.parents
        assert lock_dir != project_root

    def test_default_anchor_locks_without_an_explicit_root(self, tmp_path, monkeypatch):
        """The short call shape works end-to-end. `target` is a tmp_path that
        is NOT a repo and does NOT exist — the primary caller's shape (a
        destination repo root, possibly not yet created) — proving the guard
        never stats `target`.

        `COORDINATOR_LOCK_ROOT` keeps the sidecar out of the operator's real
        `~/.coordinator`; it does not change which code path runs.
        """
        monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "machine"))
        target = tmp_path / "never-created-destination"
        assert not target.exists()

        with held_lock(target, timeout=2, holder_label="default-anchor"):
            pass

    def test_nonexistent_target_still_excludes_a_second_process(self, tmp_path):
        """A non-existent `target` must still be a real mutex, not a no-op.

        `target` is only ever a key, so absence is legitimate (held_lock's
        docstring) and must not quietly degrade exclusion — which is the
        failure mode a guard that stat'd `target` would have introduced.
        Cross-process, not nested: held_lock is explicitly non-reentrant
        within one process (module negative-spec), so a same-process nest
        would assert the documented self-deadlock rather than exclusion.
        """
        anchor = _make_git_repo(tmp_path / "anchor")
        target = tmp_path / "absent" / "destination"
        assert not target.exists()

        holder_script = tmp_path / "holder.py"
        holder_script.write_text(_HOLDER_SCRIPT, encoding="utf-8")

        proc = subprocess.Popen(
            [
                sys.executable, str(holder_script),
                str(target), str(anchor), _PROJECT_ROOT, "5", "absent-target",
            ],
            stdout=subprocess.PIPE,
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            assert proc.stdout.readline().strip() == b"LOCKED"

            with pytest.raises(LockTimeout) as exc_info:
                with held_lock(target, anchor_root=anchor, timeout=0.3):
                    pass  # pragma: no cover - must not be reached

            assert "absent-target" in str(exc_info.value)
        finally:
            proc.kill()
            proc.wait()


_INSTALL_HOLDER_SCRIPT = textwrap.dedent("""\
    import sys, time
    from pathlib import Path

    install_root, target, hold_secs, label = sys.argv[1:5]
    sys.path.insert(0, install_root)
    from coordinator_core.locked_write import held_lock

    # No anchor_root: this is the shape the real caller uses, and the one
    # whose namespace must not vary with which install is running.
    with held_lock(Path(target), timeout=10, holder_label=label):
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        time.sleep(float(hold_secs))
""")

_INSTALL_CONTENDER_SCRIPT = textwrap.dedent("""\
    import sys
    from pathlib import Path

    install_root, target, timeout = sys.argv[1:4]
    sys.path.insert(0, install_root)
    from coordinator_core.locked_write import held_lock, LockTimeout

    try:
        with held_lock(Path(target), timeout=float(timeout), holder_label="contender"):
            sys.stdout.write("ACQUIRED\\n")
    except LockTimeout as exc:
        sys.stdout.write("TIMEOUT %s\\n" % (exc,))
    sys.stdout.flush()
""")

_INSTALL_PRINT_DIR_SCRIPT = textwrap.dedent("""\
    import sys

    install_root = sys.argv[1]
    sys.path.insert(0, install_root)
    from coordinator_core.locked_write import _machine_lock_dir

    sys.stdout.write(str(_machine_lock_dir()) + "\\n")
""")


def _make_fake_install(root: Path) -> Path:
    """Materialise a second, independent INSTALL of the engine's lock module.

    A faithful stand-in for the multi-copy topology this machine actually
    runs (engine-resident checkout + shared install): a separate directory
    tree, off any `sys.path` the parent has, with its own physical copy of
    `locked_write.py` imported as its own module object. `held_lock`'s
    default path deliberately touches nothing else in the package, so the
    copy needs no other engine module — which is itself the point: the
    rendezvous must be derivable without reference to the install.
    """
    pkg = root / "coordinator_core"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    source = Path(_PROJECT_ROOT) / "coordinator_core" / "locked_write.py"
    (pkg / "locked_write.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


def _isolated_env(machine_root: Path) -> dict:
    """Child env pinning the machine rendezvous into *machine_root*.

    `COORDINATOR_LOCK_ROOT` is the same category of opt-out as `anchor_root`
    (module docstring): it exists so tests get per-`tmp_path` isolation
    without writing into the operator's real `~/.coordinator`. Every child
    in one test gets the SAME value — the stand-in for "one machine", not a
    per-caller choice.
    """
    env = dict(os.environ)
    env["COORDINATOR_LOCK_ROOT"] = str(machine_root)
    return env


class TestTwoInstallsShareOneNamespace:
    """The residual the previous fix left open: two DIFFERENT installs of this
    engine locking one `target` landed in two sidecars and were not mutually
    excluded — silently, no error, no diagnostic. These fail against the
    anchor-to-my-own-checkout scheme (each install resolves under its own
    root) and pass against `_machine_lock_dir`.
    """

    def test_two_installs_resolve_the_same_rendezvous(self, tmp_path):
        """Install-independence of the namespace, with no env override in
        play — the property the real deployment relies on. Each child prints
        the directory its OWN copy of the module resolves; they must agree,
        and neither may sit under its install root.
        """
        install_a = _make_fake_install(tmp_path / "install-a")
        install_b = _make_fake_install(tmp_path / "install-b")

        script = tmp_path / "print_dir.py"
        script.write_text(_INSTALL_PRINT_DIR_SCRIPT, encoding="utf-8")

        env = dict(os.environ)
        env.pop("COORDINATOR_LOCK_ROOT", None)

        def _dir_for(install_root: Path) -> Path:
            out = subprocess.run(
                [sys.executable, str(script), str(install_root)],
                capture_output=True,
                check=True,
                env=env,
                creationflags=_NO_WINDOW,
            ).stdout.decode().strip()
            return Path(os.path.realpath(out))

        dir_a = _dir_for(install_a)
        dir_b = _dir_for(install_b)

        assert dir_a == dir_b
        assert Path(os.path.realpath(str(install_a))) not in dir_a.parents
        assert Path(os.path.realpath(str(install_b))) not in dir_b.parents

    def test_two_installs_are_mutually_excluded_on_one_target(self, tmp_path):
        """The exclusion itself, cross-process and cross-install.

        `target` is a path neither install owns and which does not exist —
        the primary caller's shape (a destination repo root). Under the old
        scheme the contender would have taken a DIFFERENT sidecar and
        acquired immediately; here it must time out naming the holder.
        """
        machine_root = tmp_path / "machine"
        install_a = _make_fake_install(tmp_path / "install-a")
        install_b = _make_fake_install(tmp_path / "install-b")
        target = tmp_path / "destination-repo"
        assert not target.exists()

        holder_script = tmp_path / "install_holder.py"
        holder_script.write_text(_INSTALL_HOLDER_SCRIPT, encoding="utf-8")
        contender_script = tmp_path / "install_contender.py"
        contender_script.write_text(_INSTALL_CONTENDER_SCRIPT, encoding="utf-8")

        holder = subprocess.Popen(
            [
                sys.executable, str(holder_script),
                str(install_a), str(target), "10", "install-a-publish",
            ],
            stdout=subprocess.PIPE,
            env=_isolated_env(machine_root),
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            assert holder.stdout.readline().strip() == b"LOCKED"

            result = subprocess.run(
                [
                    sys.executable, str(contender_script),
                    str(install_b), str(target), "0.5",
                ],
                capture_output=True,
                check=True,
                env=_isolated_env(machine_root),
                creationflags=_NO_WINDOW,
            ).stdout.decode()

            assert result.startswith("TIMEOUT"), result
            assert "install-a-publish" in result
            assert f"pid={holder.pid}" in result
        finally:
            holder.kill()
            holder.wait()


class TestMachineRendezvousCrashRelease:
    """The acceptance bar for adopting a machine-scoped rendezvous at all.

    The mechanism retired on 2026-08-07 (`ops/ceremony/ceremony_lock.py`)
    wedged the tree because a holder that died without unwinding was
    unreclaimable — its liveness was self-description in a directory with no
    reaper. This asserts the distinguishing property: the kernel lock is
    released by the OS on holder death, so the NEXT acquirer — from a
    different install — proceeds with no reaper and no human `rm`. If this
    ever fails, the mechanism has acquired the retired one's failure mode and
    must be removed, not repaired in place.
    """

    def test_sigkilled_holder_releases_the_machine_lock_for_another_install(
        self, tmp_path
    ):
        machine_root = tmp_path / "machine"
        install_a = _make_fake_install(tmp_path / "install-a")
        install_b = _make_fake_install(tmp_path / "install-b")
        target = tmp_path / "destination-repo"

        holder_script = tmp_path / "install_holder.py"
        holder_script.write_text(_INSTALL_HOLDER_SCRIPT, encoding="utf-8")
        contender_script = tmp_path / "install_contender.py"
        contender_script.write_text(_INSTALL_CONTENDER_SCRIPT, encoding="utf-8")

        victim = subprocess.Popen(
            [
                sys.executable, str(holder_script),
                str(install_a), str(target), "60", "victim-install-a",
            ],
            stdout=subprocess.PIPE,
            env=_isolated_env(machine_root),
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            assert victim.stdout.readline().strip() == b"LOCKED"
            # SIGKILL: no `finally`, no release from code, no reaper anywhere.
            victim.kill()
            victim.wait()

            result = subprocess.run(
                [
                    sys.executable, str(contender_script),
                    str(install_b), str(target), "3",
                ],
                capture_output=True,
                check=True,
                env=_isolated_env(machine_root),
                creationflags=_NO_WINDOW,
            ).stdout.decode()

            assert result.strip() == "ACQUIRED", result
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait()

    def test_grandchild_spawned_under_the_lock_does_not_inherit_it(self, tmp_path):
        """Pins the module docstring's named CLOEXEC dependency (Review:
        code-reviewer P2): the crash-release argument above assumes a child
        spawned while `held_lock` is open does NOT inherit the open lock fd/
        handle and outlive its parent's death. That assumption rests on two
        defaults this module never sets itself — `os.open`'s `O_CLOEXEC`
        (PEP 446) and `subprocess.Popen`'s `close_fds=True` — so this proves
        the composed behaviour rather than trusting the defaults by citation.

        Holds the lock, spawns a plain `subprocess.Popen` child from INSIDE
        the held scope, kills only the holder (the child is left running,
        unkilled, exactly as an inherited-handle failure would need it to
        be), and confirms a second install still acquires promptly. If the
        child had inherited the lock fd/handle, this would time out instead.
        """
        machine_root = tmp_path / "machine"
        install_a = _make_fake_install(tmp_path / "install-a")
        install_b = _make_fake_install(tmp_path / "install-b")
        target = tmp_path / "destination-repo"

        holder_script = tmp_path / "grandchild_holder.py"
        holder_script.write_text(_GRANDCHILD_HOLDER_SCRIPT, encoding="utf-8")
        sleep_script = tmp_path / "sleep_forever.py"
        sleep_script.write_text(_SLEEP_FOREVER_SCRIPT, encoding="utf-8")
        contender_script = tmp_path / "install_contender.py"
        contender_script.write_text(_INSTALL_CONTENDER_SCRIPT, encoding="utf-8")

        grandchild_pid = None
        victim = subprocess.Popen(
            [
                sys.executable, str(holder_script),
                str(install_a), str(target), str(sleep_script), "60",
            ],
            stdout=subprocess.PIPE,
            env=_isolated_env(machine_root),
            creationflags=_NO_WINDOW,  # popup-safe-env-suppressed
        )
        try:
            line = victim.stdout.readline().strip().decode()
            assert line.startswith("LOCKED ")
            grandchild_pid = int(line.split()[1])

            # SIGKILL the holder only — the grandchild is deliberately left
            # running, unkilled, to prove it never held the lock in the
            # first place (it was never inherited).
            victim.kill()
            victim.wait()

            result = subprocess.run(
                [
                    sys.executable, str(contender_script),
                    str(install_b), str(target), "3",
                ],
                capture_output=True,
                check=True,
                env=_isolated_env(machine_root),
                creationflags=_NO_WINDOW,
            ).stdout.decode()

            assert result.strip() == "ACQUIRED", result
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait()
            if grandchild_pid is not None:
                try:
                    os.kill(grandchild_pid, 9 if os.name != "nt" else 15)
                except OSError:
                    pass


_GRANDCHILD_HOLDER_SCRIPT = textwrap.dedent("""\
    import subprocess, sys, time
    from pathlib import Path

    install_root, target, sleep_forever_script, hold_secs = sys.argv[1:5]
    sys.path.insert(0, install_root)
    from coordinator_core.locked_write import held_lock

    with held_lock(Path(target), timeout=10, holder_label="grandchild-parent"):
        # Plain subprocess.Popen — default close_fds=True — spawned WHILE the
        # lock fd is open, mirroring a real subprocess launched from inside a
        # held_lock scope (e.g. a function-gate subprocess). Not detached and
        # not killed alongside the parent: outlives it deliberately.
        child = subprocess.Popen([sys.executable, sleep_forever_script])
        sys.stdout.write("LOCKED %d\\n" % child.pid)
        sys.stdout.flush()
        time.sleep(float(hold_secs))
""")

_SLEEP_FOREVER_SCRIPT = textwrap.dedent("""\
    import time
    while True:
        time.sleep(10)
""")


@pytest.mark.skipif(
    not _MSVCRT_AVAILABLE,
    reason="Windows-only: verifies the msvcrt mandatory-range-lock docstring claim",
)
class TestWindowsMandatoryRangeLock:
    def test_plain_read_at_locked_byte_range_blocked_but_metadata_offset_readable(
        self, tmp_path
    ):
        """Proves (rather than merely asserts) the `_METADATA_OFFSET` docstring
        claim: `msvcrt.locking`'s 1-byte range at offset 0 is a MANDATORY lock
        that also blocks a separate fd's plain `read()`, while the holder
        metadata written from offset 1 stays outside that range and remains
        readable by a fresh, non-locking fd for the whole time the lock is
        held. If this fails, the claim is false and is break-class — do not
        paper over it.
        """
        import errno
        import msvcrt  # noqa: F401 — presence already gated by skipif

        from coordinator_core.locked_write import _METADATA_OFFSET

        repo = _make_git_repo(tmp_path / "repo")
        target = repo / "dest-mirror"
        lock_path = _lock_path_for(target, repo)

        with held_lock(target, anchor_root=repo, timeout=2, holder_label="ranged"):
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                with pytest.raises(OSError) as exc_info:
                    os.read(fd, 1)
                assert exc_info.value.errno in (errno.EACCES, errno.EDEADLOCK)

                os.lseek(fd, _METADATA_OFFSET, os.SEEK_SET)
                raw = os.read(fd, 4096)
                assert raw, "holder metadata at offset 1 must stay readable"
                assert b"ranged" in raw
            finally:
                os.close(fd)
