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
