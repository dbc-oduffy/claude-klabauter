"""dispatch.emit honours a sizing's em_analysis.stop_after on the ask route.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_emit_stop_after_from_sizing.py -q
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_contract import OP_ASK_STAGE, RUN_DIR_ROOT, STOP_AFTER_KEY

REL = "state/sizings/2026-10-21-slice.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
STAGE_TEXT = OP_ASK_STAGE


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo, stop=None):
    doc = contract.intake_record(name="slice", turn={"text": "plan it", "turn": 4}, today="2026-10-21")
    if stop is not None:
        doc["em_analysis"][STOP_AFTER_KEY] = stop
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _emit(repo, *extra):
    code = cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo), *extra])
    scripts = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    return code, (scripts[-1].read_text(encoding="utf-8") if scripts else "")


def test_sizing_without_stop_after_composes_stage(repo):
    _put(repo)
    code, script = _emit(repo)
    assert code == 0
    assert STAGE_TEXT in script


def test_sizing_stop_after_plan_composes_no_stage(repo):
    _put(repo, stop="plan")
    code, script = _emit(repo)
    assert code == 0
    assert script and STAGE_TEXT not in script


def test_non_plan_sizing_stop_after_refuses(repo, capsys):
    _put(repo, stop="execute")
    code, script = _emit(repo)
    assert code != 0
    assert not script
    assert "stop_after" in capsys.readouterr().err


def _marked_parent(repo, run_id="ask-20261104T170113-26e46c"):
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_stop_after_marker

    d = record_dir(repo, run_id)
    d.mkdir(parents=True)
    write_stop_after_marker(d, "plan", "test")
    return run_id


def _chained(repo, **extra):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    return _dispatch_emit({"ask": True, "sizing_path": REL, "target_root": str(repo), **extra})


def _latest_script(repo):
    scripts = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    return scripts[-1].read_text(encoding="utf-8") if scripts else ""


def test_chained_emit_inherits_parent_stop_after_and_persists_marker(repo):
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_FILE, record_dir

    _put(repo)
    parent = _marked_parent(repo)
    reply = _chained(repo, chain_parent=parent)
    assert "error" not in reply
    assert STAGE_TEXT not in _latest_script(repo)
    run_dir = record_dir(repo, reply["run_id"])
    assert json.loads((run_dir / STOP_AFTER_FILE).read_text())[STOP_AFTER_KEY] == "plan"
    assert json.loads((run_dir / "run.json").read_text())["chain_parent"] == parent


def test_chained_emit_without_marked_parent_still_stages(repo):
    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_FILE, record_dir

    _put(repo)
    record_dir(repo, "ask-20261104T170113-26e46c").mkdir(parents=True)
    reply = _chained(repo, chain_parent="ask-20261104T170113-26e46c")
    assert STAGE_TEXT in _latest_script(repo)
    assert not (record_dir(repo, reply["run_id"]) / STOP_AFTER_FILE).exists()


def test_chain_parent_without_record_dir_refuses(repo):
    _put(repo)
    with pytest.raises(ValueError, match="chain_parent"):
        _chained(repo, chain_parent="ask-20261104T170113-aaaaaa")


def test_staged_target_run_refuses_inherited_stop_after(repo, monkeypatch):
    import types
    from datetime import datetime as real_datetime

    from coordinator_core.ops.dispatch_emit import op
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    class _FixedNow:
        @staticmethod
        def now():
            return real_datetime(2026, 11, 4, 17, 1, 59)

    monkeypatch.setattr(op, "datetime", _FixedNow)
    monkeypatch.setattr(op, "uuid", types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="9a73a4ffff")))
    _put(repo)
    parent = _marked_parent(repo)
    d = record_dir(repo, "warp-2026-10-21-slice-20261104T170159-9a73a4")
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="stop_after"):
        _chained(repo, chain_parent=parent)


def test_resuming_a_parked_chained_run_does_not_repark_but_a_fresh_successor_does(repo):
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, STOP_AFTER_PLAN, record_dir
    from coordinator_core.ops.dispatch_emit.op import _inherited_stop_after

    parked = _marked_parent(repo, "warp-2026-11-04-slice-20261104T170159-9a73a4")
    halted_run._write(record_dir(repo, parked), {"halted": HALT_PARKED})
    assert _inherited_stop_after(repo, "warp-2026-11-04-slice-20261104T171000-bbbbbb", None, parked) is None
    fresh = _marked_parent(repo, "ask-20261104T170113-26e46c")
    assert _inherited_stop_after(repo, "warp-2026-11-04-slice-20261104T171000-cccccc", fresh) == STOP_AFTER_PLAN
    assert _inherited_stop_after(repo, "warp-2026-11-04-slice-20261104T171000-dddddd", None, fresh) == STOP_AFTER_PLAN


def test_non_ask_route_refuses_marked_chain_parent(repo):
    from coordinator_core.ops.dispatch_emit.op import _refuse_non_ask_chain_parent

    parent = _marked_parent(repo)
    with pytest.raises(ValueError, match="stop_after"):
        _refuse_non_ask_chain_parent({"chain_parent": parent, "target_root": str(repo)})
    _refuse_non_ask_chain_parent({"chain_parent": "ask-20261104T170113-bbbbbb", "target_root": str(repo)})
