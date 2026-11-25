"""phase_accounting: total-tree cost deltas, engine attribution filtered to the caller, the round-log tee.

Run: python -m pytest coordinator/bin/lib/tests/test_phase_accounting.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import phase_accounting as pa  # noqa: E402


def _cost(**over) -> pa.PhaseCost:
    base = dict(wall=1.0, cpu_own=0.5, cpu_total=2.0, spawns=3, files=7, engine_s=0.25, engine_note="",
                t_start=0.0, t_end=1.0)
    base.update(over)
    return pa.PhaseCost(**base)


def test_fields_name_every_figure():
    assert _cost().fields() == "cpu_total=2.000s spawns=3 files=7 engine=0.250s"


def test_an_unreadable_figure_prints_unavailable_never_zero():
    line = _cost(cpu_total=None, spawns=None, engine_s=None, engine_note="unflushed").fields()
    assert line == "cpu_total=unavailable spawns=unavailable files=7 engine=unavailable:unflushed"


def test_files_counter_is_a_delta_per_span():
    acct = pa.PhaseAccount()
    try:
        pa.count_files(5)
        mark = acct.mark()
        pa.count_files(11)
        pa.count_files(0)
        assert acct.cost_since(mark).files == 11
    finally:
        acct.close()


@pytest.mark.spawns_process
def test_a_child_process_is_counted_in_the_span_total():
    acct = pa.PhaseAccount()
    try:
        mark = acct.mark()
        subprocess.run([sys.executable, "-c", "pass"], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cost = acct.cost_since(mark)
    finally:
        acct.close()
    if cost.spawns is None:
        pytest.skip("platform reports no process count")
    assert cost.spawns >= 1
    assert cost.cpu_total is not None and cost.cpu_total >= 0.0


def _telemetry(monkeypatch, tmp_path, rows) -> None:
    from coordinator_core.warm import telemetry

    path = tmp_path / "request-telemetry.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(telemetry, "request_telemetry_path", lambda engine_root=None: path)
    monkeypatch.setattr(telemetry, "flush_requests", lambda engine_root=None: None)


def _row(session, start, end, cpu_ms):
    return {"session": session, "start_ts": start, "end_ts": end, "cpu_ms": cpu_ms}


def test_engine_cpu_counts_only_this_session_inside_the_window(monkeypatch, tmp_path):
    _telemetry(monkeypatch, tmp_path, [
        _row("mine", 10.0, 11.0, 200.0),
        _row("mine", 12.0, 13.0, 300.0),
        _row("peer", 10.0, 11.0, 9000.0),
        _row("mine", 1.0, 2.0, 700.0),
        _row("peer", 20.0, 21.0, 1.0),
    ])
    assert pa.engine_cpu_for_windows([(9.0, 14.0)], "mine") == [(0.5, "")]


def test_engine_is_unavailable_when_the_window_is_not_yet_flushed(monkeypatch, tmp_path):
    _telemetry(monkeypatch, tmp_path, [_row("mine", 10.0, 11.0, 200.0)])
    assert pa.engine_cpu_for_windows([(9.0, 50.0)], "mine") == [(None, "unflushed")]


def test_engine_is_unavailable_without_a_session_id_or_a_telemetry_file(monkeypatch, tmp_path):
    from coordinator_core.warm import telemetry

    assert pa.engine_cpu_for_windows([(0.0, 1.0)], None) == [(None, "no-session-id")]
    monkeypatch.setattr(telemetry, "request_telemetry_path", lambda engine_root=None: tmp_path / "absent.jsonl")
    monkeypatch.setattr(telemetry, "flush_requests", lambda engine_root=None: None)
    [(seconds, why)] = pa.engine_cpu_for_windows([(0.0, 1.0)], "mine")
    assert seconds is None and why == "FileNotFoundError"


def test_attach_engine_fills_each_span_from_one_read(monkeypatch, tmp_path):
    _telemetry(monkeypatch, tmp_path, [_row("mine", 10.0, 11.0, 100.0), _row("mine", 20.0, 21.0, 400.0)])
    out = pa.attach_engine([_cost(t_start=9.0, t_end=12.0), _cost(t_start=19.0, t_end=21.0)], "mine")
    assert [c.engine_s for c in out] == [0.1, 0.4]


def test_the_unattributed_line_is_total_minus_the_listed_spans():
    line = pa.unattributed_line("round", _cost(cpu_total=10.0, spawns=20), 4.0, 5, True)
    assert line == "  [timing] round: unattributed cpu_total=6.000s spawns=15"


def test_the_unattributed_line_never_invents_a_remainder_from_unreadable_spans():
    line = pa.unattributed_line("round", _cost(cpu_total=10.0), 4.0, 5, False)
    assert "cpu_total=unavailable" in line


def test_tee_writes_both_streams_to_the_log_and_exports_its_path(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(pa.ROUND_LOG_ENV, raising=False)
    with pa.tee_round_log(tmp_path, "alpha beta") as path:
        print("to-stdout")
        print("to-stderr", file=sys.stderr)
        import os
        assert os.environ[pa.ROUND_LOG_ENV] == str(path)
    assert path is not None and path.parent == tmp_path / ".coordinator-local" / "publish-rounds"
    assert path.name.endswith("-alpha_beta.log")
    assert path.read_text(encoding="utf-8").splitlines() == ["to-stdout", "to-stderr"]
    captured = capsys.readouterr()
    assert "to-stdout" in captured.out and "to-stderr" in captured.err
    import os
    assert pa.ROUND_LOG_ENV not in os.environ


def test_a_delegated_child_appends_to_the_parents_log(tmp_path, monkeypatch):
    parent_log = tmp_path / "parent.log"
    parent_log.write_text("parent\n", encoding="utf-8")
    monkeypatch.setenv(pa.ROUND_LOG_ENV, str(parent_log))
    with pa.tee_round_log(tmp_path / "elsewhere", "alpha") as path:
        print("child")
    assert path == parent_log
    assert parent_log.read_text(encoding="utf-8") == "parent\nchild\n"
    assert not (tmp_path / "elsewhere").exists()


def test_an_unwritable_log_dir_never_fails_the_round(tmp_path, monkeypatch):
    monkeypatch.delenv(pa.ROUND_LOG_ENV, raising=False)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the repo dir should be", encoding="utf-8")
    with pa.tee_round_log(blocker, "alpha") as path:
        print("still runs")
    assert path is None
