"""dispatch.terminal_commit refuses a landing whose standing REQUIRES_CHANGES review still owes findings.

Drives the real handler at default arguments against a temp git repo; the review-owed refusal
(`terminal_commit_review.review_owed_refusal`) is reached through `_tests_not_run_refusal`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

_SIDECAR = "wave-reviewer.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _sidecar(verdict: str) -> str:
    ledger = (
        '[{"id": "F1", "status": "open", "severity": "ASK", "file": "a.py"},'
        ' {"id": "F2", "status": "open", "severity": "ASK", "file": "a.py"}]'
    )
    return f"---\nverdict: {verdict}\n---\n# review\n\n## Findings Ledger\n\n```json\n{ledger}\n```\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, verdict: str) -> dict:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / _SIDECAR).write_text(_sidecar(verdict), encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_sidecar_paths": [_SIDECAR],
        "wave_reviews": [{"agent_type": "coordinator:reviewer", "verdict": verdict, "sidecar_path": _SIDECAR}],
    }
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": review},
        repo_root=repo / ".git",
    )


def test_standing_requires_changes_with_owed_findings_refuses_the_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "REQUIRES_CHANGES")

    assert out.get("committed") is not True, out
    assert not out.get("sha"), out
    assert out.get("refused") == "review-owed", out
    assert "REQUIRES_CHANGES" in out["error"]
    assert f"{_SIDECAR}:F1" in out["error"] and f"{_SIDECAR}:F2" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_same_ledger_under_a_passing_verdict_is_not_refused_as_review_owed(repo):
    out = _fire(repo, "PASS")

    assert out.get("refused") != "review-owed", out
