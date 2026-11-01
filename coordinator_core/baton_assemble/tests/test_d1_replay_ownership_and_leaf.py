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
import coordinator_core.session.claims as claims
from coordinator_core.test_baton_assemble import _write_artifact

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


def _hold(monkeypatch, *basenames: str) -> None:
    monkeypatch.setattr(
        claims,
        "list_claims_by_session",
        lambda sid, cwd=None: [("handoff-claims", b) for b in basenames],
    )


def _seed_claim(repo: Path, sid: str, basename: str) -> Path:
    d = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    d.mkdir(parents=True)
    (d / "session_id").write_text(sid + "\n", encoding="utf-8")
    (d / "stage").write_text("apply\n", encoding="utf-8")
    return d


# -- predecessor follows continued_into to the live leaf ---------------------


def test_held_claim_on_continued_baton_resolves_to_live_leaf(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _baton(repo, "2026-10-28-a.md", *_continued("example-retrieval-repo:state/handoffs/2026-10-31-b.md"), archived=True)
    _baton(repo, "2026-10-31-b.md", "authoring_session: " + SESSION)
    _hold(monkeypatch, "2026-10-28-a.md")

    primary, additional, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == "state/handoffs/2026-10-31-b.md"
    assert additional == []


def test_leaf_walk_crosses_several_hops(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _baton(repo, "a.md", *_continued("state/handoffs/b.md"), archived=True)
    _baton(repo, "b.md", *_continued("state/handoffs/c.md"))
    _baton(repo, "c.md")
    _hold(monkeypatch, "a.md")

    primary, _, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == "state/handoffs/c.md"


def test_unresolvable_or_cyclic_continued_into_keeps_the_held_baton(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _baton(repo, "dangling.md", *_continued("state/handoffs/gone.md"))
    _baton(repo, "x.md", *_continued("state/handoffs/y.md"))
    _baton(repo, "y.md", *_continued("state/handoffs/x.md"))

    _hold(monkeypatch, "dangling.md")
    assert ba._resolve_held_handoff_for_session(repo, session_id=SESSION)[0] == "state/handoffs/dangling.md"

    _hold(monkeypatch, "x.md")
    assert ba._resolve_held_handoff_for_session(repo, session_id=SESSION)[0] in (
        "state/handoffs/x.md",
        "state/handoffs/y.md",
    )


def test_uncontinued_held_baton_is_unchanged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _baton(repo, "live.md", "claimed_at: 2026-10-28T10:00:00Z", "claimed_by: s")
    _hold(monkeypatch, "live.md")

    assert ba._resolve_held_handoff_for_session(repo, session_id=SESSION) == (
        "state/handoffs/live.md",
        [],
        False,
    )


def test_two_claims_converging_on_one_leaf_collapse_to_it(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _baton(repo, "old.md", *_continued("state/handoffs/new.md"), archived=True)
    _baton(repo, "new.md")
    _hold(monkeypatch, "new.md", "old.md")

    primary, additional, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == "state/handoffs/new.md"
    assert additional == []


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


def _claim_names(repo: Path) -> set[str]:
    d = repo / ".git" / "coordinator-sessions" / "handoff-claims"
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


def test_follow_handoff_claim_moves_the_session_claim_to_the_successor(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, SESSION, "old.md")

    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is True

    assert _claim_names(repo) == {"new.md"}


def test_follow_handoff_claim_drops_the_stale_claim_when_successor_already_held(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, SESSION, "old.md")
    _seed_claim(repo, SESSION, "new.md")

    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is True

    assert _claim_names(repo) == {"new.md"}


def test_follow_handoff_claim_refuses_a_claim_the_session_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, OTHER, "old.md")

    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is False
    assert _claim_names(repo) == {"old.md"}


def test_follow_handoff_claim_refuses_a_successor_held_by_another_session(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, SESSION, "old.md")
    _seed_claim(repo, OTHER, "new.md")

    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is False
    assert _claim_names(repo) == {"old.md", "new.md"}
