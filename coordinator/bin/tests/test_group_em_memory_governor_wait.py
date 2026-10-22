"""group-em-memory-governor-wait: starts at end, wakes on the first new complete line."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "group-em-memory-governor-wait.py"
_spec = importlib.util.spec_from_file_location("group_em_memory_governor_wait", _SRC)
assert _spec is not None and _spec.loader is not None
w = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = w
_spec.loader.exec_module(w)


def _appender(path: Path, writes: list[bytes]):
    def sleep(_s: float) -> None:
        if writes:
            with path.open("ab") as fh:
                fh.write(writes.pop(0))
    return sleep


def test_existing_lines_do_not_wake(tmp_path):
    log = tmp_path / "governor.log"
    log.write_bytes(b"GOVERNOR PAUSED pid=1\n")
    assert w.wait_for_line(log, 0, sleep=lambda _s: None, max_polls=3) is None


def test_first_new_line_wakes_including_runaway(tmp_path):
    log = tmp_path / "governor.log"
    log.write_bytes(b"old\n")
    sleep = _appender(log, [b"GOVERNOR RUNAWAY pid=7 footprint=13000MB\nGOVERNOR PAUSED pid=8\n"])
    assert w.wait_for_line(log, 0, sleep=sleep, max_polls=3) == "GOVERNOR RUNAWAY pid=7 footprint=13000MB"


def test_partial_line_waits_for_newline(tmp_path):
    log = tmp_path / "governor.log"
    log.write_bytes(b"")
    sleep = _appender(log, [b"GOVERNOR RES", b"UMED pid=3\r\n"])
    assert w.wait_for_line(log, 0, sleep=sleep, max_polls=4) == "GOVERNOR RESUMED pid=3"


def test_missing_then_truncated_log(tmp_path):
    log = tmp_path / "governor.log"
    calls = []

    def sleep(_s: float) -> None:
        calls.append(1)
        if len(calls) == 1:
            log.write_bytes(b"a long first line that sets the offset\n")
        elif len(calls) == 2:
            log.write_bytes(b"short\n")
    # Created after start: the first write is new. Truncation then resets to 0.
    assert w.wait_for_line(log, 0, sleep=sleep, max_polls=3) == "a long first line that sets the offset"


def test_help_exits_zero():
    import pytest

    with pytest.raises(SystemExit) as exc:
        w.main(["--help"])
    assert exc.value.code == 0


def _record(governor_pid, governor_start, frozen):
    import json

    return json.dumps({"governor_pid": governor_pid, "governor_start_time": governor_start,
                       "frozen": [{"pid": p, "start_time": s} for p, s in frozen]})


def test_live_recorded_governor_is_left_alone(tmp_path):
    pidfile = tmp_path / "frozen.json"
    text = _record(99, 500.0, [(11, 100.0), (12, 100.0)])
    pidfile.write_text(text, encoding="utf-8")
    calls: list = []
    starts = {99: 500.0, 11: 100.0, 12: 100.0}
    assert w.thaw_on_start(pidfile, lambda p, s: calls.append(p), starts.get) == []
    assert calls == []
    assert pidfile.read_text(encoding="utf-8") == text


def test_dead_governor_thaws_only_matching_start_times(tmp_path):
    pidfile = tmp_path / "frozen.json"
    pidfile.write_text(_record(99, 500.0, [(11, 100.0), (12, 100.0)]), encoding="utf-8")
    calls: list = []
    starts = {11: 100.0, 12: 777.0}  # 99 gone; 12 was reused
    assert w.thaw_on_start(pidfile, lambda p, s: calls.append((list(p), s)), starts.get) == [11]
    assert calls == [([11], False)]
    assert not pidfile.exists()


def test_legacy_pidfile_thaws_nothing(tmp_path):
    pidfile = tmp_path / "frozen.json"
    pidfile.write_text("[11, 12]", encoding="utf-8")
    calls: list = []
    assert w.thaw_on_start(pidfile, lambda p, s: calls.append(p), {11: 1.0, 12: 1.0}.get) == []
    assert calls == []


def test_start_thaws_pidfile_leftovers_before_tailing(tmp_path, monkeypatch):
    pidfile = tmp_path / "frozen.json"
    pidfile.write_text(_record(99, 500.0, [(11, 100.0), (12, 100.0)]), encoding="utf-8")
    starts = {11: 100.0, 12: 100.0}
    log = tmp_path / "governor.log"
    order: list = []
    calls: list = []

    def fake_signal(pids, stop):
        calls.append((list(pids), stop))
        order.append("thaw")

    monkeypatch.setattr(w, "wait_for_line", lambda *a, **k: order.append("tail"))
    assert w.main([str(log)], thaw=lambda: w.thaw_on_start(pidfile, fake_signal, starts.get)) == 0
    assert calls == [([11, 12], False)]
    assert order == ["thaw", "tail"]
    assert not pidfile.exists()


def test_start_with_no_pidfile_thaws_nothing(tmp_path):
    calls: list = []
    assert w.thaw_on_start(tmp_path / "absent.json", lambda p, s: calls.append(p)) == []
    assert calls == []
