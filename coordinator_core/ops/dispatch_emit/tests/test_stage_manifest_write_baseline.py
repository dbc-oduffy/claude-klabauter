from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_contract as c


def _manifest(**kw):
    rows = (c.ManifestRow("C1", "executor", "sonnet", "b.md", ("a.py",), 0, adopts_stranded=True),)
    return c.StageManifest("scratch/warp/r", rows, ("a.py",), "scratch/warp/r/req.json", **kw)


def test_round_trip_baseline_and_flag():
    m = _manifest(write_baseline={"a.py": "abc", "gone.py": None})
    data = json.loads(json.dumps(m.to_json()))
    assert data["write_baseline"] == {"a.py": "abc", "gone.py": None}
    assert data["rows"][0]["adopts_stranded"] is True
    back = c.StageManifest.from_json(data)
    assert back.write_baseline == {"a.py": "abc", "gone.py": None}
    assert back.rows[0].adopts_stranded is True


def test_old_manifest_reads_empty_and_false():
    m = _manifest()
    data = m.to_json()
    assert "write_baseline" not in data
    data["rows"][0].pop("adopts_stranded", None)
    back = c.StageManifest.from_json(data)
    assert back.write_baseline == {}
    assert back.rows[0].adopts_stranded is False


def test_flag_default_not_serialised():
    row = c.ManifestRow("C1", "executor", "sonnet", "b.md", (), 0)
    assert "adopts_stranded" not in row.to_json()
