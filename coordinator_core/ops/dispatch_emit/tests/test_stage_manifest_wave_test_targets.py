"""StageManifest.wave_test_targets: optional serialisation and absent-key read."""

import json

from coordinator_core.ops.dispatch_emit import ask_contract as c


def _manifest(**kw):
    return c.StageManifest("scratch/warp/r", (), ("a.py",), "scratch/warp/r/req.json", **kw)


def test_round_trip_with_field():
    m = _manifest(wave_test_targets={"0": ("t/a.py",), "1": ("t/a.py", "t/b.py")})
    out = json.loads(json.dumps(m.to_json()))
    assert out["wave_test_targets"] == {"0": ["t/a.py"], "1": ["t/a.py", "t/b.py"]}
    assert c.StageManifest.from_json(out) == m


def test_empty_field_not_emitted_and_round_trips():
    m = _manifest()
    out = m.to_json()
    assert "wave_test_targets" not in out
    assert c.StageManifest.from_json(out) == m


def test_absent_key_reads_empty_dict():
    data = _manifest().to_json()
    assert c.StageManifest.from_json(data).wave_test_targets == {}
    data["wave_test_targets"] = None
    assert c.StageManifest.from_json(data).wave_test_targets == {}
