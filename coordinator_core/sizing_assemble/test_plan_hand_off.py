"""`sizing-assemble --write` on an accepted `route: plan` sizing at L or below runs
emit-wave-fire itself and prints only its output; any other route prints the decision."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes
from coordinator_core.session_baton import store

_COMMON = ["--intent", "Ship the thing", "--premise-provenance", "read", "--premise-evidence", "read it"]


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-ho"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-ho")
    return tmp_path


@pytest.fixture()
def emitted(monkeypatch):
    calls: list[list[str]] = []

    class _Emitter:
        @staticmethod
        def main(argv):
            calls.append(argv)
            print("Workflow({scriptPath: 'x.mjs'})")
            return 0

    monkeypatch.setattr(sizing_assemble, "_load_emit_wave_fire", lambda: _Emitter)
    return calls


def _write(repo: Path, tshirt: str, capsys):
    path = Path(record_homes.record_path(str(repo), "sizings", "2026-10-12-x.yaml"))
    code = sizing_assemble.main(["--tshirt", tshirt, *_COMMON, "--write", str(path)])
    out, _err = capsys.readouterr()
    return code, out, path


def test_plan_at_m_fires_and_prints_only_the_workflow_line(repo, emitted, capsys):
    code, out, path = _write(repo, "M", capsys)
    assert code == 0
    assert out == "Workflow({scriptPath: 'x.mjs'})\n"
    (argv,) = emitted
    assert argv[argv.index("--from-sizing") + 1] == path.resolve().relative_to(repo.resolve()).as_posix()
    assert Path(argv[argv.index("--trail-dir") + 1]).parent == repo.resolve() / "state" / "plan-blitz"


def test_a_refused_fire_prints_the_decision_and_fails(repo, monkeypatch, capsys):
    class _Refusing:
        @staticmethod
        def main(argv):
            print("emit-wave-fire: REFUSED — no plugin root")
            return 2

    monkeypatch.setattr(sizing_assemble, "_load_emit_wave_fire", lambda: _Refusing)
    code, out, _path = _write(repo, "M", capsys)
    assert code == sizing_assemble.EXIT_BUSINESS_FAIL
    assert json.loads(out)["route"] == "plan"


def test_xs_does_not_fire(repo, emitted, capsys):
    code, out, _path = _write(repo, "XS", capsys)
    assert code == 0
    assert emitted == []
    assert json.loads(out)["route"] != "plan"


@pytest.mark.parametrize("tshirt", ["S", "L"])
def test_s_and_l_fire(repo, emitted, capsys, tshirt):
    code, out, _path = _write(repo, tshirt, capsys)
    assert code == 0
    assert out == "Workflow({scriptPath: 'x.mjs'})\n"
    assert len(emitted) == 1


def _write_no_hand_off(repo: Path, tshirt: str, capsys, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import NO_PLAN_HAND_OFF_FLAG

    opened: list = []
    monkeypatch.setattr(sizing_assemble, "_open_routed_obligation", lambda p: opened.append(p))
    path = Path(record_homes.record_path(str(repo), "sizings", "2026-10-12-x.yaml"))
    code = sizing_assemble.main(["--tshirt", tshirt, *_COMMON, "--write", str(path),
                                  "--interaction-mode", "hands-on", NO_PLAN_HAND_OFF_FLAG])
    out, _err = capsys.readouterr()
    return code, json.loads(out), path, opened


def test_flag_literal_is_the_ask_contract_constant():
    from coordinator_core.ops.dispatch_emit.ask_contract import NO_PLAN_HAND_OFF_FLAG, PM_EXIT_QUESTION_KEY

    assert NO_PLAN_HAND_OFF_FLAG == "--no-plan-hand-off"
    assert PM_EXIT_QUESTION_KEY == "pm_exit_question"


def test_no_plan_hand_off_at_m_writes_without_firing(repo, emitted, capsys, monkeypatch):
    code, decision, path, opened = _write_no_hand_off(repo, "M", capsys, monkeypatch)
    assert code == 0
    assert emitted == []
    assert opened == []
    assert path.exists()
    assert decision["route"] == "plan"
    assert decision["pm_exit_question"] is None


def test_no_plan_hand_off_at_xl_carries_the_next_move_question(repo, emitted, capsys, monkeypatch):
    _code, decision, _path, _opened = _write_no_hand_off(repo, "XL", capsys, monkeypatch)
    question = decision["pm_exit_question"]
    assert question and question in decision["next_move"]
    assert emitted == []


def test_pm_exit_question_on_a_shape_route_is_the_room_entry():
    assert sizing_assemble.pm_exit_question("M", "shape", prompt_pending=False) == "enter coordinator:shape"


def test_xl_keeps_the_pm_touchpoint(repo, emitted, capsys):
    code, out, _path = _write(repo, "XL", capsys)
    assert code == 0
    assert emitted == []
    assert json.loads(out)["resolved_estimate"]["tshirt"] == "XL"
