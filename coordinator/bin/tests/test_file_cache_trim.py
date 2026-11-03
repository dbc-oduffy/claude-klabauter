"""Tests for coordinator/bin/lib/file_cache_trim.py against a recording fake host (all OSes)."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from file_cache_contract import (  # noqa: E402
    FILE_CACHE_MAX_HARD_ENABLE,
    CacheLimits,
    CounterError,
    ExitCode,
    MemorySnapshot,
    PrivilegeError,
    TrimError,
)
from file_cache_trim import TrimFire, fire_box_cache_trim, fire_trim_task, project_after, run  # noqa: E402

GIB = 1024**3
BEFORE = MemorySnapshot(40 * GIB, 10 * GIB, 30 * GIB, 1 * GIB)
AFTER = MemorySnapshot(3 * GIB, 50 * GIB, 60 * GIB, 1 * GIB)
LIMITS = CacheLimits(0, 100 * GIB, 0)


class FakeHost:
    def __init__(self, elevated=True, priv_error=False, memories=None, limits=None,
                 trim_error=None, read_errors=()):
        self.elevated = elevated
        self.priv_error = priv_error
        self.memories = list(memories or [BEFORE, AFTER])
        self.limits = list(limits or [LIMITS, LIMITS])
        self.trim_error = trim_error
        self.read_errors = list(read_errors)  # read indices (0-based) that raise CounterError
        self.calls: list[str] = []
        self.reads = 0
        self.priv_active = False

    def is_elevated(self):
        self.calls.append("is_elevated")
        return self.elevated

    @contextmanager
    def quota_privilege(self):
        self.calls.append("quota_privilege")
        if self.priv_error:
            raise PrivilegeError("1300")
        self.priv_active = True
        try:
            yield
        finally:
            self.priv_active = False
            self.calls.append("privilege_restored")

    def read_memory(self):
        self.calls.append("read_memory")
        idx = self.reads
        self.reads += 1
        if idx in self.read_errors:
            raise CounterError("boom")
        return self.memories[idx]

    def read_limits(self):
        self.calls.append("read_limits")
        return self.limits[self.reads - 1]

    def trim_working_set(self):
        self.calls.append("trim")
        assert self.priv_active
        if self.trim_error:
            raise self.trim_error


def _run(host, *argv, platform="win32", fire=lambda sample: None):
    return run(list(argv), host, platform, sleep=lambda s: None, fire=fire)


FIRED_AT = datetime(2026, 11, 3, 20, 20, 0, tzinfo=timezone.utc)
OLD_RECORD = "2026-11-03T19:26:10.2877861Z\ntrimmed\nsystem cache 14.1 -> 2.6 GB; available 16.9 -> 28.7 GB\n"
NEW_NUMBERS = "system cache 28.5 -> 3.9 GB; available 12.0 -> 35.3 GB"


def _record(stamp, numbers=NEW_NUMBERS, status="trimmed"):
    return f"{stamp}\n{status}\n{numbers}\n"


def _csv_row(status, last="0x0"):
    return f'"HOST","BoxCacheTrim","N/A","{status}","Interactive/Background","11/3/2026","{last}","user"\n'


class _CP:
    def __init__(self, rc, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class FakeSchtasks:
    """Scripted schtasks: `states` is the Status column per query (the last repeats); a /run
    appends `on_run` to the log; `on_sleep` appends text to the log on the Nth sleep."""

    def __init__(self, log, states=("Ready",), query_rc=0, run_rc=0, run_err="", on_run="", last="0x0"):
        self.log, self.states, self.query_rc = log, list(states), query_rc
        self.run_rc, self.run_err, self.on_run, self.last = run_rc, run_err, on_run, last
        self.calls: list[str] = []
        self.sleeps = 0
        self.on_sleep: dict[int, str] = {}

    def __call__(self, cmd, **kw):
        verb = cmd[1]
        self.calls.append(verb)
        if verb == "/query":
            if self.query_rc:
                return _CP(self.query_rc)
            status = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return _CP(0, _csv_row(status, self.last))
        if verb == "/run":
            if self.run_rc == 0 and self.on_run:
                self._append(self.on_run)
            return _CP(self.run_rc, "", self.run_err)
        raise AssertionError(cmd)

    def _append(self, text):
        old = self.log.read_text() if self.log.exists() else ""
        self.log.write_text(old + text)

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps in self.on_sleep:
            self._append(self.on_sleep[self.sleeps])


def _fire(tmp_path, fake, **kw):
    return fire_trim_task(fake, fake.log, fake.sleep, now=lambda: FIRED_AT, **kw)


def _log(tmp_path, text=OLD_RECORD):
    log = tmp_path / "trim.log"
    log.write_text(text)
    return log


def test_fire_reports_the_record_written_after_the_fire(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), on_run=_record("2026-11-03T20:20:06.1234567Z"))
    result = _fire(tmp_path, fake)
    assert result.ok and result.detail == NEW_NUMBERS
    assert fake.calls == ["/query", "/run"]


def test_fire_never_reports_a_stale_record_as_success(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Ready",))
    result = _fire(tmp_path, fake)
    assert not result.ok
    assert "14.1 -> 2.6" not in result.line()
    assert result.line().startswith("NOT TRIMMED:")
    assert "no result record from this run" in result.detail


def test_fire_freshness_boundary_is_the_fire_instant(tmp_path):
    just_before = _fire(tmp_path, FakeSchtasks(_log(tmp_path), states=("Ready",),
                                               on_run=_record("2026-11-03T20:19:59.9999990Z")))
    exactly = _fire(tmp_path, FakeSchtasks(_log(tmp_path), on_run=_record("2026-11-03T20:20:00.0000000Z")))
    assert not just_before.ok
    assert exactly.ok


def test_fire_waits_for_a_record_still_being_written(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Ready", "Running"),
                        on_run="2026-11-03T20:20:01.0000000Z\ntrimmed\n")
    fake.on_sleep[2] = NEW_NUMBERS + "\n"
    result = _fire(tmp_path, fake)
    assert result.ok and result.detail == NEW_NUMBERS
    assert fake.sleeps >= 2


def test_fire_failed_record_is_not_success(tmp_path):
    fake = FakeSchtasks(_log(tmp_path),
                        on_run="2026-11-03T20:20:01.0000000Z\nfailed: SeIncreaseQuotaPrivilege not granted, error 1300\n")
    result = _fire(tmp_path, fake)
    assert not result.ok and "SeIncreaseQuotaPrivilege" in result.detail


def test_fire_task_still_running_before_fire_is_waited_on_then_fails_without_firing(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Running",), last="0x41301")
    result = _fire(tmp_path, fake, wait_seconds=6.0)
    assert not result.ok and "0x41301" in result.detail
    assert "/run" not in fake.calls
    assert result.waited >= 6.0


def test_fire_task_leaving_running_before_fire_then_fires(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Running", "Running", "Ready"),
                        on_run=_record("2026-11-03T20:20:02.0000000Z"))
    assert _fire(tmp_path, fake).ok
    assert fake.calls.count("/query") == 3 and fake.calls[-1] == "/run"


def test_fire_refused_as_already_running_is_failure_not_none(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), run_rc=1, run_err="ERROR: The task is currently running (0x800710E0)")
    result = _fire(tmp_path, fake)
    assert result is not None and not result.ok and "0x800710E0" in result.detail


def test_fire_run_failure_for_another_reason_returns_none(tmp_path):
    assert _fire(tmp_path, FakeSchtasks(_log(tmp_path), run_rc=1, run_err="ERROR: Access is denied.")) is None


def test_fire_bounded_wait_expiry_is_failure_naming_the_task_state(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Ready", "Running"), last="0x41301")
    result = _fire(tmp_path, fake, wait_seconds=4.0)
    assert not result.ok
    assert "after 4s" in result.detail and "Running" in result.detail and "0x41301" in result.detail


def test_fire_task_leaving_running_without_a_record_fails_early(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Ready",), last="0x1")
    result = _fire(tmp_path, fake, wait_seconds=30.0)
    assert not result.ok and "left Running" in result.detail and "0x1" in result.detail
    assert result.waited < 30.0


def test_fire_samples_once_per_poll(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), states=("Ready", "Running"))
    fake.on_sleep[3] = _record("2026-11-03T20:20:03.0000000Z")
    ticks = []
    assert _fire(tmp_path, fake, sample=lambda: ticks.append(1)).ok
    assert len(ticks) == 3


def test_fire_refuses_without_a_live_group_em_and_spawns_nothing(tmp_path):
    fake = FakeSchtasks(_log(tmp_path))
    for live in (lambda: False, lambda: 1 / 0):
        assert fire_box_cache_trim(fake, fake.log, fake.sleep, live_fn=live) is None
    assert fake.calls == []


def test_fire_task_absent_returns_none_without_running(tmp_path):
    fake = FakeSchtasks(_log(tmp_path), query_rc=1)
    assert fire_box_cache_trim(fake, fake.log, fake.sleep) is None
    assert fake.calls == ["/query"]


def test_governor_line_is_this_runs_result_or_not_trimmed(tmp_path):
    fresh = FakeSchtasks(_log(tmp_path), on_run=_record("2026-11-03T20:30:00.0000000Z"))
    line = fire_box_cache_trim(fresh, fresh.log, fresh.sleep, live_fn=lambda: True)
    assert NEW_NUMBERS in line and "this run's result" in line
    stale = FakeSchtasks(_log(tmp_path), states=("Ready",))
    line = fire_box_cache_trim(stale, stale.log, stale.sleep, live_fn=lambda: True)
    assert line.startswith("NOT TRIMMED:") and "14.1 -> 2.6" not in line


def test_governor_wait_is_bounded_below_the_manual_wait(tmp_path):
    import file_cache_trim as fct

    stuck = FakeSchtasks(_log(tmp_path), states=("Running",))
    line = fire_box_cache_trim(stuck, stuck.log, stuck.sleep, live_fn=lambda: True)
    assert line.startswith("NOT TRIMMED:")
    assert stuck.sleeps * fct.STATE_POLL_SECONDS <= fct.GOVERNOR_WAIT_SECONDS + fct.STATE_POLL_SECONDS


class ScriptHost:
    """Unelevated host whose memory reads come from `reads` (the last repeats) and log events."""

    def __init__(self, reads, events=None):
        self.reads = list(reads)
        self.events = events if events is not None else []

    def is_elevated(self):
        return False

    def read_memory(self):
        self.events.append("read")
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]


def snap(cache, avail):
    return MemorySnapshot(int(cache * GIB), int(avail * GIB), 0, 0)


def _apply(host, fired, capsys, events=None):
    def fire(sample):
        if events is not None:
            events.append("fire")
        sample()
        return fired

    def sleep(seconds):
        if events is not None:
            events.append("settle")

    code = run(["--apply"], host, "win32", sleep=sleep, fire=fire)
    out = capsys.readouterr()
    return code, out.out, out.err


OK_FIRE = TrimFire(True, NEW_NUMBERS, 6.2)


def test_apply_real_drop_exits_0_printing_both_measurements(capsys):
    host = ScriptHost([snap(28, 12), snap(28, 12), snap(4.9, 35.9)])
    code, out, err = _apply(host, OK_FIRE, capsys)
    assert code == ExitCode.OK
    assert "28.00 GiB" in out and "4.90 GiB" in out and "35.90 GiB" in out
    assert "waited 6.2s" in out and NEW_NUMBERS in out


def test_apply_unchanged_cache_exits_nonzero_naming_the_numbers(capsys):
    host = ScriptHost([snap(28, 12), snap(28, 12), snap(27.5, 12.4)])
    code, out, err = _apply(host, OK_FIRE, capsys)
    assert code == ExitCode.TRIM_FAILED
    assert "28.00 GiB -> 27.50 GiB" in err and "12.00 GiB -> 12.40 GiB" in err
    assert "did not drop" in err


def test_apply_reads_after_numbers_only_after_the_result_and_the_settle(capsys):
    events = []
    host = ScriptHost([snap(28, 12), snap(28, 12), snap(4, 36)], events)
    _apply(host, OK_FIRE, capsys, events)
    assert events[0] == "read" and events.index("fire") < events.index("settle") < len(events) - 1
    assert events[-1] == "read" and events.count("read") == 4


def test_apply_drop_then_refill_is_told_apart_from_a_trim_that_never_took(capsys):
    host = ScriptHost([snap(28, 12), snap(3, 37), snap(3, 37), snap(27, 13)])
    code, out, err = _apply(host, OK_FIRE, capsys)
    assert code == ExitCode.TRIM_FAILED
    assert "refilled" in err and "concurrent reader" in err and "28.00 GiB -> 27.00 GiB" in err


def test_apply_held_drop_with_partial_refill_exits_0_noting_the_refill(capsys):
    host = ScriptHost([snap(28, 12), snap(3, 37), snap(3, 37), snap(15, 25)])
    code, out, err = _apply(host, OK_FIRE, capsys)
    assert code == ExitCode.OK
    assert "refilled" in out and "concurrent reader" in out


def test_apply_result_not_from_this_run_is_failure_with_no_after_read(capsys):
    host = ScriptHost([snap(28, 12)])
    code, out, err = _apply(host, TrimFire(False, "no result record from this run after 30s wait", 30.0), capsys)
    assert code == ExitCode.TRIM_FAILED
    assert "no result record from this run" in err and "28.00 GiB" in err
    assert host.events.count("read") == 2  # before + the one sample inside fire; never an after-read


def test_apply_tiny_cache_has_nothing_to_trim(capsys):
    host = ScriptHost([snap(0.4, 30), snap(0.4, 30), snap(0.4, 30)])
    code, out, err = _apply(host, OK_FIRE, capsys)
    assert code == ExitCode.OK and "nothing to trim" in out


def test_apply_after_read_failure_exits_7_naming_the_result(capsys):
    class Boom(ScriptHost):
        def read_memory(self):
            self.events.append("read")
            if len(self.events) >= 3:
                raise CounterError("boom")
            return snap(28, 12)

    code, out, err = _apply(Boom([]), OK_FIRE, capsys)
    assert code == ExitCode.COUNTERS_UNAVAILABLE
    assert "could not be re-read" in err


def test_unelevated_apply_without_task_still_exits_3():
    host = ScriptHost([snap(28, 12)])
    assert _run(host, "--apply", fire=lambda sample: None) == ExitCode.NOT_ELEVATED


def test_unelevated_dry_run_never_fires_the_task():
    def boom(sample):
        raise AssertionError("fired")

    assert _run(FakeHost(elevated=False), fire=boom) == ExitCode.NOT_ELEVATED


def test_non_windows_exits_6_with_zero_host_calls(capsys):
    host = FakeHost()
    assert _run(host, platform="linux") == ExitCode.NOT_WINDOWS
    assert host.calls == []


@pytest.mark.parametrize("argv", [(), ("--apply",)])
def test_not_elevated_exits_3_touching_nothing_else(argv, capsys):
    host = FakeHost(elevated=False)
    assert _run(host, *argv) == ExitCode.NOT_ELEVATED
    assert host.calls == ["is_elevated"] + (["read_memory"] if argv else [])
    assert "elevated (Administrator) shell" in capsys.readouterr().err


def test_privilege_error_exits_4_before_any_counter_read():
    host = FakeHost(priv_error=True)
    assert _run(host) == ExitCode.PRIVILEGE_UNAVAILABLE
    assert "read_memory" not in host.calls and "trim" not in host.calls


def test_before_counter_error_exits_7_and_restores_privilege():
    host = FakeHost(read_errors=[0])
    assert _run(host, "--apply") == ExitCode.COUNTERS_UNAVAILABLE
    assert "trim" not in host.calls
    assert host.calls[-1] == "privilege_restored"


def test_dry_run_reports_before_and_projection_without_trimming(capsys):
    host = FakeHost()
    assert _run(host) == ExitCode.OK
    out = capsys.readouterr().out
    assert "before" in out and "after (projected, nothing trimmed)" in out
    for name in ("CacheBytes", "standby", "available"):
        assert out.count(name) == 2
    assert "no trim performed" in out
    assert "trim" not in host.calls
    assert host.calls[-1] == "privilege_restored"
    assert host.reads == 1


def test_apply_trims_exactly_once_and_prints_delta(capsys):
    host = FakeHost()
    assert _run(host, "--apply") == ExitCode.OK
    assert host.calls.count("trim") == 1
    out = capsys.readouterr().out
    assert "delta" in out and "-37.00 GiB" in out and "+40.00 GiB" in out
    assert host.calls[-1] == "privilege_restored"


def test_trim_error_exits_5_with_win32_error(capsys):
    host = FakeHost(trim_error=TrimError(5))
    assert _run(host, "--apply") == ExitCode.TRIM_FAILED
    assert "Win32 error 5" in capsys.readouterr().err
    assert host.reads == 1
    assert host.calls[-1] == "privilege_restored"


def test_flags_changed_after_trim_exits_8(capsys):
    changed = CacheLimits(0, 100 * GIB, FILE_CACHE_MAX_HARD_ENABLE)
    host = FakeHost(limits=[LIMITS, changed])
    assert _run(host, "--apply") == ExitCode.CAP_CHANGED
    assert "CAP CHANGED" in capsys.readouterr().err


def test_after_read_counter_error_exits_7_naming_that_trim_ran(capsys):
    host = FakeHost(read_errors=[1])
    assert _run(host, "--apply") == ExitCode.COUNTERS_UNAVAILABLE
    assert "trim DID run" in capsys.readouterr().err
    assert host.calls[-1] == "privilege_restored"


@pytest.mark.parametrize("argv", [(), ("--apply",)])
def test_preexisting_cap_warns_and_is_left_untouched(argv, capsys):
    capped = CacheLimits(0, 100 * GIB, FILE_CACHE_MAX_HARD_ENABLE)
    host = FakeHost(limits=[capped, capped])
    assert _run(host, *argv) == ExitCode.OK
    out = capsys.readouterr().out
    assert "WARNING" in out and "will not change it" in out


def test_privilege_restore_failure_is_reported_not_masking(capsys):
    class BadRestore(FakeHost):
        @contextmanager
        def quota_privilege(self):
            yield
            raise OSError("restore failed")

    assert _run(BadRestore()) == ExitCode.OK
    assert "failed to restore" in capsys.readouterr().err


def test_help_exits_zero_on_any_platform(capsys):
    with pytest.raises(SystemExit) as exc:
        _run(None, "--help", platform="linux")
    assert exc.value.code == 0


def test_unknown_flag_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        _run(FakeHost(), "--cap")
    assert exc.value.code == ExitCode.USAGE


def test_project_after_moves_cache_into_standby_and_available():
    p = project_after(BEFORE)
    assert p.cache_bytes == 0
    assert p.standby_bytes == BEFORE.standby_bytes + BEFORE.cache_bytes
    assert p.available_bytes == BEFORE.available_bytes + BEFORE.cache_bytes
    assert p.modified_bytes == BEFORE.modified_bytes
