"""
Tests for coordinator_core.ops.capture_fan_out_threshold.

Mirrors the oracle's coverage (Port of: capture-fan-out-threshold.test.sh,
Example-doctrine-repo a2fe06f8, 2026-07-22, AC7): absent-key write, no-clobber re-run, env-clobber
non-suppression (env layer is irrelevant here since this module only ever
calls `machine-local keys`/`set`, never reads
MACHINE_LOCAL_FAN_OUT_LARGE_WAVE_THRESHOLD itself), --check-only no-mutation,
--check-only pre-existing, and bad-flag usage error.

`machine-local` is stubbed via monkeypatched subprocess.run so these tests
are hermetic (no real registry touched, no `machine-local` binary required
on PATH).
"""

from __future__ import annotations

import os
from typing import List

import pytest

from coordinator_core.ops import capture_fan_out_threshold as mod


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeRegistry:
    """In-memory stand-in for the machine-local registry keyed layer."""

    def __init__(self, keys: List[str] | None = None):
        self._keys = set(keys or [])
        self.set_calls: list[tuple[str, str]] = []

    def run(self, argv, **kwargs):
        # The CLI is now RESOLVED to a concrete argv rather than invoked by bare
        # name — a bare "machine-local" is unrunnable on Windows (extension-less
        # wrapper -> WinError 193; CreateProcess ignores PATHEXT -> WinError 2).
        # So the prefix may be ["…/machine-local"], ["…/machine-local.cmd"], or
        # [sys.executable, "…/_machine_local.py"]. Assert the CLI's IDENTITY, not
        # its spelling, and locate the subcommand rather than fixing its index.
        assert any(
            "machine-local" in os.path.basename(str(a)).lower().replace("_", "-")
            for a in argv
        ), f"no machine-local CLI found in argv: {argv}"

        for i, arg in enumerate(argv):
            if arg in ("keys", "set"):
                sub = argv[i:]
                break
        else:
            raise AssertionError(f"unexpected machine-local invocation: {argv}")

        if sub[0] == "keys":
            return _FakeCompletedProcess(stdout="\n".join(sorted(self._keys)) + ("\n" if self._keys else ""))
        key, value = sub[1], sub[2]
        self._keys.add(key)
        self.set_calls.append((key, value))
        return _FakeCompletedProcess(stdout="")


@pytest.fixture(autouse=True)
def _pin_cpu_count(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)


def _expected_value() -> int:
    return 3 * 4


def test_key_absent_writes_3x_cores(monkeypatch):
    reg = _FakeRegistry(keys=[])
    monkeypatch.setattr(mod.subprocess, "run", reg.run)

    text, rc = mod.capture(check_only=False)

    assert rc == 0
    assert f"written ({_expected_value()})" in text
    assert reg.set_calls == [(mod._KEY, str(_expected_value()))]


def test_key_present_no_clobber(monkeypatch):
    reg = _FakeRegistry(keys=[mod._KEY])
    monkeypatch.setattr(mod.subprocess, "run", reg.run)

    text, rc = mod.capture(check_only=False)

    assert rc == 0
    assert "pre-existing" in text
    assert reg.set_calls == []


def test_check_only_absent_key_no_mutation(monkeypatch):
    reg = _FakeRegistry(keys=[])
    monkeypatch.setattr(mod.subprocess, "run", reg.run)

    text, rc = mod.capture(check_only=True)

    assert rc == 0
    assert f"would write ({_expected_value()})" in text
    assert reg.set_calls == []
    assert mod._KEY not in reg._keys


def test_check_only_present_key_reports_pre_existing(monkeypatch):
    reg = _FakeRegistry(keys=[mod._KEY])
    monkeypatch.setattr(mod.subprocess, "run", reg.run)

    text, rc = mod.capture(check_only=True)

    assert rc == 0
    assert "pre-existing" in text


def test_main_bad_flag_usage_error(monkeypatch, capsys):
    reg = _FakeRegistry(keys=[])
    monkeypatch.setattr(mod.subprocess, "run", reg.run)

    rc = mod.main(["--bogus"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "usage: capture-fan-out-threshold.sh" in captured.err


def test_main_no_args_writes(monkeypatch, capsys):
    reg = _FakeRegistry(keys=[])
    monkeypatch.setattr(mod.subprocess, "run", reg.run)

    rc = mod.main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert f"written ({_expected_value()})" in captured.out


def test_missing_machine_local_binary_degrades_to_absent(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("machine-local not found")

    monkeypatch.setattr(mod.subprocess, "run", _raise)

    # keys-probe degrades to empty -> treated as absent -> attempts a write,
    # which also raises FileNotFoundError inside capture(); the write call
    # itself is NOT guarded in the production code (only the keys-probe is),
    # matching the bash oracle's own asymmetry (only the `keys` read is
    # wrapped in `|| true`; the `machine-local set` write is unguarded and
    # was expected to fail loudly if machine-local truly isn't installed).
    with pytest.raises(FileNotFoundError):
        mod.capture(check_only=False)
