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
