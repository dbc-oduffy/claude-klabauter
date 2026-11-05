"""co_declared_peer_holds: a live unindexed sibling with a manifest is indexed once, never re-read."""

from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit import peer_hunk_gate


def test_live_unindexed_sibling_with_manifest_gets_indexed(tmp_path, monkeypatch):
    idx.rebuild(tmp_path)
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import sibling_claims

    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", lambda *a, **k: ["live-run"])
    monkeypatch.setattr(sibling_claims, "sibling_run_claims", lambda *a, **k: {})
    monkeypatch.setattr(sibling_claims, "halted_records", lambda *a, **k: [])
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda root, run_id: False)
    monkeypatch.setattr(
        warp_run_deliverable, "_manifest_declared", lambda run_id, root: (("a.py", "b.py"), (), False)
    )
    peer_hunk_gate.co_declared_peer_holds(tmp_path, "me", ["a.py"], ["a.py"])
    assert idx.entries(tmp_path)["live-run"].declared == ("a.py", "b.py")


def test_sibling_with_prefixes_or_unreadable_manifest_is_not_indexed(tmp_path, monkeypatch):
    idx.rebuild(tmp_path)
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import sibling_claims

    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", lambda *a, **k: ["pre", "bad"])
    monkeypatch.setattr(sibling_claims, "sibling_run_claims", lambda *a, **k: {})
    monkeypatch.setattr(sibling_claims, "halted_records", lambda *a, **k: [])
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda root, run_id: False)
    monkeypatch.setattr(
        warp_run_deliverable,
        "_manifest_declared",
        lambda run_id, root: (("a.py",), ("d",), False) if run_id == "pre" else (("a.py",), (), True),
    )
    peer_hunk_gate.co_declared_peer_holds(tmp_path, "me", ["a.py"], ["a.py"])
    assert idx.entries(tmp_path) == {}


def _expired(monkeypatch, leg_name):
    from coordinator_core.ops.dispatch_emit import land_deadline

    def check(leg):
        if leg == leg_name:
            raise land_deadline.LandDeadlineExceeded(leg, [], 0.0)

    monkeypatch.setattr(land_deadline, "check", check)
    return land_deadline


def test_without_released_paths_checks_the_deadline_before_reading(tmp_path, monkeypatch):
    import pytest

    from coordinator_core.ops.dispatch_emit import halted_hold_release

    ld = _expired(monkeypatch, "halted_hold_release.without_released_paths")
    with pytest.raises(ld.LandDeadlineExceeded):
        halted_hold_release.without_released_paths(tmp_path, [("a.py", "r1")])
    monkeypatch.setattr(ld, "check", lambda leg: None)
    assert halted_hold_release.without_released_paths(tmp_path, [("a.py", "r1")]) == [("a.py", "r1")]


def test_merge_ancestry_release_propagates_the_deadline_not_swallows_it(tmp_path, monkeypatch):
    import pytest

    from coordinator_core.ops.dispatch_emit import merge_ancestry_release

    ld = _expired(monkeypatch, "merge_ancestry_release.release")
    holds = [("a.py", "r1")]
    with pytest.raises(ld.LandDeadlineExceeded):
        merge_ancestry_release.release(tmp_path, holds)
    monkeypatch.setattr(ld, "check", lambda leg: None)
    assert merge_ancestry_release.release(tmp_path, holds) == holds
