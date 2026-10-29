"""A `no-plan-governed` answer reads the two plan-only gates not-applicable in apply's post-close verdict; unanswered they stay indeterminate."""

import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import apply as ws_apply
from coordinator_core.workstream_complete import completion_verdict as cv

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_GATES = {"open_spine_row_worklist", "landed_reconciliation"}
GATES = {name: {"verdict": "indeterminate"} for name in PLAN_GATES}


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "tracked.txt"], tmp_path)
    _git(["commit", "-q", "-m", "seed"], tmp_path)
    return tmp_path


def _apply(monkeypatch, root: Path, decisions: dict[str, Any]) -> dict[str, Any]:
    from coordinator_core.ops.ceremony import post_commit_tail

    class _Result:
        commit_failed = False
        committed_sha = "deadbeef"
        pushed = False
        integrity_breach = False
        diagnostics: tuple = ()

    monkeypatch.setattr(
        ws_apply.directives_commit_tail, "run_close_commit_and_release_claims", lambda root, **kw: _Result()
    )
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: None)
    monkeypatch.setattr(post_commit_tail, "fold_completion_entry_commit", lambda *a, **k: {"acted": []})
    module = ModuleType("fake_entry")
    module.main = lambda argv: 0
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: module)
    envelope = {
        "artifact": {"path": str(root)},
        "directives": [],
        "judgment_points": [],
        "decisions": decisions,
        "preflight": {"session_shape": {"sid": "sid-mine"}},
        "gates": GATES,
    }
    monkeypatch.setattr(ws_apply, "brief", lambda decisions=None: envelope)
    return ws_apply.apply(decisions=decisions)[1]["completion_verdict"]


@pytest.mark.parametrize("answer", ["no-plan-governed", {"disposition": "no-plan-governed"}, {"value": "no-plan-governed"}])
def test_answered_no_plan_reads_plan_gates_not_applicable(monkeypatch, repo, answer):
    verdict = _apply(monkeypatch, repo, {"subject": "s", "no-governing-plan": answer})
    status = {r["gate"]: r["status"] for r in verdict["readings"]}
    assert {status[g] for g in PLAN_GATES} == {"not-applicable"}
    assert not PLAN_GATES & set(verdict["indeterminate_gates"])


@pytest.mark.parametrize(
    "decisions",
    [{}, {"no-governing-plan": "plan-governed-rerun-with-plan"}, {"no-governing-plan": {"note": "x"}}],
)
def test_unanswered_or_other_answer_stays_indeterminate(monkeypatch, repo, decisions):
    verdict = _apply(monkeypatch, repo, {"subject": "s", **decisions})
    assert PLAN_GATES <= set(verdict["indeterminate_gates"])


def test_predicate_refuses_non_answers():
    assert cv.no_plan_governed({"no-governing-plan": "no-plan-governed"})
    assert not cv.no_plan_governed(None)
    assert not cv.no_plan_governed({"no-governing-plan": 3})
    assert not cv.no_plan_governed({"no-governing-plan": "plan-governed-rerun-with-plan"})
