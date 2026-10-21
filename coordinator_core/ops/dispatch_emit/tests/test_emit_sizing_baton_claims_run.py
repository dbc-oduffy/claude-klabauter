"""dispatch.emit with --sizing records --baton as the run's claimed baton (run-baton.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton, record_dir

REL = "state/sizings/2026-10-21-claim.yaml"
BATON = "state/handoffs/2026-10-21-claim.md"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    (tmp_path / BATON).write_text(
        "---\nhandoff_id: hnd-x-abc123\ndeliverable_id: dlv-x-abc123\n---\nbody\n",
        encoding="utf-8", newline="\n",
    )
    doc = {
        "schema": "sizing-object", "name": "claim fixture", "intent": "claim the baton",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch",
        "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "baton": BATON,
        "exit_criterion": {
            "statement": "done",
            "accepted": {"pm_quote": "y", "on": "2026-10-21", "mode": "pm"},
        },
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def _emit(repo, capsys, *extra):
    rc = cli.main(["--ask", "--sizing", REL, *extra, "--repo-root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    return Path(reply["path"]).name.removesuffix(".workflow.mjs")


def test_emit_with_baton_claims_it_for_the_run(repo, capsys):
    run_id = _emit(repo, capsys, "--baton", BATON)
    assert read_run_baton(repo, run_id) == BATON


def test_emit_without_baton_writes_no_run_baton(repo, capsys):
    run_id = _emit(repo, capsys)
    assert read_run_baton(repo, run_id) is None
    assert not (record_dir(repo, run_id) / "run-baton.json").exists()
