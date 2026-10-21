"""dispatch.emit persists an ask's route preference and the scout brief renders it.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_ask_route_preference_emit.py -q
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit import route_preference as rp
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

REL = "state/sizings/2026-10-17-intake.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
WHY = "spans two workstreams; PM asked for roadmap-planning"
HEADING = "## EM route preference"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _emit(repo, **extra):
    result = _dispatch_emit({"ask": "add a thing", "repo_root": str(repo), **extra}, repo_root=repo)
    (script,) = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    return result, script.read_text(encoding="utf-8")


def _pref_file(repo, result):
    return repo / RECORD_DIR_ROOT / result["run_id"] / rp.ROUTE_PREFERENCE_FILE


def test_a_raw_ask_with_a_preference_writes_the_record_file_and_renders_the_block(repo):
    result, script = _emit(repo, route_preference="roadmap", em_rationale=WHY)
    saved = json.loads(_pref_file(repo, result).read_text(encoding="utf-8"))
    assert saved == {"route_preference": "roadmap", "em_rationale": WHY}
    assert HEADING in script and "roadmap" in script and WHY in script
    # `_emit` passes no ask_source, and a raw ask defaults to relayed, so the ask text follows "Relayed ask (".
    assert script.index(HEADING) < script.index("Relayed ask (")


def test_each_key_is_independently_optional(repo):
    result, script = _emit(repo, em_rationale=WHY)
    assert json.loads(_pref_file(repo, result).read_text(encoding="utf-8")) == {
        "route_preference": None, "em_rationale": WHY,
    }
    assert HEADING in script and WHY in script


def test_no_preference_writes_no_file_and_adds_no_block_or_slot(repo):
    result, script = _emit(repo)
    assert not _pref_file(repo, result).exists()
    assert HEADING not in script and "omit when it matches" not in script


def test_the_scout_prompt_is_byte_identical_without_a_preference():
    kw = dict(repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
              wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size"]),
              plan_blitz_text="stub", **REVIEW_KW)
    plain = compose_ask_script(**kw)
    assert compose_ask_script(route_preference=rp.RoutePreference(None, None), **kw) == plain
    with_pref = compose_ask_script(route_preference=rp.parse("plan", WHY), **kw)
    assert with_pref != plain and HEADING in with_pref


def test_a_preference_adds_the_override_slot_to_every_scout_gate_command():
    kw = dict(repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
              wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size"]),
              plan_blitz_text="stub", **REVIEW_KW)
    plain = compose_ask_script(**kw)
    script = compose_ask_script(route_preference=rp.parse("goal", None), **kw)
    assert "omit when it matches" not in plain
    assert script.count("omit when it matches") >= 2
    assert re.search(r'writes\\?":\[<your writes>\],\\?"route_override_reason', script)
    assert "reworded sizing routes differently" in script


def test_an_invalid_preference_is_refused_naming_the_key(repo):
    with pytest.raises(ValueError, match="route_preference"):
        _emit(repo, route_preference="shape")
    with pytest.raises(ValueError, match="em_rationale"):
        _emit(repo, em_rationale="x" * 2001)


def _put_intake(repo, **em):
    doc = contract.intake_record(name="intake", turn={"text": "ship it", "turn": 4}, today="2026-10-17")
    doc["em_analysis"].update(em)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_an_intake_slice_carrying_the_keys_persists_and_renders_them(repo):
    _put_intake(repo, route_preference="plan", em_rationale=WHY)
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo)]) == 0
    (script_path,) = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    script = script_path.read_text(encoding="utf-8")
    (saved,) = list((repo / RECORD_DIR_ROOT).rglob(rp.ROUTE_PREFERENCE_FILE))
    assert json.loads(saved.read_text(encoding="utf-8")) == {"route_preference": "plan", "em_rationale": WHY}
    assert HEADING in script and WHY in script


def test_an_intake_slice_without_the_keys_writes_no_file(repo):
    _put_intake(repo)
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo)]) == 0
    assert not list((repo / RECORD_DIR_ROOT).rglob(rp.ROUTE_PREFERENCE_FILE))


def test_either_key_beside_a_non_intake_sizing_path_raises(repo):
    _put_intake(repo)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["status"] = "routed"
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    for extra in ({"route_preference": "goal"}, {"em_rationale": WHY}):
        with pytest.raises(ValueError, match="sizing_path"):
            _dispatch_emit({"sizing_path": REL, "repo_root": str(repo), **extra}, repo_root=repo)


def _compose(route_preference=None):
    kw = dict(repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
              wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size"]),
              plan_blitz_text="stub", **REVIEW_KW)
    return compose_ask_script(route_preference=route_preference, **kw)


_KEY = rp.ROUTE_VERDICT_KEY
_FOLD = f"{_KEY}: _reworded.{_KEY} ?? null"
_INIT = f"{_KEY}: _scouted.{_KEY} ?? null"
_RESULT = f"{_KEY}: _gate?.{_KEY} ?? null"


def test_a_preference_tells_both_scout_tails_to_return_the_verdict():
    script = _compose(rp.parse("goal", None))
    assert f"design_route, {_KEY} when present" in script
    assert script.count(f"design_route, {_KEY}; when the command is denied") == 1


def test_no_preference_leaves_the_scout_tails_without_the_verdict():
    assert f"design_route, {_KEY}" not in _compose()


@pytest.mark.parametrize("pref", [rp.parse("goal", None), None])
def test_the_verdict_fold_gate_initialiser_and_result_are_emitted_with_or_without_a_preference(pref):
    script = _compose(pref)
    assert _FOLD in script and _INIT in script
    assert script.count(_RESULT) == 1
    assert re.search(r"return \{ arm: _gate\.arm,[^\n]*" + re.escape(_RESULT), script)
