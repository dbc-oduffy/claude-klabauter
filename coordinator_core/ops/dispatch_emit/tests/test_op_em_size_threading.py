"""dispatch.emit accepts the EM's `em_size`, refuses a value outside the size set, and threads it into the composed ask script.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_op_em_size_threading.py -q
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


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


def test_em_size_is_accepted_and_rides_the_composed_script(repo):
    result, script = _emit(repo, em_size="m")
    assert "error" not in result
    assert 'em_size: "M"' in script


def test_no_em_size_leaves_the_script_without_it(repo):
    _result, script = _emit(repo)
    assert "em_size:" not in script


def test_em_size_outside_the_size_set_is_refused(repo):
    with pytest.raises(ValueError, match="em_size"):
        _emit(repo, em_size="HUGE")
    assert not list((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
