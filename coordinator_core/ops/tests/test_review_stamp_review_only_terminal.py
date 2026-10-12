"""A review-only mint stamps the commit its terminal_commit just landed, never an older run's re-verified head."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.tests.test_review_stamp_superseding import _PLAN_ID, _frontmatter, _git, _plan, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_RUN_REL = "state/review-runs/run.md"


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_record(repo: Path) -> dict:
    data = {
        "plan_id": _PLAN_ID,
        "prep": {"run_base_sha": "b" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.txt"]},
        "delivery": {"verdict": "PASS", "product_files": 1},
        "tests": {"status": "pass", "run": 3, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "met", "observation": "held", "sidecar": None},
        "unresolved": [],
        "confinement_violations": 0,
        "fixes_applied": 0,
        "slices": 1,
        "brief_conformance": {"items": 0, "met": 0, "unmet": 0},
    }
    _frontmatter(repo / _RUN_REL, data)
    return data


def _older_reverify(repo: Path, old_head: str) -> None:
    _frontmatter(
        repo / "state" / "delivery-verdicts" / "2026-10" / "v.md",
        {
            "kind": "delivery-verdict",
            "supersedes": _RUN_REL,
            "recorded_at": "2026-10-10T00:00:00Z",
            "head_sha": old_head,
            "delivery": {"verdict": "PASS", "product_files": 1},
        },
    )


def test_landed_terminal_sha_survives_an_older_reverify_of_the_same_record(tmp_path):
    repo, old_head = _repo(tmp_path)
    record = _run_record(repo)
    _older_reverify(repo, old_head)
    landed = _commit(repo, "late.txt", "review-only landing")

    stamp = m.mint(
        _plan(repo), repo, build_test_path=None, resolved=(landed, repo / _RUN_REL, record),
        criterion_open_ok=True,
    )

    assert landed != old_head
    assert stamp["terminal_commit_sha"] == landed
    assert m.check(_plan(repo), repo) is None


def test_trailer_walk_picks_the_newest_inline_review_commit_for_the_plan(tmp_path):
    repo, _ = _repo(tmp_path)
    for stem in ("older", "newer"):
        _frontmatter(
            repo / ".coordinator-local" / "subagent-share" / "s1" / f"{stem}.md",
            {"plan_id": _PLAN_ID, "prep": {"product_files": 1}},
        )
    _commit(repo, "one.txt", "first\n\nInline-Review: applies older")
    newest = _commit(repo, "two.txt", "second\n\nInline-Review: applies newer")

    sha, _, _ = m._resolve_terminal_commit(repo, _PLAN_ID)

    assert sha == newest
