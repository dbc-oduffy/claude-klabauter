"""publish_round: scratch_root round dir lifecycle, reaper, mirror lookup."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys

import pytest

from coordinator_core import machine_resolver, publish_round


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        machine_resolver,
        "registry_get",
        lambda key: str(tmp_path) if key == "paths.scratch_root" else None,
    )
    monkeypatch.delenv(publish_round.ROUND_ENV, raising=False)
    return tmp_path


def _dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc.wait()
    return proc.pid


def test_round_dir_layout_and_normal_exit_removal(scratch):
    with publish_round.round_dir() as rnd:
        assert rnd.is_dir()
        assert rnd.parent == scratch / publish_round._REPO_DIR_NAME
        assert rnd.name.startswith(f"publish-round-{os.getpid()}-")
        assert os.environ[publish_round.ROUND_ENV] == str(rnd)
        assert publish_round.current_round() == rnd
    assert not rnd.exists()
    assert publish_round.ROUND_ENV not in os.environ
    assert publish_round.current_round() is None


def test_round_dir_removed_on_exception_with_readonly_git_objects(scratch):
    with pytest.raises(RuntimeError):
        with publish_round.round_dir() as rnd:
            obj = rnd / "mirror-k" / ".git" / "objects" / "ab"
            obj.mkdir(parents=True)
            f = obj / "cdef"
            f.write_text("x")
            os.chmod(f, stat.S_IREAD)
            raise RuntimeError("boom")
    assert not rnd.exists()


@pytest.mark.skipif(getattr(signal, "SIGTERM", None) is None, reason="no SIGTERM")
def test_round_dir_removed_on_sigterm_unwind(scratch):
    prior = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        with publish_round.round_dir() as rnd:
            handler = signal.getsignal(signal.SIGTERM)
            assert handler is not prior
            handler(signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM
    assert not rnd.exists()
    assert signal.getsignal(signal.SIGTERM) is prior


def test_unset_scratch_root_is_typed_refusal_naming_key(monkeypatch):
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    with pytest.raises(machine_resolver.SettingsKeyMissing) as exc:
        with publish_round.round_dir():
            pass
    assert exc.value.key == "paths.scratch_root"
    assert "paths.scratch_root" in str(exc.value)
    assert publish_round.reap_dead_rounds() == []
    assert publish_round.live_round_mirrors() == []


def test_mirror_dest_only_inside_open_round(scratch):
    with pytest.raises(publish_round.RoundNotOpenError):
        publish_round.mirror_dest("claude_klabauter")
    with publish_round.round_dir() as rnd:
        assert publish_round.mirror_dest("claude_klabauter") == rnd / "mirror-claude_klabauter"
    with pytest.raises(publish_round.RoundNotOpenError):
        publish_round.mirror_dest("claude_klabauter")


def test_current_round_ignores_env_naming_a_missing_dir(scratch, monkeypatch):
    monkeypatch.setenv(publish_round.ROUND_ENV, str(scratch / "gone"))
    assert publish_round.current_round() is None


def test_reap_dead_rounds_removes_dead_keeps_live_and_foreign(scratch):
    root = scratch / publish_round._REPO_DIR_NAME
    dead = root / f"publish-round-{_dead_pid()}-aabbccdd"
    (dead / "mirror-k").mkdir(parents=True)
    foreign = root / "not-a-round"
    foreign.mkdir()
    with publish_round.round_dir() as live:
        removed = publish_round.reap_dead_rounds()
        assert removed == [dead]
        assert live.is_dir()
    assert not dead.exists()
    assert foreign.is_dir()


def test_is_round_path_refuses_round_candidates_and_accepts_engine_root(scratch):
    engine_script = scratch / "engine" / "unimatrix_box_mux.py"
    engine_script.parent.mkdir()
    engine_script.write_text("")
    assert not publish_round.is_round_path(engine_script)
    assert not publish_round.is_round_path(scratch / publish_round._REPO_DIR_NAME / "not-a-round" / "x.py")
    with publish_round.round_dir() as rnd:
        assert publish_round.is_round_path(rnd)
        assert publish_round.is_round_path(publish_round.mirror_dest("k") / "coordinator" / "bin" / "x.py")


def test_is_round_path_false_when_scratch_root_unset(monkeypatch):
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert publish_round.is_round_path("anything") is False


def test_live_round_mirrors_lists_only_live_owner_mirrors(scratch):
    root = scratch / publish_round._REPO_DIR_NAME
    dead = root / f"publish-round-{_dead_pid()}-aabbccdd"
    (dead / "mirror-k").mkdir(parents=True)
    with publish_round.round_dir() as live:
        mine = publish_round.mirror_dest("k")
        mine.mkdir()
        assert publish_round.live_round_mirrors() == [mine]
        assert live.is_dir()
