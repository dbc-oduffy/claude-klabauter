"""Handoff apply: d1 replay claims only a baton this run could have scaffolded,
the session-held predecessor resolves to the live leaf, and the session's
handoff claim can follow the minted baton.

Fixtures use a bare `.git` directory (no spawned git): claim paths resolve by
filesystem walk.
"""

from __future__ import annotations

from pathlib import Path


import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _write_artifact

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SESSION = "sess-current-0001"
OTHER = "sess-other-0002"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _baton(repo: Path, name: str, *extra: str, archived: bool = False) -> Path:
    base = "archive/handoffs/2026-10" if archived else "state/handoffs"
    return _write_artifact(repo / base / name, ["kind: session-handoff", *extra])


def _continued(into: str) -> list[str]:
    return [
        "claimed_at: 2026-10-28T10:00:00Z",
        "claimed_by: test-session",
        "deployment_state: continued",
        f"continued_into: {into}",
    ]


# -- predecessor follows continued_into to the live leaf ---------------------


# -- d1 replay ownership -----------------------------------------------------


def _lineage_for_continued_predecessor(repo: Path, target_fm: list[str]):
    recorded = "state/handoffs/2026-10-31-prior-successor.md"
    pred = _baton(
        repo,
        "2026-10-30-pred.md",
        *_continued(recorded),
        "handoff_id: hnd-pred-1a2b4c",
    )
    _baton(repo, "2026-10-31-prior-successor.md", *target_fm)
    lineage = ba.resolve_lineage("handoff", str(pred), repo, session_id=SESSION)
    return lineage, recorded


def test_replay_of_a_baton_another_session_authored_mints_a_fresh_path(tmp_path):
    repo = _repo(tmp_path)
    lineage, recorded = _lineage_for_continued_predecessor(repo, ["authoring_session: " + OTHER])

    assert lineage["output_path"] != recorded
    assert "resumed_successor" not in lineage or not lineage["resumed_successor"]
    d1 = next(d for d in ba._build_directives("handoff", lineage, root=repo) if d["id"] == "d1")
    assert d1["already_satisfied"] is False


@pytest.mark.parametrize(
    "target_fm",
    [["authoring_session: " + SESSION], []],
    ids=["this-run-authored", "pre-stamp-no-authoring-session"],
)
def test_replay_of_this_runs_own_scaffold_still_resumes(tmp_path, target_fm):
    repo = _repo(tmp_path)
    lineage, recorded = _lineage_for_continued_predecessor(repo, target_fm)

    assert lineage["output_path"] == recorded
    d1 = next(d for d in ba._build_directives("handoff", lineage, root=repo) if d["id"] == "d1")
    assert d1["already_satisfied"] is True


# -- the session claim follows the minted baton ------------------------------
