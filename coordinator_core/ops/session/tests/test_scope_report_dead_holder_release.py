"""Dead-holder release in the ownership leg: pins both sides of the decision."""

from __future__ import annotations

from coordinator_core.ops.session import scope_report
from coordinator_core.ops.session.scope_report import assert_paths_in_session_scope
from coordinator_core.session import claim_index as ci


def _install(monkeypatch, clear_result, after):
    """First classify says PEER-held; after a clear, re-classify says ``after``."""
    state = {"cleared": [], "classify_calls": []}

    def _classify(session_id, paths, sessions_dir=None, cwd=None):
        state["classify_calls"].append(list(paths))
        cleared = set(state["cleared"])
        return ci.OwnershipAnswer(
            by_path={
                p: (
                    ci.PathOwnership(verdict=after)
                    if p in cleared
                    else ci.PathOwnership(verdict=ci.OWNERSHIP_PEER, peers=["peer"])
                )
                for p in paths
            },
            complete=True,
        )

    def _clear(class_, basename, baton_repo_root="", cwd=None):
        assert class_ == "artifact"
        if clear_result(basename):
            state["cleared"].append(basename)
            return True
        return False

    monkeypatch.setattr(scope_report.claim_index, "classify_paths", _classify)
    monkeypatch.setattr(scope_report.claims, "clear_claim_if_dead", _clear)
    return state


def test_dead_holder_claim_released_then_path_is_mine(monkeypatch):
    state = _install(monkeypatch, lambda p: True, ci.OWNERSHIP_MINE)
    ok, reason = assert_paths_in_session_scope("me", ["a.py"], cwd=".")
    assert ok is True, reason
    assert state["cleared"] == ["a.py"]
    assert state["classify_calls"][1] == ["a.py"]


def test_live_holder_still_denies_and_names_holder(monkeypatch):
    state = _install(monkeypatch, lambda p: False, ci.OWNERSHIP_MINE)
    ok, reason = assert_paths_in_session_scope("me", ["a.py"], cwd=".")
    assert ok is False
    assert "peer" in reason
    assert len(state["classify_calls"]) == 1


def test_released_path_is_not_admitted_as_orphan_without_adoption(monkeypatch):
    _install(monkeypatch, lambda p: True, ci.OWNERSHIP_UNCLAIMED)
    ok, _ = assert_paths_in_session_scope("me", ["a.py"], cwd=".")
    assert ok is False


def test_mixed_pathspec_releases_only_dead_and_denies_live(monkeypatch):
    state = _install(monkeypatch, lambda p: p == "dead.py", ci.OWNERSHIP_MINE)
    ok, reason = assert_paths_in_session_scope("me", ["dead.py", "live.py"], cwd=".")
    assert ok is False
    assert "live.py" in reason
    assert state["cleared"] == ["dead.py"]
    assert state["classify_calls"][1] == ["dead.py"]


def test_clear_raising_leaves_denial_standing(monkeypatch):
    _install(monkeypatch, lambda p: True, ci.OWNERSHIP_MINE)

    def _boom(*a, **k):
        raise OSError("io")

    monkeypatch.setattr(scope_report.claims, "clear_claim_if_dead", _boom)
    ok, _ = assert_paths_in_session_scope("me", ["a.py"], cwd=".")
    assert ok is False
