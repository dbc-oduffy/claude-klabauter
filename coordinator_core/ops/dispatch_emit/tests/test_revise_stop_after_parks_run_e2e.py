"""End to end: an emitted run revised `stop_after="plan"` before stage answers parked from `dispatch.ask_stage`
(no manifest.json), the composed ask script ends that reply as a `halted_by` parked terminal, and the halt-record
`dispatch.terminal_commit` lands the record dir and the run's plan."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage, halted_run, requirements_ledger as rl, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PARKED,
    HALTED_RECORD_PARAM,
    PARKED_REPLY_KEY,
    STOP_AFTER_FILE,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN as STAGEABLE_PLAN, PLAN_REL as STAGEABLE_REL
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261021T000000-bbbbbb"
SIZING = handle._SIZING
PLAN = handle._PLAN
_SIZING_DOC = {
    "schema": "sizing-object", "name": "park fixture", "intent": "exercise park",
    "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
    "fork": None, "xl_exit": None, "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    "deliverable_id": "dlv-park-abc123", "interaction_mode": "pm",
    "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / SIZING).parent.mkdir(parents=True)
    (root / SIZING).write_text(yaml.safe_dump(_SIZING_DOC), encoding="utf-8", newline="\n")
    (root / PLAN).parent.mkdir(parents=True)
    (root / PLAN).write_text("# plan the run authored\n", encoding="utf-8", newline="\n")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=SIZING, run_id=RUN, session_id=None,
        wrap_stage=lambda _t: (handle._BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="run.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _emit_and_revise(root: Path) -> None:
    rl.start(root, RUN)
    rl.bind(root, RUN, SIZING, ask_text="plan then stop")
    reply = rl._handler({"run_id": RUN, "stop_after": "plan"}, repo_root=root)
    assert reply["ok"] is True and reply["stop_after"] == "plan", reply


def test_revised_run_parks_at_stage_halts_parked_and_commits_its_artifacts(repo, monkeypatch):
    _emit_and_revise(repo)
    rec = record_dir(repo, RUN)
    assert (rec / STOP_AFTER_FILE).is_file()

    stage_reply = ask_stage._handler({"run_id": RUN, "plan_path": PLAN, "writes": []}, repo_root=repo)
    assert stage_reply == {PARKED_REPLY_KEY: "plan", "plan_path": PLAN}
    assert not (rec / "manifest.json").exists()

    out = handle._run(_script(monkeypatch), stage_reply)
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_PARKED == "parked", halt
    assert halt["run_id"] == RUN and halt["plan"] == PLAN and halt["sizing"] == SIZING
    assert "not whole" not in str(halt)
    assert "halt-commit" in [c["label"] for c in out["calls"]]

    landed = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halt}, repo_root=repo / ".git"
    )
    assert landed["committed"] is True, landed
    names = set(_git(["show", "--name-only", "--format=", landed["sha"]], repo).split())
    assert PLAN in names, names
    assert {n for n in names if n.startswith(f"state/warp/{RUN}/")}, names
    assert "Halted-By: parked" in _git(["log", "-1", "--format=%B"], repo)
    marker = json.loads((rec / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == halted_run.STATUS_HALTED and marker["halted"] == HALT_PARKED


def test_an_unrevised_run_stages_and_never_parks(repo):
    rl.start(repo, RUN)
    rl.bind(repo, RUN, SIZING, ask_text="plan then stop")

    (repo / STAGEABLE_REL).write_text(STAGEABLE_PLAN, encoding="utf-8", newline="\n")

    stage_reply = ask_stage._handler({"run_id": RUN, "plan_path": STAGEABLE_REL, "writes": []}, repo_root=repo)

    assert PARKED_REPLY_KEY not in stage_reply and "error" not in stage_reply
    assert (record_dir(repo, RUN) / "manifest.json").is_file()
