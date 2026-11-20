"""A refire that closes the halt it resumes (superseded-by-refire, engine stamp changed) still records that halt as
`resumed_from`, never an older halted ask run of the same sizing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, halt_resume_record as hrr, halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-11-14-slice.yaml"
OLD_ASK = "ask-20261114T211100-aaaaaa"
HALTED_WARP = "warp-2026-11-14-slice-20261114T221100-bbbbbb"
OLD_ENGINE, NEW_ENGINE = "a" * 40, "b" * 40


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(
        ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
    )
    doc = contract.intake_record(name="slice", turn={"text": "plan it", "turn": 4}, today="2026-11-14")
    (tmp_path / SIZING).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    monkeypatch.setattr(hrr, "engine_sha", lambda: OLD_ENGINE)
    for run in (OLD_ASK, HALTED_WARP):
        halted_run.write_halt_marker(record_dir(tmp_path, run), halted="x", sizing_rel=SIZING)
    monkeypatch.setattr(hrr, "engine_sha", lambda: NEW_ENGINE)
    return tmp_path


def test_the_refire_records_the_halt_it_closed_not_the_older_ask(repo: Path) -> None:
    reply = _dispatch_emit({"ask": True, "sizing_path": SIZING, "target_root": str(repo)})
    assert "error" not in reply
    run = json.loads((record_dir(repo, reply["run_id"]) / "run.json").read_text(encoding="utf-8"))
    assert run["resumed_from"] == HALTED_WARP
