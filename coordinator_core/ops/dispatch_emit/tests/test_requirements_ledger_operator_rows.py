"""An unmet ledger row withheld by an `execution_mode: operator` plan row classifies awaiting_operator:
the delivered rows still land and the reply names each pending operator command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, OwedRow, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DLV = "dlv-alpha-aaaaaa"
RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"
COMMAND = "python scratch/walk-cost/run_units.py --units d1 d2b"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_SID = "11111111-2222-3333-4444-555555555555"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_judge_classifies_an_operator_withheld_row_awaiting_operator_with_its_command():
    ledger = rl.Ledger((rl.Item("R1", "a", "pm", "t"), rl.Item("R2", "b", "pm", "t")), False)
    verdict = {"items": [
        {"id": "R1", "status": "met", "wired": True},
        {"id": "R2", "status": "not_met", "withheld_by": "C2"},
    ]}
    result = rl.judge(ledger, verdict, operator={"C2": COMMAND})
    assert result["pass"] is True
    assert result["open"] == [{"id": "R2", "reason": "awaiting_operator C2", "row": "C2", "command": COMMAND}]


def test_judge_still_fails_a_withheld_by_naming_neither_a_gated_nor_an_operator_row():
    ledger = rl.Ledger((rl.Item("R1", "a", "pm", "t"),), False)
    verdict = {"items": [{"id": "R1", "status": "not_met", "withheld_by": "C7"}]}
    result = rl.judge(ledger, verdict, operator={"C2": COMMAND})
    assert result["pass"] is False and result["open"] == []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship a.py\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\nprime_exit_criterion:\n"
        "  statement: done\n  falsifier:\n    how: pytest t.py::test_x -q\n    expected_when_true: passes\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n"
        "- id: C2\n  title: op\n  execution_mode: operator\n  disposition: open\n  body: |\n    run it\n```\n",
    )
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id=DLV, plan_path=PLAN)
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        owed=(OwedRow(id="C2", reason="operator", detail=COMMAND),),
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    rl.revise(root, RUN, verbatim="and the box-slot measurement")
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def test_delivered_row_commits_and_the_operator_row_reads_awaiting_operator_with_its_command(repo):
    verdict = {"items": [
        {"id": "R1", "status": "met", "wired": True},
        {"id": "R2", "status": "not_met", "withheld_by": "C2"},
    ]}
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C2"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": verdict,
    }
    out = land_run._handler(params, repo_root=repo / ".git")
    assert out["committed"] is True, out
    assert "awaiting_operator" not in out
    assert "a.py" in _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    trailers = _git(repo, "log", "-1", "--format=%(trailers:key=Requirement-Open,valueonly)", out["sha"])
    assert trailers.strip() == "R2 awaiting_operator C2"
