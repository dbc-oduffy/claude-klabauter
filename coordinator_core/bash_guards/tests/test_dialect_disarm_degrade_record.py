"""Coverage for `_dialect._log_dialect_parser_unavailable`'s DR-402 durable
degrade row (C12 of docs/plans/2026-09-01-the-dogfooded-install-stops-lying-
about.md) -- closes the gap the code-reviewer named (finding 1, sidecar
coordinatorcode-reviewer.a840704a6560b71a0): the record_degrade call had no
test proving it fires, proving its `kind`/`cause` shape, or proving it
survives the sibling settings-home log write failing.

`_LOGGED_PARSER_UNAVAILABLE` is a module-global once-per-process gate, so
every test here resets it via monkeypatch to keep cases independent.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _dialect
from coordinator_core.warm import telemetry


def _reset_once_per_process_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_dialect, "_LOGGED_PARSER_UNAVAILABLE", False)


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


def test_once_per_process_gate_still_suppresses_a_second_call(monkeypatch, tmp_path):
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

    assert len(calls) == 1, (
        "documents current behaviour named in reviewer finding 4: the "
        "per-process gate also caps the durable row to the FIRST guard's "
        "cause -- not asserting this is correct, only that it is what ships"
    )
