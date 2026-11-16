"""Pins for holder_proven_dead: the one definition of a corpse registry row, shared with entry_live."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

SRC = Path(__file__).resolve().parents[1] / "unimatrix-liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("unimatrix_liveness_ghost", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lv = _load()


@pytest.fixture
def alive(monkeypatch):
    monkeypatch.setattr(lv, "pid_alive", lambda pid: True)
    monkeypatch.setattr(lv, "proc_start", lambda pid, platform=None: "start-A")


def test_procstart_mismatch_is_proven_dead(alive):
    d = {"pid": 4242, "procStart": "start-B"}
    assert lv.holder_proven_dead(d) is True
    assert lv.entry_live(d) is False


def test_procstart_match_is_not_dead(alive):
    d = {"pid": 4242, "procStart": "start-A"}
    assert lv.holder_proven_dead(d) is False
    assert lv.entry_live(d) is True


@pytest.mark.parametrize("recorded", [None, "", 7])
def test_live_pid_without_usable_procstart_is_not_proof(alive, recorded):
    d = {"pid": 4242} if recorded is None else {"pid": 4242, "procStart": recorded}
    assert lv.holder_proven_dead(d) is False
    assert lv.entry_live(d) is True


def test_unreadable_start_is_not_proof(monkeypatch):
    monkeypatch.setattr(lv, "pid_alive", lambda pid: True)

    def boom(pid, platform=None):
        raise OSError("cannot read")

    monkeypatch.setattr(lv, "proc_start", boom)
    d = {"pid": 4242, "procStart": "start-A"}
    assert lv.holder_proven_dead(d) is False
    assert lv.entry_live(d) is True


def test_dead_pid_is_proven_dead(monkeypatch):
    monkeypatch.setattr(lv, "pid_alive", lambda pid: False)
    d = {"pid": 4242, "procStart": "start-A"}
    assert lv.holder_proven_dead(d) is True
    assert lv.entry_live(d) is False


@pytest.mark.parametrize("d", [{}, {"pid": "12"}, {"pid": True}, {"pid": None}, {"pid": 1.5}, None, []])
def test_missing_nonint_or_bool_pid_is_proven_dead(d):
    assert lv.holder_proven_dead(d) is True
    assert lv.entry_live(d) is False
