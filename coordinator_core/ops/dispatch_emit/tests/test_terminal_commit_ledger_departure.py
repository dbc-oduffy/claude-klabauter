"""dispatch.terminal_commit lands a recorded departure and reports it; a silent one is refused.

Through the op handler at default params: a `departed` row with a reason lands, the reply carries
``ledger_departures`` and the commit a ``Requirement-Departed`` trailer (never ``Requirement-Open``);
a reasonless departure and an evidence-less PM-sourced ``met`` come back ``ledger-unmet``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DLV = "dlv-alpha-aaaaaa"
RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"
WHY = "the checker chose a file-backed store over the asked cache"
_REVIEWED = reviewed()
_SID = "11111111-2222-3333-4444-555555555555"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


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
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id=DLV, plan_path=PLAN)
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    rl.revise(root, RUN, verbatim="use the asked cache")
    monkeypatch.setattr(cr, "_actual_loe", lambda *a, **k: "S")
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda *a, **k: ([], [])
    )
    return root


def _params(verdict):
    return {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict}


def _verdict(second):
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": "a.py"}, {"id": "R2", **second}]}


def test_a_departure_with_a_reason_lands_and_is_reported_and_trailered(repo):
    out = terminal_commit._handler(
        _params(_verdict({"status": "departed", "departure_reason": WHY})), repo_root=repo / ".git"
    )
    assert out["committed"] is True, out
    assert out["ledger_departures"] == [{"id": "R2", "reason": WHY}]
    departed = _git(repo, "log", "-1", "--format=%(trailers:key=Requirement-Departed,valueonly)", out["sha"])
    assert departed.strip() == f"R2 {WHY}"
    assert _git(repo, "log", "-1", "--format=%(trailers:key=Requirement-Open,valueonly)", out["sha"]).strip() == ""
    assert '"status": "met"' not in (repo / SIZING).read_text(encoding="utf-8")


def test_a_run_with_no_departure_reports_none(repo):
    out = terminal_commit._handler(
        _params(_verdict({"status": "met", "wired": True, "met_by": "a.py"})), repo_root=repo / ".git"
    )
    assert out["committed"] is True, out
    assert "ledger_departures" not in out


@pytest.mark.parametrize("second", [
    {"status": "departed"},
    {"status": "departed", "departure_reason": "   "},
])
def test_a_departure_without_a_reason_is_refused(repo, second):
    out = terminal_commit._handler(_params(_verdict(second)), repo_root=repo / ".git")
    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert any("departed without a recorded reason" in f["reason"] for f in out["ledger_failures"])


def test_a_pm_sourced_met_without_evidence_is_refused(repo):
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True}, {"id": "R2", "status": "departed", "departure_reason": WHY}]}
    out = terminal_commit._handler(_params(verdict), repo_root=repo / ".git")
    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert any(f["id"] == "R1" and "met without evidence" in f["reason"] for f in out["ledger_failures"])
