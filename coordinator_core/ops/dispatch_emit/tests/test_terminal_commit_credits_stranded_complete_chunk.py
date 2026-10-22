"""dispatch.terminal_commit re-grades a stranded complete chunk: a passed ledger plus a clean review
(``ledger-met``) or a ``workflow.revise credit_chunks`` entry (``credited``) lands it, and the
``no-credited-chunk-landed`` refusal names the route."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_credit, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
REPORT = "reports/C1.md"
_SID = "11111111-2222-3333-4444-555555555555"
_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_PARTIAL_REPORT = (
    "status: PARTIAL (C1 in-footprint delivered; 5 tests outside the footprint pin the old emitted text)\n\n"
    "Examined 2 paths, changed 2.\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _review(slice_files=("a.py",), **over) -> dict:
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [],
                 "slice_files": list(slice_files)},
        "delivery": {"verdict": "FAIL", "product_files": 1},
        "integration": {"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    }
    review.update(over)
    return review


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
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _stage(repo: Path, report_text: str | None = _PARTIAL_REPORT) -> None:
    report = ""
    if report_text is not None:
        report = REPORT
        _write(repo, REPORT, report_text)
    _write(repo, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",), report=report),
            ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),
        )
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")


def _call(repo: Path, review: dict, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C1"], "inline_review": review,
        "session_id": _SID, "ledger_verdict": _VERDICT, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_ledger_met_partial_report_in_the_review_slice_lands_and_regrades(repo):
    _stage(repo)
    out = _call(repo, _review())
    assert out["committed"] is True, out
    assert out["regraded"]["C1"].startswith("ledger-met: R1")
    assert "C1" not in out.get("incomplete_chunks", [])
    assert out["stranded"] == {}
    assert "a.py" in _git(repo, "log", "--name-only", "--format=")


def test_blocked_report_stays_stranded_and_the_refusal_names_the_route(repo):
    _stage(repo, "BLOCKED: no runtime\n")
    out = _call(repo, _review())
    assert out["refused"] == "no-credited-chunk-landed"
    assert f"workflow.revise run_id={RUN}" in out["error"] or "workflow.revise run_id=" in out["error"]
    assert "credit_chunks=[{chunk, evidence}]" in out["error"]


def test_partial_report_blocker_text_rides_the_refusal(repo):
    _stage(repo, "PARTIAL: edits done\nNot done: five tests outside the footprint\n")
    out = _call(repo, _review(slice_files=("other.py",)))
    assert out["refused"] == "no-credited-chunk-landed"
    assert "C1 blocker: Not done: five tests outside the footprint" in out["error"]


def test_file_outside_the_slice_is_refused(repo):
    _stage(repo)
    out = _call(repo, _review(slice_files=("other.py",)))
    assert out.get("refused") == "no-credited-chunk-landed", out
    assert "5 tests outside the footprint" in out["error"]
    assert "a.py" not in _git(repo, "ls-files")


@pytest.mark.parametrize(
    "integration",
    [
        {"sidecar": "s", "unresolved": ["F1"], "confinement_violations": 0},
        {"sidecar": "s", "unresolved": [], "confinement_violations": 2},
    ],
    ids=["unresolved", "confinement"],
)
def test_unclean_review_does_not_cover_the_chunk(repo, integration):
    _stage(repo)
    out = _call(repo, _review(integration=integration))
    assert out["refused"] == "no-credited-chunk-landed"


def test_credit_jsonl_lands_a_chunk_the_review_slice_does_not_cover(repo):
    _stage(repo)
    run_credit.record(repo / RECORD_DIR_ROOT / RUN, [{"chunk": "C1", "evidence": "tests pass"}], "t")
    out = _call(repo, _review(slice_files=("other.py",)))
    assert out["committed"] is True, out
    assert out["regraded"]["C1"] == "credited: tests pass"
    assert out["stranded"] == {}


def test_revise_credit_chunks_then_refire_lands_the_chunk(repo):
    _stage(repo)
    review = _review(slice_files=("other.py",))
    assert _call(repo, review)["refused"] == "no-credited-chunk-landed"
    revised = rl._handler(
        {"run_id": RUN, "credit_chunks": [{"chunk": "C1", "evidence": "ledger R1 met; tests pass"}]},
        repo_root=repo,
    )
    assert revised["ok"] is True, revised
    out = _call(repo, review)
    assert out["committed"] is True, out
    assert out["regraded"]["C1"] == "credited: ledger R1 met; tests pass"


def test_held_chunk_named_in_credit_jsonl_stays_stranded(repo):
    _stage(repo)
    run_credit.record(repo / RECORD_DIR_ROOT / RUN, [{"chunk": "C1", "evidence": "ok"}], "t")
    out = _call(repo, _review(), held=[{"chunk": "C1", "reason": "peer"}])
    assert "regraded" not in out
    assert "a.py" not in _git(repo, "ls-files")


def test_done_chunk_is_unaffected(repo):
    _stage(repo, "DONE: wired\n")
    out = _call(repo, _review())
    assert out["committed"] is True, out
    assert "regraded" not in out
    assert out["partial_committed"] == ["C1"]


def test_refusal_leads_with_credit_chunks(repo):
    _stage(repo, "BLOCKED: no runtime\n")
    error = _call(repo, _review())["error"]
    assert error.index("workflow.revise") < error.index("finish or land")


PLAN = "docs/plans/p.md"
EVIDENCE = "docs/plans/p.evidence.yaml"


def _stage_evidence_only(repo: Path, report_text: str) -> None:
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "a")
    _write(repo, REPORT, report_text)
    _write(repo, PLAN, "plan\n")
    _write(repo, EVIDENCE, "rows: []\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report=REPORT),),
        plan_path=PLAN,
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")


def test_own_evidence_append_lands_without_the_halt(repo):
    _stage_evidence_only(repo, "DONE: evidence appended\n")
    out = _call(repo, _review(slice_files=("other.py",)))
    assert out.get("refused") != "no-credited-chunk-landed", out


def test_evidence_append_with_a_blocked_report_still_halts(repo):
    _stage_evidence_only(repo, "BLOCKED: no runtime\n")
    out = _call(repo, _review(slice_files=("other.py",)))
    assert out.get("refused") == "no-credited-chunk-landed", out


def test_done_report_with_no_plan_change_still_halts(repo):
    _stage_evidence_only(repo, "DONE: evidence appended\n")
    _git(repo, "add", PLAN, EVIDENCE)
    _git(repo, "commit", "-q", "-m", "plan")
    out = _call(repo, _review(slice_files=("other.py",)))
    assert out.get("refused") == "no-credited-chunk-landed", out


def test_ledger_unjudged_refuses_before_any_regrade(repo):
    _stage(repo)
    out = _call(repo, _review(), ledger_verdict=None)
    assert out["refused"] == "ledger-unjudged"
