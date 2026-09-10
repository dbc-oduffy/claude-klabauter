"""bin.tests.test_claude_klabauter_doctor_dialect_degrade_field — coverage for DEFECT TWO of
Y3 (state/debt-backlog/2026-09-01-the-dialect-degrade-row-names-one-guard-a2f800037e9e.yaml).

`_run_probe_dialect_guard_armed`'s `data["durable_degrade_record_exists"]` field
previously answered "does `degrade.jsonl` have ANY row at all" (`degrade_path(...)
.exists()`), which is true for a `KIND_COLD_RUN`/`KIND_HOOK_TIMEOUT`/unrelated
`KIND_COLD_FAILED` row from `warm/hook_http.py`'s own call sites -- a field named
for "did the dialect guard degrade" that reads True for a condition it does not
name. This module proves the field distinguishes an unrelated degrade row from
this guard's own.

Loads bin/claude-klabauter-doctor-probe.py as a module via importlib (same pattern as
test_claude_klabauter_doctor_new_probes.py) for fast, isolated, monkeypatched execution.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_dialect_degrade_field_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_KEY] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_KEY, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


def test_field_is_false_when_the_only_degrade_row_is_unrelated_to_the_dialect_guard(
    monkeypatch, tmp_path
):
    """A `cold_run` row (warm/hook_http.py's own, unrelated call site) exists in
    degrade.jsonl, but the dialect guard itself is ARMED under every checked
    interpreter -- `durable_degrade_record_exists` must read False, not True,
    because the guard never degraded. The pre-fix `.exists()` check reads True
    here, which is exactly defect two: a field conflating any degrade with
    this one."""
    mod = _require_module()

    from coordinator_core.warm import telemetry as _telemetry

    _telemetry.record_degrade(
        kind=_telemetry.KIND_COLD_RUN,
        cause="no reachable warm listener (unrelated to the dialect guard)",
        engine_root=tmp_path,
    )
    assert _telemetry.degrade_path(tmp_path).exists()

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)

    def _fake_probe_armed(interpreter, claude_klabauter_root, timeout=10.0):
        return True, f"ARMED under {interpreter}"

    from coordinator_core.bash_guards import _dialect as _dialect_mod

    monkeypatch.setattr(_dialect_mod, "probe_armed", _fake_probe_armed)

    result = mod._run_probe_dialect_guard_armed(tmp_path)

    assert result.status == mod._PASS
    assert result.data["durable_degrade_record_exists"] is False, (
        "an unrelated cold_run row must not make this field read True -- "
        "the field is named for THIS guard's degrade, not the shared sink's"
    )


def test_field_is_true_when_this_guard_actually_degraded(monkeypatch, tmp_path):
    """Sanity check on the other side: once THIS guard's own disarm event has
    written its attributable row, the field reads True."""
    mod = _require_module()

    from coordinator_core.bash_guards import _dialect as _dialect_mod

    monkeypatch.setattr(_dialect_mod, "_LOGGED_PARSER_UNAVAILABLE_GUARDS", set())
    monkeypatch.setattr(
        _dialect_mod, "_dialect_parser_unavailable_log_path", lambda: tmp_path / "log.txt"
    )

    from coordinator_core.warm import telemetry as _telemetry

    real_record_degrade = _telemetry.record_degrade

    def _spy(*, kind, cause, engine_root=None):
        return real_record_degrade(kind=kind, cause=cause, engine_root=tmp_path)

    monkeypatch.setattr(_telemetry, "record_degrade", _spy)

    _dialect_mod._log_dialect_parser_unavailable("check_some_guard", "missing package")

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)

    def _fake_probe_armed(interpreter, claude_klabauter_root, timeout=10.0):
        return False, "SILENT under interpreter -- cause: missing-package -- tree-sitter-pwsh not importable"

    monkeypatch.setattr(_dialect_mod, "probe_armed", _fake_probe_armed)

    result = mod._run_probe_dialect_guard_armed(tmp_path)

    assert result.status == mod._DEGRADED
    assert result.data["durable_degrade_record_exists"] is True
