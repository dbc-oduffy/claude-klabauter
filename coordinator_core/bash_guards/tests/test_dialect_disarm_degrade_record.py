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


def test_probe_armed_survives_non_utf8_probe_output(tmp_path):
    """A probe child emitting bytes the locale codec cannot decode must yield
    a (False, reason) verdict, not an escaping UnicodeDecodeError."""
    import sys

    from coordinator_core.bash_guards._dialect import probe_armed

    pkg = tmp_path / "coordinator_core"
    (pkg / "bash_guards").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bash_guards" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bash_guards" / "_dialect.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe\\x81 bad')\nsys.stdout.flush()\nsys.exit(1)\n",
        encoding="utf-8",
    )
    armed, reason = probe_armed(sys.executable, tmp_path)
    assert armed is False
    assert "bad" in reason


def _site_less_interpreter(tmp_path):
    """A POSIX wrapper running the current interpreter with `-S`, so the parser
    packages are importable only from an engine-site on the probe child's path."""
    import importlib.util
    import os
    import stat
    import sys

    if os.name == "nt":
        pytest.skip("POSIX wrapper interpreter")
    for name in ("tree_sitter", "tree_sitter_pwsh"):
        if importlib.util.find_spec(name) is None:
            pytest.skip(f"{name} not installed in this interpreter")
    wrapper = tmp_path / "py-nosite"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)


def _stamped_engine_site_with_parser(home):
    import importlib.util
    import shutil
    import sys

    site = home / "engine-site"
    site.mkdir(parents=True)
    for name in ("tree_sitter", "tree_sitter_pwsh"):
        spec = importlib.util.find_spec(name)
        shutil.copytree(spec.submodule_search_locations[0], site / name)
    (site / ".engine-site-stamp").write_text(
        f"{sys.version_info[0]}.{sys.version_info[1]}\n", encoding="utf-8"
    )


def test_probe_armed_sees_a_parser_present_only_in_engine_site(tmp_path, monkeypatch):
    from coordinator_core.bash_guards._dialect import probe_armed

    interpreter = _site_less_interpreter(tmp_path)
    home = tmp_path / "settings-home"
    _stamped_engine_site_with_parser(home)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[3]
    armed, detail = probe_armed(interpreter, repo_root)
    assert armed is True, detail


def test_probe_armed_without_engine_site_still_reports_missing_package(tmp_path, monkeypatch):
    from coordinator_core.bash_guards._dialect import probe_armed

    interpreter = _site_less_interpreter(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "empty-home"))
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[3]
    armed, detail = probe_armed(interpreter, repo_root)
    assert armed is False
    assert "missing-package" in detail
