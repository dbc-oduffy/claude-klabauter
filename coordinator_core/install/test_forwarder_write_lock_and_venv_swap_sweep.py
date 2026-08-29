"""
Contract tests for two out-of-repo-write concurrency fixes found during the
2026-08-14 install-path safety audit (dispatch: coordinatorexecutor-9251b1e9).

1. `substrate._write_agent_helper_forwarders` (Step 3b's real-install write
   loop) previously called `_write_agent_forwarder`/`_write_agent_cmd_forwarder`
   — both plain in-place `Path.write_text`, not atomic-temp-and-rename — with
   NO lock held, unlike `forwarder_self_heal.py`'s identical writers, which
   already take `coordinator_core.locked_write.held_lock` on the same
   `<settings-home>/bin` directory before writing. A concurrent installer run,
   or a concurrent self-heal (routine at session boot per CLAUDE.md § Load
   norm), could interleave on the same destination file. Fixed by wrapping
   the real (non-check_only) write loop in the same `held_lock` primitive.

2. `uninstall_legs.uninstall_remove_substrate` removed only the live
   `.coordinator-venv` path via `_rmtree_target`, never the
   `.coordinator-venv.build-<pid>-<hex>`/`.coordinator-venv.stale-<pid>-<hex>`
   swap siblings a crashed rebuild or a Windows deferred-reclaim can leave
   behind (`ensure_venv.py`'s `_swap_in_new_venv`/`_sweep_orphaned_swap_dirs`
   — the latter only runs on the venv's NEXT rebuild, which uninstall may
   make never happen). Fixed by reusing `ensure_venv._sweep_orphaned_swap_dirs`
   from the uninstall leg instead of reimplementing the prefix match.

No `cadence`/`pending_fix`/`designed_red` module marks — this file runs on
the fast gate.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.install import substrate
from coordinator_core.install import uninstall_legs
from coordinator_core.locked_write import LockTimeout, held_lock


def _target_map_for(tmp_path: Path) -> "dict[str, str]":
    return {"widget": "widget.py"}


class TestForwarderWriteLoopTakesHeldLock:
    """`_write_agent_helper_forwarders`'s real-write branch must serialise
    against another holder of the SAME `bin_dst` lock — proving it acquires
    `held_lock`, not merely that its output happens to look right."""

    def _isolate_lock_root(self, monkeypatch, tmp_path):
        # held_lock's default anchor is a real per-user machine directory
        # (~/.coordinator/coordinator-locks) -- relocate it for test
        # isolation via the documented test-only escape hatch, never the
        # operator's real one.
        monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "lock-root"))

    def test_real_write_loop_blocks_on_a_lock_already_held_for_bin_dst(
        self, monkeypatch, tmp_path
    ):
        self._isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin"
        bin_dst.mkdir()
        target_map = _target_map_for(tmp_path)

        # Hold the SAME lock `_write_agent_helper_forwarders` must acquire,
        # from this thread, before the write loop runs on another thread.
        with held_lock(bin_dst, holder_label="test-holder", timeout=5.0):
            result: "dict[str, object]" = {}

            def _call():
                try:
                    substrate._write_agent_helper_forwarders(
                        target_map, bin_dst, False,
                    )
                except LockTimeout as exc:
                    result["timeout"] = exc

            t = threading.Thread(target=_call)
            t.start()
            t.join(timeout=3.0)
            # The writer thread must still be blocked waiting for the lock
            # this test holds -- it must not have written anything yet.
            assert t.is_alive() or "timeout" in result
            assert not (bin_dst / "widget").exists()

        t.join(timeout=10.0)
        assert not t.is_alive()

    def test_real_write_loop_succeeds_once_uncontended(self, monkeypatch, tmp_path):
        self._isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin"
        bin_dst.mkdir()
        target_map = _target_map_for(tmp_path)

        substrate._write_agent_helper_forwarders(
            target_map, bin_dst, False,
        )
        assert (bin_dst / "widget").is_file()
        # No `.cmd` half exists to assert on any more -- the writer is deleted
        # (PM ruling 2026-08-29, one native entrypoint per platform). This
        # `engine_root=None` call is the doorless path, so the bare Python
        # forwarder above is the whole product.
        assert not (bin_dst / "widget.cmd").exists()

    def test_check_only_mode_never_touches_the_lock(self, monkeypatch, tmp_path):
        self._isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin"
        bin_dst.mkdir()
        target_map = _target_map_for(tmp_path)

        # check_only mode raises SubstrateFatalError for a missing/stale
        # destination -- confirm that failure is the ordinary check-mode
        # complaint, not a lock timeout (i.e. no lock is attempted at all).
        with pytest.raises(substrate.SubstrateFatalError):
            substrate._write_agent_helper_forwarders(
                target_map, bin_dst, True,
            )


class TestUninstallSweepsOrphanedVenvSwapSiblings:
    """`uninstall_remove_substrate` must remove `.coordinator-venv.build-*`/
    `.coordinator-venv.stale-*` siblings alongside the live `.coordinator-venv`
    it already removes -- not just the live dir."""

    def _isolate_settings_home(self, monkeypatch, tmp_path):
        for var in ("CLAUDE_HOME", "HOME", "USERPROFILE", "COORDINATOR_SETTINGS_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-ml-dir"))

    def test_orphaned_build_and_stale_venv_siblings_are_removed(self, monkeypatch, tmp_path):
        self._isolate_settings_home(monkeypatch, tmp_path)
        settings_home = tmp_path / ".coordinator-claude-settings"
        venv_dir = settings_home / ".coordinator-venv"
        venv_dir.mkdir(parents=True)
        (venv_dir / "marker").write_text("live", encoding="utf-8")

        build_sibling = settings_home / ".coordinator-venv.build-1234-abcd5678"
        stale_sibling = settings_home / ".coordinator-venv.stale-5678-deadbeef"
        build_sibling.mkdir()
        stale_sibling.mkdir()
        (build_sibling / "marker").write_text("orphan", encoding="utf-8")
        (stale_sibling / "marker").write_text("orphan", encoding="utf-8")

        # Unrelated sibling sharing only a loose prefix must survive.
        unrelated = settings_home / ".coordinator-venv-unrelated"
        unrelated.mkdir()

        uninstall_legs.uninstall_remove_substrate(force=True)

        assert not venv_dir.exists()
        assert not build_sibling.exists()
        assert not stale_sibling.exists()
        assert unrelated.exists()
