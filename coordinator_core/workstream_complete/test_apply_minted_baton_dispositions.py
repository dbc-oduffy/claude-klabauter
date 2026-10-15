"""A baton this session minted keeps its `handoff_dispositions` at the close without a pickup claim.

Run: python -m pytest coordinator_core/workstream_complete/test_apply_minted_baton_dispositions.py -q
"""

from __future__ import annotations

from coordinator_core.session import claims
from coordinator_core.workstream_complete import apply as _apply
from coordinator_core.workstream_complete import directives_commit_tail


def _baton(root, name, authoring_session):
    d = root / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nauthoring_session: {authoring_session}\ndeployment_state: in_flight\n---\nbody\n",
        encoding="utf-8",
    )


def _fake_store(monkeypatch):
    held: list[str] = []
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: list(held)
    )

    def _claim(basename, baton_repo_root="", cwd=None):
        held.append(basename)
        return True

    monkeypatch.setattr(claims, "claim_handoff", _claim)
    return held


def test_minted_baton_disposition_is_kept_with_no_prior_claim(tmp_path, monkeypatch):
    _baton(tmp_path, "mine.md", "sid-1")
    held = _fake_store(monkeypatch)
    decisions = {"handoff_dispositions": {"mine.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    claimed = _apply._claim_minted_batons(tmp_path, "sid-1", decisions)
    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid-1", decisions)

    assert claimed == ["mine.md"] and held == ["mine.md"]
    assert candidates == [("state/handoffs/mine.md", "deadbeef")]


def test_baton_authored_by_another_session_is_not_claimed(tmp_path, monkeypatch):
    _baton(tmp_path, "theirs.md", "sid-other")
    held = _fake_store(monkeypatch)
    decisions = {"handoff_dispositions": {"theirs.md": {"disposition": "closed", "closed_reason": "stale"}}}

    assert _apply._claim_minted_batons(tmp_path, "sid-1", decisions) == []
    assert held == []


def test_close_tail_claims_minted_batons_before_resolving(tmp_path, monkeypatch):
    _baton(tmp_path, "mine.md", "sid-1")
    _fake_store(monkeypatch)
    seen: list = []

    def _resolve(root, sid, decisions):
        seen.append(directives_commit_tail._held_handoff_basenames(root, sid))
        return []

    monkeypatch.setattr(directives_commit_tail, "resolve_ship_stamp_candidates", _resolve)
    monkeypatch.setattr(
        directives_commit_tail,
        "run_close_commit_and_release_claims",
        lambda root, **kw: directives_commit_tail.CommitTailOutcome(
            committed_sha="abc", pushed=None, push_status="not-attempted", commit_failed=False,
            integrity_breach=False, sha_unverified=False, diagnostics=[],
        ),
    )
    decisions = {
        "subject": "close",
        "handoff_dispositions": {"mine.md": {"disposition": "shipped", "shipped_in": "deadbeef"}},
    }

    _apply._run_close_commit_tail(tmp_path, decisions, "sid-1")

    assert seen and all(s == ["mine.md"] for s in seen)


def test_real_store_claim_is_visible_to_the_held_read_and_not_reclaimed(tmp_path, monkeypatch):
    from coordinator_core.pickup_assemble.tests._git_harness import init_repo

    init_repo(tmp_path)
    _baton(tmp_path, "mine.md", "sid-1")
    cdir = tmp_path / ".git" / "coordinator-sessions" / "handoff-claims" / "mine.md"
    cdir.mkdir(parents=True)
    (cdir / "session_id").write_text("sid-1\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(claims, "claim_handoff", lambda *a, **k: calls.append(a) or True)
    decisions = {"handoff_dispositions": {"mine.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    assert directives_commit_tail._held_handoff_basenames(tmp_path, "sid-1") == ["mine.md"]
    assert _apply._claim_minted_batons(tmp_path, "sid-1", decisions) == []
    assert calls == []


def test_reapply_after_ship_stamp_drops_the_shipped_disposition():
    """A re-apply after the ship stamp landed does not re-stamp or refuse."""
    decisions = {"handoff_dispositions": {"mine.md": {"disposition": "shipped"}, "o.md": {"disposition": "closed"}}}
    out = _apply._undischarged_ship_dispositions(decisions, {"d-ship-consumed-handoff:mine.md": "x"})
    assert list(out["handoff_dispositions"]) == ["o.md"]
