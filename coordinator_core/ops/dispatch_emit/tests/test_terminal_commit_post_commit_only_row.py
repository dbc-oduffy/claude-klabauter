"""A writes-empty post-commit-only row (sha reply/memo) never gates dispatch.terminal_commit: a
product chunk's commit is not refused for it, while a stranded writes-bearing row still refuses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
_SID = "11111111-2222-3333-4444-555555555555"
_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_C2_PARTIAL = "PARTIAL: sha reply owed after the commit\n"


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
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _stage(repo: Path, c1_report: str = "DONE: wired\n") -> None:
    _write(repo, "a.py", "a\n")
    _write(repo, "docs/note.md", "note\n")
    _write(repo, "reports/C1.md", c1_report)
    _write(repo, "reports/C2.md", _C2_PARTIAL)
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",), report="reports/C1.md"),
            ChunkCommit(id="C2", title="reply", paths=(), report="reports/C2.md"),
            ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),
        )
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")


def _review() -> dict:
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1},
        "integration": {"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    }


def _call(repo: Path, incomplete: list, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": incomplete, "inline_review": _review(),
        "session_id": _SID, "ledger_verdict": _VERDICT, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_done_product_chunk_commits_beside_a_partial_writes_empty_row(repo):
    _stage(repo)
    out = _call(repo, ["C2"])
    assert out.get("refused") != "no-credited-chunk-landed", out
    assert out["committed"] is True, out
    assert "a.py" in _git(repo, "log", "--name-only", "--format=")


def test_withheld_product_chunk_is_not_refused_for_the_writes_empty_row(repo):
    _stage(repo)
    out = _call(repo, ["C2"], withhold_paths=[{"path": "a.py", "reason": "held back"}])
    assert out.get("refused") != "no-credited-chunk-landed", out
    assert "a.py" not in _git(repo, "ls-files")


def test_stranded_writes_bearing_row_still_refuses_beside_the_writes_empty_row(repo):
    _stage(repo, "BLOCKED: no runtime\n")
    out = _call(repo, ["C1", "C2"])
    assert out.get("refused") == "no-credited-chunk-landed", out
    assert "a.py" not in _git(repo, "ls-files")
