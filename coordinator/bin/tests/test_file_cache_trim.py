"""Tests for coordinator/bin/lib/file_cache_trim.py against a recording fake host (all OSes)."""

from __future__ import annotations

import sys
from contextlib import contextmanager
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
from file_cache_trim import fire_box_cache_trim, project_after, run  # noqa: E402

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


def _run(host, *argv, platform="win32", fire=lambda: None):
    return run(list(argv), host, platform, sleep=lambda s: None, fire=fire)


class _CP:
    def __init__(self, rc):
        self.returncode = rc


def _fake_schtasks(query_rc=0, run_rc=0, log=None, log_after=None):
    calls = []

    def fake(cmd, **kw):
        verb = cmd[1]
        calls.append(verb)
        if verb == "/run" and log_after is not None:
            log.write_text(log_after)
        return _CP(query_rc if verb == "/query" else run_rc)

    return fake, calls


def test_fire_returns_new_log_tail_after_run(tmp_path):
    log = tmp_path / "trim.log"
    log.write_text("old line\n")
    fake, calls = _fake_schtasks(log=log, log_after="old line\nnew: system cache 11.6 -> 3.0 GB\n")
    assert fire_box_cache_trim(fake, log, lambda s: None) == "new: system cache 11.6 -> 3.0 GB"
    assert calls == ["/query", "/run"]


def test_fire_task_absent_returns_none_without_running(tmp_path):
    fake, calls = _fake_schtasks(query_rc=1)
    assert fire_box_cache_trim(fake, tmp_path / "trim.log", lambda s: None) is None
    assert calls == ["/query"]


def test_fire_run_failure_returns_none(tmp_path):
    fake, _ = _fake_schtasks(run_rc=1)
    assert fire_box_cache_trim(fake, tmp_path / "trim.log", lambda s: None) is None


def test_unelevated_apply_fires_task_instead_of_failing(capsys):
    host = FakeHost(elevated=False)
    assert _run(host, "--apply", fire=lambda: "tail line") == ExitCode.OK
    assert host.calls == ["is_elevated"]
    assert "tail line" in capsys.readouterr().out


def test_unelevated_apply_without_task_still_exits_3():
    assert _run(FakeHost(elevated=False), "--apply", fire=lambda: None) == ExitCode.NOT_ELEVATED


def test_unelevated_dry_run_never_fires_the_task():
    def boom():
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
    assert host.calls == ["is_elevated"]
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
