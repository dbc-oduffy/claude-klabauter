"""Tests for the swap-window fail-open report: counting door log lines inside a window, the typed
results for a missing home or log, the bounded tail read, and the one advisory line."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.warm import swap_window_report as swr

pytestmark = pytest.mark.complete

T0 = datetime(2026, 11, 12, 7, 6, 50, tzinfo=timezone.utc).timestamp()


def _line(epoch: float, cause: str = "no response within 8000ms of delivery") -> str:
    stamp = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"ts={stamp} deadline_ms=8000 elapsed_ms=8001 op=ping door_pid=1 leg=read leg_ms=8000 cause={cause}\n"


def _write_log(home: Path, *lines: str) -> None:
    log = home.joinpath(*swr.LOG_RELPATH)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(lines), encoding="utf-8")


def test_counts_only_lines_inside_the_window(tmp_path: Path) -> None:
    _write_log(tmp_path, _line(T0 - 30), _line(T0 + 1), _line(T0 + 5, "second"), _line(T0 + 99))
    result = swr.count_fail_opens(tmp_path, T0, T0 + 10)
    assert result.status == swr.STATUS_COUNTED
    assert result.count == 2
    assert result.first_cause == "no response within 8000ms of delivery"


def test_a_line_stamped_the_window_start_second_counts_though_the_window_starts_mid_second(
    tmp_path: Path,
) -> None:
    _write_log(tmp_path, _line(T0))
    assert swr.count_fail_opens(tmp_path, T0 + 0.7, T0 + 3).count == 1
    assert swr.count_fail_opens(tmp_path, T0 + 1.0, T0 + 3).count == 0


def test_a_missing_settings_home_or_log_is_a_typed_result_not_a_raise(tmp_path: Path) -> None:
    assert swr.count_fail_opens(None, T0, T0 + 1).status == swr.STATUS_NO_SETTINGS_HOME
    assert swr.count_fail_opens(tmp_path / "absent", T0, T0 + 1).status == swr.STATUS_NO_LOG
    assert swr.count_fail_opens(tmp_path, T0, T0 + 1).status == swr.STATUS_NO_LOG


def test_an_unreadable_log_is_typed_and_reported_loudly(tmp_path: Path, monkeypatch) -> None:
    _write_log(tmp_path, _line(T0))

    def boom(_log):
        raise PermissionError("denied")

    monkeypatch.setattr(swr, "_tail_lines", boom)
    result = swr.count_fail_opens(tmp_path, T0, T0 + 1)
    assert result.status == swr.STATUS_UNREADABLE
    assert "could not read" in (swr.format_report(result, T0, T0 + 1) or "")


def test_the_tail_read_is_bounded_and_flags_a_window_it_cannot_reach(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(swr, "TAIL_BYTES", 600)
    _write_log(tmp_path, *[_line(T0 + i) for i in range(20)])
    lines, cut = swr._tail_lines(tmp_path.joinpath(*swr.LOG_RELPATH))
    assert cut and 0 < len(lines) < 20
    assert all(ln.startswith("ts=") for ln in lines)
    result = swr.count_fail_opens(tmp_path, T0, T0 + 30)
    assert result.truncated and 0 < result.count < 20
    recent = swr.count_fail_opens(tmp_path, T0 + 19, T0 + 30)
    assert not recent.truncated and recent.count == 1


def test_no_fail_open_in_the_window_prints_nothing(tmp_path: Path) -> None:
    _write_log(tmp_path, _line(T0 - 60))
    err = io.StringIO()
    assert swr.report(T0, T0 + 5, err, settings_home=tmp_path) is None
    assert err.getvalue() == ""


def test_a_fail_open_in_the_window_prints_one_handover_line_naming_count_and_window(tmp_path: Path) -> None:
    _write_log(tmp_path, _line(T0 + 1), _line(T0 + 2))
    err = io.StringIO()
    line = swr.report(T0, T0 + 5, err, settings_home=tmp_path)
    out = err.getvalue()
    assert out == line + "\n"
    assert out.count("\n") == 1 and out.startswith("[handover] 2 guard fail-open(s)")
    assert "07:06:50Z..07:06:55Z" in out and "allowed unevaluated" in out


def test_report_never_raises_on_a_broken_stream(tmp_path: Path) -> None:
    class _Broken(io.StringIO):
        def write(self, _s):
            raise OSError("closed")

    _write_log(tmp_path, _line(T0 + 1))
    assert swr.report(T0, T0 + 5, _Broken(), settings_home=tmp_path) is None
