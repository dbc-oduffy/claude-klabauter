"""StageManifest.gated serialisation: round-trip, and an absent key reads as ()."""

from coordinator_core.ops.dispatch_emit.ask_contract import (
    GatedRow,
    ManifestRow,
    StageManifest,
)


def _manifest(**kw):
    return StageManifest(
        run_dir="state/scratch/warp/r",
        rows=(ManifestRow("A", "executor", "m", "b.md", ("a.py",), 0),),
        review_declared_paths=("a.py",),
        marker_path="m.json",
        plan_id="p",
        **kw,
    )


def test_gated_round_trips():
    gated = (
        GatedRow("G1", "external_gate", "owner_repo=x requires=y"),
        GatedRow("G2", "transitive_gate_closure", "via G1"),
    )
    m = _manifest(gated=gated)
    data = m.to_json()
    assert data["gated"] == [
        {"id": "G1", "reason": "external_gate", "gate": "owner_repo=x requires=y",
         "owner_repo": "", "closure_key": None},
        {"id": "G2", "reason": "transitive_gate_closure", "gate": "via G1",
         "owner_repo": "", "closure_key": None},
    ]
    assert StageManifest.from_json(data) == m


def test_absent_gated_key_reads_empty():
    data = _manifest().to_json()
    del data["gated"]
    assert StageManifest.from_json(data).gated == ()
