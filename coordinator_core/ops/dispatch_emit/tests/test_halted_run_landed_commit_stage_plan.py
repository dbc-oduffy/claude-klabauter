"""Landing a commit-stage halt whose record names the run plan queues the close job with plan-reconciliation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "ask-20261115T092847-332070"
UUID = "123e4567-e89b-12d3-a456-426614174000"
PLAN = "docs/plans/2026-11/commit-stage.md"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


def _repo(root: Path, *, plan_in_marker: bool) -> str:
    _git(root, "init", "-q")
    (root / "one").write_text("one", encoding="utf-8")
    _git(root, "add", "--", "one")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "state" / "sizings" / "s.yaml").write_text("route: x\n", encoding="utf-8")
    d = root / "state" / "warp" / RID
    d.mkdir(parents=True)
    halted_run.write_halt_marker(
        d, halted="verify-overlay-red", sizing_rel="state/sizings/s.yaml",
        plan_rel=PLAN if plan_in_marker else None,
    )
    return _git(root, "rev-parse", "HEAD")


def test_commit_stage_halt_naming_the_plan_queues_plan_reconciliation(tmp_path: Path, launches) -> None:
    sha = _repo(tmp_path, plan_in_marker=True)
    close = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, tmp_path)["close"]
    assert close.get("detail") != "halt record names no plan"
    assert close["launched"] is True and "plan-reconciliation" in close["queued"]
    assert launches == [RID]
    legs = run_close.read_close_job(tmp_path, RID, run_close.datetime.now().astimezone())
    assert legs["plan-reconciliation"].status == "queued"
    job = json.loads((tmp_path / ".coordinator-local" / "close-jobs" / f"{RID}.json").read_text(encoding="utf-8"))
    assert job["plan_rel"] == PLAN


def test_commit_stage_halt_naming_no_plan_still_answers_not_applicable(tmp_path: Path, launches) -> None:
    sha = _repo(tmp_path, plan_in_marker=False)
    close = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, tmp_path)["close"]
    assert close == {"status": "not-applicable", "detail": "halt record names no plan"}
    assert launches == []
    assert "plan" not in json.loads((tmp_path / "state" / "warp" / RID / "halted.json").read_text(encoding="utf-8"))


def _emitted_halt(stage_lines: str, *, plan_decl: str) -> dict:
    from coordinator_core.ops.dispatch_emit.tests.test_commit_stage_halt import _run_node

    stubs = (
        "const agent = async () => ({ committed: false }); const phase = () => {}; "
        "const _haltOnUsageLimit = () => {}; const _terminalCommitParamsBlock = () => ''; "
    )
    program = (
        "(async () => {\n" + stubs + plan_decl + "\n"
        "let _halted = null; const _runId = 'r'; const _sizingRel = 's'; const _closeable = true; "
        "let _nextAction = { kind: 'terminal_commit', params: {} }; let _commit = null; const _RUN_TOKEN = 'tok';\n"
        "let _incompleteChunks = []; let _blockedChunks = []; let _blockedRows = []; let _reviseAndRefire = null; "
        "const _mkRevise = () => null;\n"
        + stage_lines + "\nconsole.log(JSON.stringify({ halted: _halted }));\n})();"
    )
    return _run_node(program)["halted"]


def _stages():
    from coordinator_core.ops.dispatch_emit import ask_compose

    def in_run():
        lines: list = []
        ask_compose._append_docs_and_commit(
            lines, head="h", anchor="a", repo_root="/r", run_id=RID, manifest_rel="m.json")
        return "\n".join(lines)

    def roadmap() -> str:
        return "\n".join(ask_compose.roadmap_commit_lines(head="h", anchor="a", repo_root="/r", run_id=RID))

    return {"in_run": in_run, "roadmap": roadmap}


@pytest.mark.parametrize("stage", ["in_run", "roadmap"])
def test_emitted_commit_halt_carries_the_run_plan_and_run_id(stage) -> None:
    halted = _emitted_halt(_stages()[stage](), plan_decl=f"const _planRel = '{PLAN}';")
    assert halted["halted"] == "commit"
    assert halted["plan"] == PLAN
    assert halted["run_id"] == "r"


def test_roadmap_commit_halt_without_a_declared_plan_records_null() -> None:
    halted = _emitted_halt(_stages()["roadmap"](), plan_decl="")
    assert halted["halted"] == "commit"
    assert halted["plan"] is None
