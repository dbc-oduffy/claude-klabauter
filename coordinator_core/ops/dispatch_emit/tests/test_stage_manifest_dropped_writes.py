"""StageManifest.dropped_writes round-trips through to_json/from_json; a manifest without the key reads as ()."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest

_BASE = {"run_dir": "r", "rows": [], "review_declared_paths": [], "marker_path": "m"}


def test_round_trip_with_dropped_writes() -> None:
    drop = ({"path": "a/b.py", "sentence": "outside the row's declared scope"},)
    m = StageManifest.from_json({**_BASE, "dropped_writes": list(drop)})
    assert m.dropped_writes == drop
    assert StageManifest.from_json(m.to_json()).dropped_writes == drop


def test_missing_key_reads_empty() -> None:
    m = StageManifest.from_json(dict(_BASE))
    assert m.dropped_writes == ()
    assert m.to_json()["dropped_writes"] == []
