"""Coverage for coordinator_core.benchmarks.warm_saturation_measure: row parsing, sid counts,
occupancy sweep, bar comparison, and the report's bar keys. No live-engine dial."""

from __future__ import annotations

import json

from coordinator_core.benchmarks import warm_saturation_measure as m
from coordinator_core.session_hub_path import hub_for_common


def _proc(op, ms, route="warm_server", spawns=0, sid="s1", **kw):
    return {"kind": "process_time", "op": op, "route": route, "process_ms": ms, "spawns": spawns,
            "t_start": 1000.0, "sid": sid, **kw}


def test_read_rows_filters_window_and_junk(tmp_path):
    sink = hub_for_common(tmp_path / "a" / ".git") / "logs" / "op-latency.jsonl"
    sink.parent.mkdir(parents=True)
    lines = [
        json.dumps({"op": "x", "t_start": 100.0}),
        json.dumps({"op": "y", "t_start": 5.0}),
        "not json t_start",
        json.dumps([1, 2, "t_start"]),
        json.dumps({"op": "nostart"}),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows = m._read_rows(sink, cutoff=50.0, tail_bytes=1 << 20)
    assert [r["op"] for r in rows] == ["x"]
    assert rows[0]["_sink"] == "a"


def test_session_counts_with_and_without_sid():
    rows = [{"sid": "a"}, {"sid": "a"}, {"sid": "b"}, {"sid": None}, {}]
    assert m._session_counts(rows) == (2, 2)


def test_op_table_bar_comparison_and_handler_dedup():
    rows = [
        _proc("hooks.x", 100.0),
        _proc("hooks.x", 600.0, spawns=2),
        _proc("hooks.x", 250.0),
        _proc("hooks.x", 9999.0, measurement_scope="per_op_handler"),
    ]
    t = m._op_table(rows)["hooks.x [warm_server]"]
    assert t["n"] == 3
    assert t["over_500ms"] == 1
    assert t["over_200ms"] == 2
    assert t["spawns"]["total"] == 2
    assert t["process_ms"]["max"] == 600.0


def test_op_table_skips_non_production_origin():
    assert m._op_table([_proc("hooks.x", 900.0, origin="benchmark")]) == {}


def test_occupancy_counts_distinct_pids_with_open_intervals():
    def pair(corr, pid, t, dur_ms):
        return [
            {"kind": "started", "route": "warm_server", "corr_id": corr, "t_start": t, "pid": pid, "op": "o"},
            {"kind": "complete", "route": "warm_server", "corr_id": corr, "elapsed_ms": dur_ms, "t_start": t},
        ]
    rows = pair("c1", 11, 0.0, 10_000) + pair("c2", 12, 1.0, 10_000) + pair("c3", 11, 2.0, 1_000)
    rows.append({"kind": "started", "route": "warm_server", "corr_id": "orphan", "t_start": 3.0, "pid": 13, "op": "o"})
    occ = m._occupancy(rows, 0.0, 20.0)
    assert occ["intervals"] == 3
    assert occ["worker_pids_seen"] == 2
    assert occ["peak_busy_workers"] == 2
    assert occ["seconds_at_or_over_heavy_cap"] == 0.0


def test_occupancy_empty():
    assert m._occupancy([], 0.0, 1.0) == {"intervals": 0}


def test_occupancy_heavy_cap_reached_by_four_pids():
    rows = []
    for i in range(4):
        rows += [
            {"kind": "started", "route": "warm_server", "corr_id": f"c{i}", "t_start": 0.0, "pid": 100 + i, "op": "o"},
            {"kind": "complete", "route": "warm_server", "corr_id": f"c{i}", "elapsed_ms": 5000},
        ]
    occ = m._occupancy(rows, 0.0, 10.0)
    assert occ["peak_busy_workers"] == 4
    assert occ["seconds_at_or_over_heavy_cap"] == 5.0


def test_main_report_carries_bar_and_sid_keys(tmp_path, capsys):
    git = tmp_path / "repo" / ".git"
    logs = hub_for_common(git) / "logs"
    git.mkdir(parents=True)
    logs.mkdir(parents=True)
    import time
    now = time.time()
    rows = [
        {**_proc("hooks.slow", 700.0), "t_start": now - 5},
        {**_proc("hooks.slow", 10.0, sid=None), "t_start": now - 4},
    ]
    (logs / "op-latency.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    rc = m.main(["--repos", str(tmp_path / "repo"), "--runtime-base", str(tmp_path / "rt"), "--check"])
    out = capsys.readouterr().out
    body = out.split("-----BEGIN SATURATION REPORT-----")[1].split("-----END SATURATION REPORT-----")[0]
    rep = json.loads(body)
    assert rep["bar_ms"] == 500
    assert rep["bars_ms"]["process"] == 500.0
    assert rep["rows_without_sid"] == 1
    assert rep["distinct_sessions_in_rows"] == 1
    assert rep["hook_ops_over_500ms_process"] == {"hooks.slow [warm_server]": 1}
    assert rep["whoami_probe"] is None
    assert rc == 1
