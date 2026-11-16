"""Fleet adoption call site of `percolate-push.py::_cmd_push`: the block prints
after a candidate claude-klabauter push, never changes the exit code, and runs
only after the publish slot is released.

Run: python -m pytest coordinator/bin/tests/test_percolate_push_fleet_adoption.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.percolate import fleet_adoption

_BIN_DIR = Path(__file__).resolve().parent.parent
_SHA = "a" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_push_fleet", _BIN_DIR / "percolate-push.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _args(**over):
    base = dict(target="claude-klabauter", percolate_root=None, promote=False, open_only=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def wired(monkeypatch):
    """Stub everything upstream of the call site; record slot state and fan_out calls."""
    state = {"slot_held": False, "fan_out_while_held": None, "calls": []}

    @contextlib.contextmanager
    def held_slot(*_a, **_k):
        state["slot_held"] = True
        try:
            yield "tok"
        finally:
            state["slot_held"] = False

    _mod._bootstrap_engine()
    monkeypatch.setattr(_mod._publish_slot_mod, "held_slot", held_slot)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda o: "root")
    monkeypatch.setattr(_mod, "_branch0_gate", lambda t, r: object())
    monkeypatch.setattr(_mod, "_resolve_dest", lambda t, r: "dest")
    state["code"] = 0
    monkeypatch.setattr(_mod, "_push_under_slot", lambda *a, **k: state["code"])
    state["branch"] = "candidate"

    def rev_parse(cmd, **_k):
        # git rev-parse semantics: --abbrev-ref names every HEAD after it; a HEAD before it is the sha.
        out, abbrev = [], False
        for arg in cmd[cmd.index("rev-parse") + 1 :]:
            if arg == "--abbrev-ref":
                abbrev = True
            elif arg == "HEAD":
                out.append(state["branch"] if abbrev else _SHA)
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(out) + "\n", stderr="")

    monkeypatch.setattr(_mod, "_run", rev_parse)
    state["rows"] = [
        fleet_adoption.BoxAdoption("machine-a", True, _SHA, ""),
        fleet_adoption.BoxAdoption("machine-b", False, "b" * 40, "diverged"),
    ]

    def fake_fan_out(sha, **_k):
        state["calls"].append(sha)
        state["fan_out_while_held"] = state["slot_held"]
        return state["rows"]

    monkeypatch.setattr(fleet_adoption, "fan_out", fake_fan_out)
    return state


def test_block_prints_on_candidate_push_after_slot_release(wired, capsys):
    assert _mod._cmd_push(_args()) == 0
    out = capsys.readouterr()
    assert wired["calls"] == [_SHA]
    assert wired["fan_out_while_held"] is False
    assert "FLEET ADOPTION" in out.out
    assert "machine-b: NOT ADOPTED diverged" in out.out
    assert "NOT FLEET-LIVE: machine-b" in out.err


def test_block_prints_on_in_sync_rerun(wired, capsys):
    # an in-sync re-run is the same _EXIT_OK return as a fresh push
    wired["code"] = _mod._EXIT_OK
    assert _mod._cmd_push(_args()) == 0
    assert "FLEET ADOPTION" in capsys.readouterr().out


def test_exit_code_unchanged_when_box_not_adopted(wired):
    assert _mod._cmd_push(_args()) == _mod._EXIT_OK


def test_all_adopted_has_no_stderr_line(wired, capsys):
    wired["rows"] = [fleet_adoption.BoxAdoption("machine-a", True, _SHA, "")]
    _mod._cmd_push(_args())
    assert "NOT FLEET-LIVE" not in capsys.readouterr().err


def test_cannot_ask_is_split_from_not_fleet_live(wired, capsys):
    wired["rows"] = [
        fleet_adoption.BoxAdoption("machine-a", True, _SHA, ""),
        fleet_adoption.BoxAdoption("machine-b", False, "b" * 40, "diverged"),
        fleet_adoption.BoxAdoption("mini", False, "", "timeout", fleet_adoption.CANNOT_ASK),
    ]
    assert _mod._cmd_push(_args()) == 0
    err = capsys.readouterr().err
    assert "NOT FLEET-LIVE: machine-b" in err
    assert "mini" not in err.split("CANNOT ASK")[0]
    assert "CANNOT ASK: mini (timeout)" in err
    assert "machine-b (" not in err


def test_only_cannot_ask_has_no_not_fleet_live_line(wired, capsys):
    wired["rows"] = [fleet_adoption.BoxAdoption("mini", False, "", "timeout", fleet_adoption.CANNOT_ASK)]
    assert _mod._cmd_push(_args()) == 0
    err = capsys.readouterr().err
    assert "NOT FLEET-LIVE" not in err
    assert "CANNOT ASK: mini (timeout)" in err


def test_other_target_skips_fan_out(wired, capsys):
    assert _mod._cmd_push(_args(target="claude-other")) == 0
    assert wired["calls"] == []
    assert "FLEET ADOPTION" not in capsys.readouterr().out


def test_non_candidate_branch_skips_fan_out(wired, capsys):
    wired["branch"] = "main"
    _mod._cmd_push(_args())
    assert wired["calls"] == []
    assert "FLEET ADOPTION" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["promote", "open_only"])
def test_promote_and_open_only_skip_fan_out(wired, flag):
    _mod._cmd_push(_args(**{flag: True, **({"open_only": False} if flag == "promote" else {"promote": False})}))
    assert wired["calls"] == []


def test_failed_push_skips_fan_out_and_keeps_code(wired):
    wired["code"] = _mod._EXIT_FAIL
    assert _mod._cmd_push(_args()) == _mod._EXIT_FAIL
    assert wired["calls"] == []


def test_fan_out_crash_does_not_change_exit_code(wired, monkeypatch, capsys):
    def boom(sha, **_k):
        raise RuntimeError("transport down")

    monkeypatch.setattr(fleet_adoption, "fan_out", boom)
    assert _mod._cmd_push(_args()) == 0
    assert "fleet adoption report failed" in capsys.readouterr().err
