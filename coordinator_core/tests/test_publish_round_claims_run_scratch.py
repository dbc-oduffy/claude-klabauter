"""publish_round claims its round dir through run_scratch and finishes it at round end."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core import machine_resolver, publish_round, run_scratch


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


def test_round_dir_is_a_scratch_claim_and_absent_after_close(scratch):
    with publish_round.round_dir() as rnd:
        assert rnd.parent == scratch / publish_round._REPO_DIR_NAME / "publish-round"
        assert run_scratch._NAME_RE.match(rnd.name)
        assert rnd.name.endswith(f".{os.getpid()}")
        mark = run_scratch.read_mark(rnd)
        assert mark["pid"] == os.getpid() and mark["complete"] is False
        assert os.environ[publish_round.ROUND_ENV] == str(rnd)
    assert not rnd.exists()
    assert publish_round.ROUND_ENV not in os.environ


def test_round_dir_finished_on_exception(scratch):
    with pytest.raises(RuntimeError):
        with publish_round.round_dir() as rnd:
            (rnd / "mirror-k").mkdir()
            raise RuntimeError("boom")
    assert not rnd.exists()


def test_unset_scratch_root_refuses_naming_key_and_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    monkeypatch.setattr(run_scratch.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(machine_resolver.SettingsKeyMissing) as exc:
        with publish_round.round_dir():
            pass
    assert exc.value.key == "paths.scratch_root"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.spawns_process
def test_sigkill_left_round_is_removed_by_next_claim_sweep(scratch):
    kind_dir = scratch / publish_round._REPO_DIR_NAME / "publish-round"
    dead = run_scratch.claim(publish_round._REPO_DIR_NAME, "publish-round", "round", pid=_dead_pid())
    (dead / "mirror-k").mkdir()
    assert dead.is_dir() and dead.parent == kind_dir
    with publish_round.round_dir() as live:
        assert not dead.exists()
        assert live.is_dir()


@pytest.mark.spawns_process
def test_live_owner_round_survives_a_sweep_and_dead_one_does_not(scratch):
    dead = run_scratch.claim(publish_round._REPO_DIR_NAME, "publish-round", "round", pid=_dead_pid())
    with publish_round.round_dir() as live:
        dead2 = run_scratch.claim(publish_round._REPO_DIR_NAME, "publish-round", "round", pid=_dead_pid())
        removed = publish_round.reap_dead_rounds()
        assert dead2 in removed and not dead2.exists()
        assert live.is_dir()
    assert not dead.exists()


@pytest.mark.spawns_process
def test_live_round_mirrors_and_is_round_path_follow_the_claim_layout(scratch):
    dead = run_scratch.claim(publish_round._REPO_DIR_NAME, "publish-round", "round", pid=_dead_pid())
    (dead / "mirror-k").mkdir()
    with publish_round.round_dir() as rnd:
        mine = publish_round.mirror_dest("k")
        mine.mkdir()
        assert publish_round.live_round_mirrors() == [mine]
        assert publish_round.is_round_path(rnd)
        assert publish_round.is_round_path(mine / "coordinator" / "bin" / "x.py")
        assert not publish_round.is_round_path(rnd.parent / "not-a-round" / "x.py")
