"""test_checked_repo_resolver_c4.py -- C4 test surface for the WRITER-group
repoint of the five near-verbatim `_resolve_repo_root` copies onto C1's
checked resolver.

Spec backlink: pln-one-checked-resolver-for-the-c-035d59
§ C4 / AC2, AC3, AC4, AC5, AC10.

C4's own module -- no other chunk in this plan writes to this file (each
chunk names its own test module; see the plan's "Dispatch shape" note).

Scope: the five near-verbatim copies this chunk repointed --
  - coordinator-tasks-mirror.py (WRITER)
  - coordinator-write-review-trail.py (WRITER)
  - verify-orientation-cache-sync.py (READER -- reclassified post-C8: no
    write path exists in this trampoline or the op it dispatches into)

`emit-cockpit-snapshot.py` (WRITER)'s coverage was removed 2026-08-25 along with the CLI
itself, which `c4912d73f` deleted as part of the emission-publish leg (kill ledger K-056).
That cut carries no recorded authority and its disposition is OPEN, so this class is removed
rather than rewritten: if K-056 is triaged as collateral and the CLI comes back, restore this
class with it. The evidence for that triage lives in K-056, not here.

`reconcile-completion-commits.py` (WRITER)'s coverage was removed 2026-08-23
along with `completion.reconcile_commits`, the op it dispatched, and the
trampoline itself -- do not resurrect this class before the op's replacement
lands with its own resolver repoint.

AC10's load-bearing assertion for this group: a wrong-repo (MISMATCH) case
proves NO artifact is written -- asserted on the ABSENCE of the write
target, not merely on an exit code. UNRESOLVED must still write normally
(DR-277, AC4 -- a degenerate/absent anchor must never harden into a
refusal).

MISMATCH/UNRESOLVED are constructed with REAL files on disk (a fabricated
`CLAUDE_CONFIG_DIR/sessions/` registry, real `.git`-marker directories for
the plausibility band) -- never by monkeypatching
`resolve_checked_repo_root`'s own return value, mirroring C1's own
`test_checked_repo_resolver.py` harness. Each script's actual downstream
write/dispatch call is stubbed out (route_mutation, or the op main) so
this module never depends on the full claude-klabauter transport/op stack --- only
on whether that downstream call site was reached at all, which is what
AC10's "before any write" ordering claim is actually about.

Temp-dir lifecycle: uses `tempfile.mkdtemp()` + `self.addCleanup(rmtree)`
rather than the `with tempfile.TemporaryDirectory()` context-manager form
-- each test `os.chdir()`s INTO the temp dir for the resolver walk, and on
Windows an rmtree attempted while cwd is still inside the tree being
deleted raises `PermissionError`. `addCleanup` callbacks run in LIFO order
after `tearDown()` (which restores cwd first), guaranteeing cwd is back
outside the tree before the rmtree cleanup fires.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from cc_invoke import require_engine_on_path  # noqa: E402

_ENGINE_ROOT = require_engine_on_path(__file__)

from lib.repo_identity import clear_repo_identity_memo  # noqa: E402
from coordinator_core.session import harness_registry as hr  # noqa: E402


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir: Path, filename: str, session_id: str, pid: int, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def _make_repo(root: Path) -> None:
    """Real directory + real `.git` marker -- sufficient for the
    plausibility band; no actual `git init` needed (repo_root.show_toplevel
    walks for the entry, non-spawning -- see coordinator_core/git/repo_root.py)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _load_module(script_name: str):
    path = os.path.join(_BIN_DIR, script_name)
    mod_name = f"c4_under_test_{script_name.replace('-', '_').replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _RepoIdentityHarness(unittest.TestCase):
    """Shared harness: real CLAUDE_CONFIG_DIR registry on disk, monkeypatched
    only at the psutil name-match leg -- same carve-out as C1's own test
    module."""

    def setUp(self):
        clear_repo_identity_memo()
        self._env_patches = []
        self._attr_patches = []
        self._old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._old_cwd)
        for key, old in self._env_patches:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        for obj, name, old in self._attr_patches:
            setattr(obj, name, old)
        clear_repo_identity_memo()

    def _mkdtemp(self) -> Path:
        td = tempfile.mkdtemp(prefix="c4-checked-resolver-")
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        return Path(td)

    def _setenv(self, key: str, value):
        old = os.environ.get(key)
        self._env_patches.append((key, old))
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _setattr(self, obj, name: str, value):
        old = getattr(obj, name)
        self._attr_patches.append((obj, name, old))
        setattr(obj, name, value)

    def _wire_pid_env(self, pid: int, hit: bool = True):
        import coordinator_core.session.core as _core

        if hit:
            self._setattr(_core, "_resolve_claude_pid_from_env", lambda: ((pid, 0.0), "env-hit"))
        else:
            self._setattr(_core, "_resolve_claude_pid_from_env", lambda: (None, "env-miss:absent"))
        self._setenv("CLAUDE_PID", str(pid))

    def _wire_registry_dir(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / "claude-config"
        self._setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        sessions_dir = config_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir

    def _wire_stable_pid_alive(self, alive: bool = True):
        import coordinator_core.pickup_assemble as _pa

        self._setattr(_pa._session_core, "stable_pid_alive", lambda pid, stored_start_epoch="": alive)

    def _wire_sid_env(self, sid):
        self._setenv("CLAUDE_CODE_SESSION_ID", sid)

    def _setup_mismatch(self, tmp_path: Path, sid: str, pid: int) -> tuple[Path, Path]:
        """Real anchor (cwd) diverges from the real registry-resolved root."""
        repo_root = tmp_path / "repo"
        foreign_root = tmp_path / "foreign"
        _make_repo(repo_root)
        _make_repo(foreign_root)
        sessions_dir = self._wire_registry_dir(tmp_path)
        _write_registry_record(sessions_dir, f"{pid}.json", sid, pid, foreign_root)
        self._wire_pid_env(pid)
        self._wire_stable_pid_alive(True)
        self._wire_sid_env(sid)
        os.chdir(str(repo_root))
        return repo_root, foreign_root

    def _setup_unresolved(self, tmp_path: Path) -> Path:
        """No sid env at all -- degrades to UNRESOLVED (AC1 fail-open bias)."""
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        self._wire_sid_env(None)
        os.chdir(str(repo_root))
        return repo_root


class TestCoordinatorTasksMirror(_RepoIdentityHarness):
    def test_mismatch_leaves_no_mirror_file_on_disk(self):
        tmp_path = self._mkdtemp()
        repo_root, _foreign = self._setup_mismatch(tmp_path, "sess-tm-mm", 7003)
        mod = _load_module("coordinator-tasks-mirror.py")
        self._setattr(mod, "_resolve_session_id", lambda cwd: "sess-tm-mm")

        rc = mod.main(["prog", "init", "checklist", "title-one"])
        self.assertEqual(rc, 1)

        mirror_file = repo_root / "state" / "tasks" / "sess-tm-mm" / "checklist.yaml"
        self.assertFalse(mirror_file.exists(), "no mirror file must be written on MISMATCH")

    def test_unresolved_still_writes_mirror_file(self):
        tmp_path = self._mkdtemp()
        repo_root = self._setup_unresolved(tmp_path)
        mod = _load_module("coordinator-tasks-mirror.py")
        self._setattr(mod, "_resolve_session_id", lambda cwd: "sess-tm-unres")

        rc = mod.main(["prog", "init", "checklist", "title-one"])
        self.assertEqual(rc, 0)

        mirror_file = repo_root / "state" / "tasks" / "sess-tm-unres" / "checklist.yaml"
        self.assertTrue(mirror_file.exists(), "the mirror file must still be written on UNRESOLVED")


@unittest.skip(
    "coordinator-write-review-trail.py's checked-resolver migration is authored but "
    "uncommitted, blocked on a live peer path-touch claim -- "
    "state/bug-backlog/2026-08-11-coordinator-write-review-trail-py-migrat-6f10a371c855.yaml. "
    "On a clean checkout of HEAD the script still has no resolve_checked_repo_root import, so "
    "these tests only pass because the working tree carries the uncommitted edit. Un-skip once "
    "that migration lands."
)
class TestCoordinatorWriteReviewTrail(_RepoIdentityHarness):
    _ARGV = [
        "--sha-range", "aaa..bbb",
        "--reviewer", "waived",
        "--scope", "session",
        "--verdict", "waived",
        "--diff-loc", "10",
        "--reviewer-evidence", "a real justification",
    ]

    def test_mismatch_refuses_before_route_mutation(self):
        tmp_path = self._mkdtemp()
        self._setup_mismatch(tmp_path, "sess-wrt-mm", 7004)
        mod = _load_module("coordinator-write-review-trail.py")

        called = {"n": 0}
        self._setattr(mod.cc_invoke, "route_mutation", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

        with self.assertRaises(SystemExit) as ctx:
            mod.main(self._ARGV)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(called["n"], 0, "route_mutation must never be reached on MISMATCH")

    def test_unresolved_still_dispatches(self):
        tmp_path = self._mkdtemp()
        self._setup_unresolved(tmp_path)
        mod = _load_module("coordinator-write-review-trail.py")

        called = {"n": 0}

        def _fake_route_mutation(*a, **k):
            called["n"] += 1
            return {"ok": True}

        self._setattr(mod.cc_invoke, "route_mutation", _fake_route_mutation)

        rc = mod.main(self._ARGV)
        self.assertEqual(rc, 0)
        self.assertEqual(called["n"], 1, "the op must still be dispatched on UNRESOLVED")


class TestVerifyOrientationCacheSync(_RepoIdentityHarness):
    """verify-orientation-cache-sync.py is a READER (AC10 reclassification):
    no write path exists anywhere in this trampoline or the op it dispatches
    into (it only returns a verify outcome code) -- so a MISMATCH warns to
    stderr and still dispatches, it never refuses (DR-277)."""

    def _wire_state_root(self, mod, tmp_path: Path) -> Path:
        cache_dir = tmp_path / "state"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "orientation_cache.md").write_text("---\n---\n", encoding="utf-8")
        self._setattr(mod, "_resolve_state_root", lambda: str(cache_dir))
        return cache_dir

    def test_mismatch_warns_and_still_dispatches(self):
        tmp_path = self._mkdtemp()
        self._setup_mismatch(tmp_path, "sess-voc-mm", 7005)
        mod = _load_module("verify-orientation-cache-sync.py")
        self._wire_state_root(mod, tmp_path)

        called = {"n": 0}
        self._setattr(mod, "_import_op_main", lambda: (lambda argv: called.__setitem__("n", called["n"] + 1) or 0))

        self.assertEqual(mod.main(), 0)
        self.assertEqual(called["n"], 1, "the verify op must still be dispatched on MISMATCH (READER, no write to protect)")

    def test_unresolved_still_dispatches(self):
        tmp_path = self._mkdtemp()
        self._setup_unresolved(tmp_path)
        mod = _load_module("verify-orientation-cache-sync.py")
        self._wire_state_root(mod, tmp_path)

        called = {"n": 0}
        self._setattr(mod, "_import_op_main", lambda: (lambda argv: called.__setitem__("n", called["n"] + 1) or 0))

        self.assertEqual(mod.main(), 0)
        self.assertEqual(called["n"], 1, "the verify op must still be dispatched on UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
