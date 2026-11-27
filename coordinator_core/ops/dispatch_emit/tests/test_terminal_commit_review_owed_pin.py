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

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def _fire(repo: Path, verdict: str, sidecar: str | None = None, claim: bool = True) -> dict:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / _SIDECAR).write_text(_sidecar(verdict) if sidecar is None else sidecar, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_sidecar_paths": [_SIDECAR],
        "wave_reviews": (
            [{"agent_type": "coordinator:reviewer", "verdict": verdict, "sidecar_path": _SIDECAR}] if claim else []
        ),
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


def _assert_review_owed(repo: Path, out: dict, head: str) -> None:
    assert out.get("committed") is not True, out
    assert not out.get("sha"), out
    assert out.get("refused") == "review-owed", out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_same_ledger_under_a_passing_verdict_commits(repo):
    out = _fire(repo, "PASS")

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


def test_blocked_verdict_with_owed_findings_refuses_the_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "BLOCKED")

    _assert_review_owed(repo, out, head)
    assert f"{_SIDECAR}:F1" in out["error"]


@pytest.mark.parametrize("verdict", ["REQUIRES_CHANGES", "BLOCKED", "Requires Changes"])
def test_standing_verdict_with_no_ledger_rows_refuses_the_commit(repo, verdict):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, verdict, sidecar=f"---\nverdict: {verdict}\n---\n# review\n\nFinding: route.py import is broken.\n")

    _assert_review_owed(repo, out, head)
    assert "no-ledger-rows" in out["error"]


@pytest.mark.parametrize("verdict", ["REQUIRES_CHANGES", "BLOCKED"])
def test_standing_verdict_stated_only_in_the_body_refuses_without_a_relayed_claim(repo, verdict):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, verdict, sidecar=f"# Staff review\n\n**Verdict:** {verdict}\n\nroute.py import is broken.\n", claim=False)

    _assert_review_owed(repo, out, head)


def test_a_quoted_standing_verdict_in_a_blockquote_or_fence_does_not_flip_a_passing_review(repo):
    body = (
        "# Review\n\n> verdict: BLOCKED\n\n```\nVerdict: REQUIRES_CHANGES\n```\n\n**Verdict:** PASS\n"
    )

    out = _fire(repo, "PASS", sidecar=body, claim=False)

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


def test_standing_verdict_whose_findings_are_all_disposed_commits(repo):
    ledger = '[{"id": "F1", "status": "rejected", "severity": "ASK", "file": "a.py"}]'
    sidecar = f"---\nverdict: BLOCKED\n---\n# review\n\n## Findings Ledger\n\n```json\n{ledger}\n```\n"

    out = _fire(repo, "BLOCKED", sidecar=sidecar)

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


def test_a_superseding_passing_verdict_in_the_sidecar_commits(repo):
    out = _fire(repo, "REQUIRES_CHANGES", sidecar="---\nverdict: PASS\n---\n# review\n\nSuperseded.\n")

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out
