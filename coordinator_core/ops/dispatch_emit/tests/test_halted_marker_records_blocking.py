"""A landed ledger-check halt records its plan and blocking ids in halted.json; other halts do not."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_BLOCKING_FIELD,
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    read_halt_record,
)
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, write_halt_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-16-x-slice-1-20261016T000000-aaaaaa"
SIZING = "state/sizings/2026-10-16-x-slice-1.yaml"
PLAN = "docs/plans/2026-10-16-x.md"


def test_marker_omits_plan_and_blocking_unless_given(tmp_path):
    path = write_halt_marker(tmp_path / "rec", halted="refusal", sizing_rel=SIZING)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "status": "halted", "halted": "refusal", "sizing_rel": SIZING,
    }


def test_marker_writes_plan_and_blocking_when_given(tmp_path):
    path = write_halt_marker(
        tmp_path / "rec", halted="ledger-check", sizing_rel=SIZING, plan_rel=PLAN, blocking=("R1", "R2")
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[HALT_PLAN_FIELD] == PLAN
    assert data[HALT_BLOCKING_FIELD] == ["R1", "R2"]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    (rec / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1"}], "owed": [{"id": "V1"}], "gated": [{"id": "G1"}]}) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def _land(repo, halted: dict) -> dict:
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    return json.loads(_git(["show", f"HEAD:{RECORD_DIR_ROOT}/{RUN}/{HALTED_FILE}"], repo))


def test_landed_ledger_halt_records_plan_and_blocking_and_reads_back(repo):
    marker = _land(
        repo,
        {"halted": "ledger-check", "reason": "r", HALT_PLAN_FIELD: PLAN, "items": [{"id": "R2"}, {"id": "R3"}]},
    )
    assert marker[HALT_PLAN_FIELD] == PLAN
    assert marker[HALT_BLOCKING_FIELD] == ["R2", "R3"]
    record = read_halt_record(repo, RUN)
    assert record is not None
    assert record.plan_rel == PLAN
    assert record.blocking == ("R2", "R3")
    assert record.staged == frozenset({"C1", "V1", "G1"})


def test_landed_refusal_halt_has_neither_key(repo):
    marker = _land(repo, {"halted": "refusal", "reason": "r", HALT_PLAN_FIELD: PLAN, "items": [{"id": "R2"}]})
    assert HALT_PLAN_FIELD not in marker
    assert HALT_BLOCKING_FIELD not in marker
