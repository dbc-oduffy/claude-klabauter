"""Coverage for `_dialect._log_dialect_parser_unavailable`'s DR-402 durable
degrade row (C12 of docs/plans/2026-09-01-the-dogfooded-install-stops-lying-
about.md) -- closes the gap the code-reviewer named (finding 1, sidecar
coordinatorcode-reviewer.a840704a6560b71a0): the record_degrade call had no
test proving it fires, proving its `kind`/`cause` shape, or proving it
survives the sibling settings-home log write failing.

`_LOGGED_PARSER_UNAVAILABLE_GUARDS` is a module-global once-per-GUARD gate
(Y3 fix -- was once-per-process), so every test here resets it via
monkeypatch to keep cases independent.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _dialect
from coordinator_core.warm import telemetry


def _reset_once_per_process_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_dialect, "_LOGGED_PARSER_UNAVAILABLE_GUARDS", set())


def test_disarm_writes_a_kind_cold_failed_row_naming_the_guard(monkeypatch, tmp_path):
    _reset_once_per_process_gate(monkeypatch)
    monkeypatch.setattr(_dialect, "_dialect_parser_unavailable_log_path", lambda: tmp_path / "log.txt")

    calls = []
    real_record_degrade = telemetry.record_degrade

    def _spy(*, kind, cause, engine_root=None):
        calls.append({"kind": kind, "cause": cause})
        return real_record_degrade(kind=kind, cause=cause, engine_root=tmp_path)

    monkeypatch.setattr(telemetry, "record_degrade", _spy)

    _dialect._log_dialect_parser_unavailable("check_some_guard", "missing package")

    assert len(calls) == 1
    assert calls[0]["kind"] == telemetry.KIND_COLD_FAILED
    assert "check_some_guard" in calls[0]["cause"]
    assert "missing package" in calls[0]["cause"]

    rows = telemetry.degrade_samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == telemetry.KIND_COLD_FAILED


def test_settings_home_log_write_failure_does_not_suppress_record_degrade(monkeypatch, tmp_path):
    _reset_once_per_process_gate(monkeypatch)

    def _boom():
        raise OSError("settings-home unavailable")

    monkeypatch.setattr(_dialect, "_dialect_parser_unavailable_log_path", _boom)

    calls = []
    real_record_degrade = telemetry.record_degrade

    def _spy(*, kind, cause, engine_root=None):
        calls.append({"kind": kind, "cause": cause})
        return real_record_degrade(kind=kind, cause=cause, engine_root=tmp_path)

    monkeypatch.setattr(telemetry, "record_degrade", _spy)

    _dialect._log_dialect_parser_unavailable("check_other_guard", "parse error")

    assert len(calls) == 1, "settings-home log failing must not skip the durable degrade row"
    assert calls[0]["kind"] == telemetry.KIND_COLD_FAILED


def test_a_second_DIFFERENT_guard_in_the_same_process_still_gets_attributed(monkeypatch, tmp_path):
    """DEFECT ONE fix (Y3): the gate is per-guard-identity, not per-process.
    Two distinct guards degrading in one process must produce TWO durable
    rows, each naming its own guard -- not one row that reads as the whole
    account of what degraded (reviewer finding 4)."""
    _reset_once_per_process_gate(monkeypatch)
    monkeypatch.setattr(_dialect, "_dialect_parser_unavailable_log_path", lambda: tmp_path / "log.txt")

    calls = []
    real_record_degrade = telemetry.record_degrade

    def _spy(*, kind, cause, engine_root=None):
        calls.append({"kind": kind, "cause": cause})
        return real_record_degrade(kind=kind, cause=cause, engine_root=tmp_path)

    monkeypatch.setattr(telemetry, "record_degrade", _spy)

    _dialect._log_dialect_parser_unavailable("check_first_guard", "reason a")
    _dialect._log_dialect_parser_unavailable("check_second_guard", "reason b")

    assert len(calls) == 2, (
        "a durable record whose job is attribution must not under-report: "
        "one-guard-degraded and first-of-several-degraded must not render "
        "identically"
    )
    assert "check_first_guard" in calls[0]["cause"] and "reason a" in calls[0]["cause"]
    assert "check_second_guard" in calls[1]["cause"] and "reason b" in calls[1]["cause"]

    rows = telemetry.degrade_samples(tmp_path)
    assert len(rows) == 2


def test_the_SAME_guard_called_twice_in_one_process_is_still_deduped(monkeypatch, tmp_path):
    """The cost caveat: dedup by guard identity, not by process, so a guard
    that keeps failing on every command in a long-lived warm process does
    not write a row per call -- only once per guard."""
    _reset_once_per_process_gate(monkeypatch)
    monkeypatch.setattr(_dialect, "_dialect_parser_unavailable_log_path", lambda: tmp_path / "log.txt")

    calls = []
    real_record_degrade = telemetry.record_degrade

    def _spy(*, kind, cause, engine_root=None):
        calls.append({"kind": kind, "cause": cause})
        return real_record_degrade(kind=kind, cause=cause, engine_root=tmp_path)

    monkeypatch.setattr(telemetry, "record_degrade", _spy)

    _dialect._log_dialect_parser_unavailable("check_repeat_guard", "reason a")
    _dialect._log_dialect_parser_unavailable("check_repeat_guard", "reason b")

    assert len(calls) == 1, "same guard, same process: still once, not once-per-call"
