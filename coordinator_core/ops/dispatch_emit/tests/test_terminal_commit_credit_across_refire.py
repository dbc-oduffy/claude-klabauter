"""dispatch.terminal_commit honours a ``credit_chunks`` entry recorded on an earlier, already-committed run
of the same sizing: the refired run grades the credited chunk ``credited: <evidence>``."""

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

RUN = "ask-20261019T120000-bbbbbb"
EARLIER = "ask-20261019T100000-aaaaaa"
SIZING = "state/sizings/2026-10-19-refire-fixture.yaml"
REPORT = "reports/C1.md"
_SID = "11111111-2222-3333-4444-555555555555"
_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_PARTIAL_REPORT = "status: PARTIAL (C1 in-footprint delivered; tests outside the footprint)\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _review() -> dict:
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [],
                 "slice_files": ["other.py"]},
        "delivery": {"verdict": "FAIL", "product_files": 1},
        "integration": {"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    }


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
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, "a.py", "a\n")
    _write(root, REPORT, _PARTIAL_REPORT)
    _write(root, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",), report=REPORT),
            ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),
        )
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    rl.start(root, EARLIER, all_of_pm_ask=False)
    rl.bind(root, EARLIER, SIZING)
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _credit_earlier_and_commit_it(repo: Path) -> None:
    run_credit.record(repo / RECORD_DIR_ROOT / EARLIER, [{"chunk": "C1", "evidence": "tests pass"}], "t")
    _git(repo, "add", "-f", RECORD_DIR_ROOT)
    _git(repo, "commit", "-q", "-m", "warp: record halted earlier")


def _call(repo: Path, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C1"], "inline_review": _review(),
        "session_id": _SID, "ledger_verdict": _VERDICT, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_committed_credit_on_an_earlier_run_lands_the_chunk(repo):
    _credit_earlier_and_commit_it(repo)
    out = _call(repo)
    assert out.get("refused") != "no-credited-chunk-landed", out
    assert out["committed"] is True, out
    assert out["regraded"]["C1"] == "credited: tests pass"
    assert "a.py" in _git(repo, "log", "--name-only", "--format=")


def test_held_chunk_is_still_not_credited(repo):
    _credit_earlier_and_commit_it(repo)
    out = _call(repo, held=[{"chunk": "C1", "reason": "peer"}])
    assert "regraded" not in out
    assert "a.py" not in _git(repo, "ls-files")


def test_no_credit_anywhere_keeps_the_refusal(repo):
    out = _call(repo)
    assert out["refused"] == "no-credited-chunk-landed"
    assert "credit_chunks=[{chunk, evidence}]" in out["error"]
