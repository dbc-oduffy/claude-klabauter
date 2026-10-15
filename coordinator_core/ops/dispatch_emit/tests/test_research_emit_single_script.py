"""One research ask yields one pipeline script; a re-emit rewrites it instead of writing a sibling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research-single")
    return tmp_path


def _emit(capsys, ask):
    assert cli_module.main(["--research", "--ask", ask]) == cli_module.EXIT_OK
    return json.loads(capsys.readouterr().out)


def _scripts(repo):
    return sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))


def test_re_emitting_one_ask_leaves_one_script(repo, capsys):
    first = _emit(capsys, "What is X?")
    second = _emit(capsys, "What is X?")
    assert second["run_id"] == first["run_id"]
    assert len(_scripts(repo)) == 1


def test_a_re_emit_after_a_recorded_submission_mints_a_new_run_id(repo, capsys):
    first = _emit(capsys, "What is X?")
    run_dir = repo / RUN_DIR_ROOT / first["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "submission.json").write_text("{}", encoding="utf-8")
    second = _emit(capsys, "What is X?")
    assert second["run_id"] != first["run_id"]
    assert len(_scripts(repo)) == 2


def test_a_different_ask_gets_its_own_script(repo, capsys):
    a = _emit(capsys, "What is X?")
    b = _emit(capsys, "What is Y?")
    assert a["run_id"] != b["run_id"]
    assert len(_scripts(repo)) == 2
