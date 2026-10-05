"""Pins the warp --ask contract names and the StageManifest round trip."""

import json

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import commit_request


def _manifest():
    rows = (
        c.ManifestRow("C1", "executor", "sonnet", "scratch/warp/r/C1.md", ("a.py", "b.py"), 0),
        c.ManifestRow("C2", "executor", "opus", "scratch/warp/r/C2.md", (), 1),
    )
    return c.StageManifest("scratch/warp/r", rows, ("a.py",), "scratch/warp/r/req.json")


def test_manifest_round_trip():
    m = _manifest()
    assert c.StageManifest.from_json(json.loads(json.dumps(m.to_json()))) == m


def test_phase_order_and_names():
    assert c.ASK_PHASES == ("size", "gate", "plan", "stage", "execute", "review")
    assert (c.OP_ASK_GATE, c.OP_ASK_STAGE) == ("dispatch.ask_gate", "dispatch.ask_stage")
    assert (c.HALT_ROOM, c.HALT_TOUCHPOINT, c.HALT_REFUSAL) == ("room", "touchpoint", "refusal")


def test_marker_distinct_from_commit_request():
    assert c.ASK_MANIFEST_MARKER != commit_request.MARKER_PREFIX
    assert not c.ASK_MANIFEST_MARKER.startswith(commit_request.MARKER_PREFIX)
    assert not commit_request.MARKER_PREFIX.startswith(c.ASK_MANIFEST_MARKER)


def test_gate_verdict_to_json():
    v = c.GateVerdict(None, {"kind": c.HALT_ROOM, "reason": "x"})
    assert v.to_json() == {"arm": None, "halt": {"kind": "room", "reason": "x"}, "baton": None}
