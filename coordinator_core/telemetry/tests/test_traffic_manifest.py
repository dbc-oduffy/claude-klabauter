"""Tests for coordinator_core.telemetry.traffic_manifest (synthetic sinks only)."""

import json

from coordinator_core.telemetry import engine_report, traffic_manifest


def _w(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _sink(tmp_path):
    cur = tmp_path / "op-latency.jsonl"
    old = tmp_path / "op-latency.1.jsonl"
    _w(old, [{"op": "a.b", "t_start": 1.0, "outcome": "ok", "origin": "production", "caller": "x.y"}])
    _w(cur, [
        {"op": "a.b", "t_start": 2.0, "outcome": "error", "kind": "complete", "origin": "production", "caller": None},
        {"op": "a.b", "t_start": 3.0, "outcome": "ok", "kind": "started", "origin": "production"},
        {"op": "a.b", "t_start": 4.0, "outcome": "ok", "kind": "complete", "origin": "test"},
        {"op": "c.d", "t_start": 5.0, "outcome": "ok", "kind": "complete"},
        {"op": "c.d", "t_start": 6.0, "outcome": "ok", "kind": "complete", "origin": "benchmark"},
    ])
    return [cur, old]


def test_fold(tmp_path):
    m = traffic_manifest.build_manifest(tmp_path, sink_paths=_sink(tmp_path))
    assert m["schema_version"] == "1"
    assert m["excluded"] == {"non_complete_kinds": 1, "non_production_origins": 2}
    eng = m["legs"]["engine_served"]
    assert eng["status"] == "measured" and eng["total"] == 3
    ab = eng["ops"]["a.b"]
    assert ab["count"] == 2 and ab["errors"] == 1
    assert ab["by_caller"] == {"x.y": 1, "null": 1}
    assert ab["by_origin"] == {"production": 2}
    assert m["source"]["generations"] == 2 and m["source"]["rows_scanned"] == 6
    assert m["source"]["window"] == {"t_first": 1.0, "t_last": 5.0}
    assert m["source"]["rows_truncated"] is False
    assert m["legs"]["resident_bash"] == {
        "status": "pending", "blocked_on": "B-β", "total": None, "entry_points": None,
    }


def test_legacy_row_without_kind_and_origin_counts(tmp_path):
    m = traffic_manifest.build_manifest(tmp_path, sink_paths=_sink(tmp_path))
    # legacy rows: a.b (old gen, no kind) and c.d (no origin -> "unknown")
    assert m["legs"]["engine_served"]["ops"]["c.d"]["by_origin"] == {"unknown": 1}


def test_truncation_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_report, "MAX_ROWS_SCANNED", 3)
    m = traffic_manifest.build_manifest(tmp_path, sink_paths=_sink(tmp_path))
    assert m["source"]["rows_truncated"] is True
    assert m["source"]["rows_scanned"] == 3


def test_main_writes_only_with_out(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert traffic_manifest.main([]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == "1"
    assert [p.name for p in tmp_path.iterdir()] == [".git"]
    out = tmp_path / "m.json"
    assert traffic_manifest.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text())["legs"]["resident_bash"]["status"] == "pending"
