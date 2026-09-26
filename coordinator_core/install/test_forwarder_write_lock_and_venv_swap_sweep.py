
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

    def _isolate_lock_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "lock-root"))

    def test_real_write_loop_blocks_on_a_lock_already_held_for_bin_dst(
        self, monkeypatch, tmp_path
    ):
        self._isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin"
        bin_dst.mkdir()
        target_map = _target_map_for(tmp_path)

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
        assert not (bin_dst / "widget.cmd").exists()

    def test_check_only_mode_never_touches_the_lock(self, monkeypatch, tmp_path):
        self._isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin"
        bin_dst.mkdir()
        target_map = _target_map_for(tmp_path)

        with pytest.raises(substrate.SubstrateFatalError):
            substrate._write_agent_helper_forwarders(
                target_map, bin_dst, True,
            )


class TestUninstallSweepsOrphanedVenvSwapSiblings:

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

        unrelated = settings_home / ".coordinator-venv-unrelated"
        unrelated.mkdir()

        uninstall_legs.uninstall_remove_substrate(force=True)

        assert not venv_dir.exists()
        assert not build_sibling.exists()
        assert not stale_sibling.exists()
        assert unrelated.exists()
