"""dispatch.terminal_commit refuses, before any git write, a landing whose reviewer verdict is
REQUIRES_CHANGES with findings not present in the landed tree."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import REFUSED_REVIEW_OWED
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIDECAR = "reviewer-sidecar.md"


def _review(repo, rows: list, verdict: str = "REQUIRES_CHANGES") -> dict:
    (repo / _SIDECAR).write_text(
        f"---\nverdict: {verdict}\n---\n\n## Findings Ledger\n\n```json\n{json.dumps(rows)}\n```\n",
        encoding="utf-8", newline="",
    )
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": len(rows),
        "wave_reviews": [{"verdict": verdict, "sidecar_path": str(repo / _SIDECAR)}],
        "wave_sidecar_paths": [_SIDECAR],
    }


def _rows() -> list:
    applied = {"id": "finding-1", "file": "ok.py", "before": "x = 0", "after": "x = 1",
               "status": "applied", "verified": True}
    ask = {"id": "finding-2", "file": "ok.py", "before": "y = 0", "after": "y = 2",
           "status": "handed-to-em", "verified": True}
    return [applied, ask]


def test_requires_changes_with_owed_findings_refuses_before_any_git_write(repo):
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "ok.py", inline_review=_review(repo, _rows()))

    assert out["committed"] is False
    assert out["refused"] == REFUSED_REVIEW_OWED
    assert "reviewer-sidecar.md:finding-2" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_same_landing_commits_once_the_owed_finding_is_disposed(repo):
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    rows = _rows()
    rows[1]["status"] = "rejected"

    out = _fire(repo, "ok.py", inline_review=_review(repo, rows))

    assert out["committed"] is True, out
