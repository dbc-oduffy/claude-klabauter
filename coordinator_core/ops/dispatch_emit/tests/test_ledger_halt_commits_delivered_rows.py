"""dispatch.land_run on an ask run whose ledger verdict leaves rows open only because the
manifest gated them or they are non-essential: the delivered rows land in one commit that names each
open row in a Requirement-Open trailer, and the register keeps those rows open."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    GatedRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DLV = "dlv-alpha-aaaaaa"
RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"
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
        gated=(GatedRow(id="C9", reason="external_gate", gate="other repo owns it", owner_repo="elsewhere"),),
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    rl.revise(root, RUN, verbatim="and the other repo's piece")
    rl.revise(root, RUN, verbatim="nice to have: a changelog line")
    monkeypatch.setattr(cr, "_actual_loe", lambda r, s: "S")
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _call(repo: Path, verdict: dict) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": verdict,
    }
    return land_run._handler(params, repo_root=repo / ".git")


def _verdict(r3: dict) -> dict:
    return {"items": [
        {"id": "R1", "status": "met", "wired": True},
        {"id": "R2", "status": "not_met", "withheld_by": "C9"},
        {"id": "R3", "status": "not_met", **r3},
    ]}


def test_withheld_and_non_essential_rows_land_the_delivered_rows_and_stay_open(repo):
    out = _call(repo, _verdict({"essential": False}))
    assert out["committed"] is True, out
    assert _git(repo, "log", "--format=%H", "--", "a.py").split() == [out["sha"]]
    open_trailers = _git(repo, "log", "-1", "--format=%(trailers:key=Requirement-Open,valueonly)", out["sha"])
    assert open_trailers.split("\n")[:2] == ["R2 withheld_by C9", "R3 non-essential"]
    assert DLV in _git(repo, "log", "-1", "--format=%(trailers:key=Deliverable-Id,valueonly)", out["sha"])
    assert "a.py" in _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    assert "status: shipped" not in (repo / SIZING).read_text(encoding="utf-8")


def test_an_essential_unmet_row_still_refuses_and_head_is_unchanged(repo):
    before = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, _verdict({}))
    assert out["committed"] is False and out["refused"] == "ledger-unmet"
    assert [f["id"] for f in out["ledger_failures"]] == ["R3"]
    assert _git(repo, "rev-parse", "HEAD") == before


def test_a_withheld_by_naming_no_gated_chunk_refuses(repo):
    verdict = _verdict({"essential": False})
    verdict["items"][1]["withheld_by"] = "C7"
    out = _call(repo, verdict)
    assert out["refused"] == "ledger-unmet"
    assert [f["id"] for f in out["ledger_failures"]] == ["R2"]
