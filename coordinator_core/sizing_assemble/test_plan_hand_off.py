"""`sizing-assemble --write` on an accepted `route: plan` sizing at L or below prints the
`coordinator:engine-ask` submission call (or refuses naming it); any other route prints the decision."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core import warp_fire, warp_live
from coordinator_core.session import record_homes
from coordinator_core.session_baton import store

_COMMON = ["--intent", "Ship the thing", "--premise-provenance", "read", "--premise-evidence", "read it"]
_REL = "state/sizings/2026-10-12-x.yaml"
_ENGINE_ASK_LINE = warp_fire.fire_command(_REL) + "\n"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-ho"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-ho")
    monkeypatch.setattr(warp_live, "live_run_for_sizing", lambda *_a, **_k: None)
    return tmp_path


def _write(repo: Path, tshirt: str, capsys):
    path = Path(record_homes.record_path(str(repo), "sizings", "2026-10-12-x.yaml"))
    code = sizing_assemble.main(["--tshirt", tshirt, *_COMMON, "--write", str(path)])
    out, err = capsys.readouterr()
    return code, out, err, path


def test_plan_at_m_prints_only_the_engine_ask_line(repo, capsys):
    code, out, _err, _path = _write(repo, "M", capsys)
    assert code == 0
    assert out == _ENGINE_ASK_LINE
    assert out.startswith('Workflow({name: "coordinator:engine-ask", args: {"sizing": "')


def test_a_live_run_refuses_naming_the_call_and_the_run(repo, monkeypatch, capsys):
    monkeypatch.setattr(warp_live, "live_run_for_sizing", lambda *_a, **_k: "warp-live-1")
    code, out, err, _path = _write(repo, "M", capsys)
    assert code == sizing_assemble.EXIT_BUSINESS_FAIL
    assert "warp-live-1" in err
    assert warp_fire.fire_command(_REL) in err
    assert json.loads(out)["route"] == "plan"


def test_a_sizing_outside_the_repo_refuses_naming_the_call(repo, tmp_path_factory, capsys):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.yaml"
    code = sizing_assemble._hand_to_engine_ask(str(outside), {"route": "plan"})
    out, err = capsys.readouterr()
    assert code == sizing_assemble.EXIT_BUSINESS_FAIL
    assert "coordinator:engine-ask" in err
    assert json.loads(out)["route"] == "plan"


def test_xs_does_not_hand_off(repo, capsys):
    code, out, _err, _path = _write(repo, "XS", capsys)
    assert code == 0
    assert not out.startswith("Workflow(")
    assert json.loads(out)["route"] != "plan"


@pytest.mark.parametrize("tshirt", ["S", "L"])
def test_s_and_l_print_the_engine_ask_line(repo, capsys, tshirt):
    code, out, _err, _path = _write(repo, tshirt, capsys)
    assert code == 0
    assert out == _ENGINE_ASK_LINE


def test_plan_next_move_names_the_engine_ask_submission(repo, monkeypatch, capsys):
    monkeypatch.setattr(sizing_assemble, "_hand_to_engine_ask", lambda p, d: print(json.dumps(d)) or 0)
    _code, out, _err, _path = _write(repo, "M", capsys)
    move = json.loads(out)["next_move"]
    assert "coordinator:engine-ask" in move
    assert "emit-wave-fire" not in move


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


def test_no_plan_hand_off_at_m_writes_without_printing_the_call(repo, capsys, monkeypatch):
    code, decision, path, opened = _write_no_hand_off(repo, "M", capsys, monkeypatch)
    assert code == 0
    assert opened == []
    assert path.exists()
    assert decision["route"] == "plan"
    assert decision["pm_exit_question"] is None


def test_no_plan_hand_off_at_xl_carries_the_next_move_question(repo, capsys, monkeypatch):
    _code, decision, _path, _opened = _write_no_hand_off(repo, "XL", capsys, monkeypatch)
    question = decision["pm_exit_question"]
    assert question and question in decision["next_move"]


def test_pm_exit_question_on_a_shape_route_is_the_room_entry():
    assert sizing_assemble.pm_exit_question("M", "shape", prompt_pending=False) == "enter coordinator:shape"


def test_xl_keeps_the_pm_touchpoint(repo, capsys):
    code, out, _err, _path = _write(repo, "XL", capsys)
    assert code == 0
    assert json.loads(out)["resolved_estimate"]["tshirt"] == "XL"


def test_a_warp_script_fire_discharges_the_plan_route_obligation():
    from coordinator_core.hooks import watchdog_undischarged_next_move as wd

    plan_terminal = wd._ROUTE_TERMINAL["plan"]
    fire = {"scriptPath": "state/warp/runs/warp-x.workflow.mjs"}
    assert wd._matches_next_action(plan_terminal, "Workflow", fire)
    assert not wd._matches_next_action(plan_terminal, "Workflow", {"scriptPath": "other.mjs"})
