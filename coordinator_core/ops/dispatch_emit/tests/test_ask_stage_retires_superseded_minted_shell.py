"""dispatch.ask_stage retires a minted ask-id shell as superseded when a successor run stages its sizing."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.warp_run_deliverable import _stale_at_minted

SIZING_REL = "state/sizings/2026-10-01-xs-fixture.yaml"
SHELL = "ask-20261001T000000-aaaaaa"
SUCCESSOR = "warp-2026-10-01-xs-fixture-20261001T010000-bbbbbb"
OTHER_SIZING = "state/sizings/2026-10-01-other.yaml"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {"intent": "Fix the thing", "exit_criterion": {"statement": "it works"}, "deliverable_id": "dlv-xs-abc123"}
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    write_run_scripts(tmp_path, SUCCESSOR)
    return tmp_path


def _shell(root, run_id=SHELL, sizing=SIZING_REL, stage_name="minted", plan_started=True):
    rdir = root / RECORD_DIR_ROOT / run_id
    rdir.mkdir(parents=True)
    data = {"stage": stage_name, "started_at": "2026-10-01T00:00:00Z", "last_activity_at": "2026-10-01T00:00:00Z"}
    if plan_started:
        data["plan_started_at"] = "2026-10-01T00:00:30Z"
    (rdir / "run-stage.json").write_text(json.dumps(data), encoding="utf-8")
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    run_token.mint(root, run_id, "scratch/w.mjs", session_id="peer", sizing_rel=sizing)
    return rdir


def _marker(rdir):
    return json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))


def _stage(repo):
    return stage(repo, run_id=SUCCESSOR, sizing_rel=SIZING_REL, writes=["pkg/a.py"])


def test_successor_stage_retires_the_minted_shell_naming_the_successor(repo):
    rdir = _shell(repo)
    _stage(repo)
    marker = _marker(rdir)
    assert marker["status"] == halted_run.STATUS_SUPERSEDED
    assert marker["superseded_by"] == SUCCESSOR
    assert marker[halted_run.SUPERSEDED_EVIDENCE_FIELD]["kind"] == halted_run.SUPERSEDED_BY_SUCCESSOR
    assert json.loads((rdir / "run-stage.json").read_text(encoding="utf-8"))["stage"] == "halted"
    assert not run_token.is_live(repo, SHELL)
    assert halted_run.halted_ask_run_for(repo, SIZING_REL) is None


def test_the_reaper_never_reads_a_retired_shell_as_stale_at_minted(repo):
    _shell(repo)
    _stage(repo)
    assert _stale_at_minted(repo, SHELL, now=4102444800.0) is False


def test_a_shell_of_another_sizing_is_left_at_minted(repo):
    rdir = _shell(repo, sizing=OTHER_SIZING)
    _stage(repo)
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert json.loads((rdir / "run-stage.json").read_text(encoding="utf-8"))["stage"] == "minted"


def test_a_run_past_minted_is_not_retired(repo):
    rdir = _shell(repo, stage_name="staged", plan_started=False)
    _stage(repo)
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_a_shell_minted_after_the_successor_is_not_retired(repo):
    rdir = _shell(repo, run_id="ask-20261001T020000-cccccc")
    _stage(repo)
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_an_unstarted_shell_with_no_successor_stays_reapable(repo):
    rdir = _shell(repo, plan_started=False)
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert _stale_at_minted(repo, SHELL, now=4102444800.0) is True


def test_mark_shell_superseded_refuses_an_existing_marker(repo):
    rdir = _shell(repo)
    (rdir / halted_run.HALTED_FILE).write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    assert halted_run.mark_shell_superseded(rdir, successor=SUCCESSOR, sizing_rel=SIZING_REL, at="2026-10-01T01:00:00Z") is False
    assert _marker(rdir)["status"] == "halted"
