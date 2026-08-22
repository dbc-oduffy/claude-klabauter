"""
bin.tests.test_claude_klabauter_doctor_eol_rider_host_suspension — producer/consumer contract
between the two eol doctor probes and the op that produces the sentinels they read.

Both `claude-klabauter.eol.census` and `claude-klabauter.eol.audit_producers` read a per-machine
sentinel written by a cadence rider that is a leg of `session.boot_sweep`. While
that op sits in `coordinator_core.op_budget_suspension.SUSPENDED_OPS`, no session
boot reaches either rider — so the sentinel-absent reading must name the
suspension rather than telling the operator to boot a session, which cannot
discharge it. Measured live on macOS (`machine-b`, 2026-08-22): every boot refused
with `-32006` and no sentinel or cadence marker existed anywhere in the tree, while
both probes reported "the cadence rider has not run yet on this machine".

Spec backlink: coordinator_core/op_budget_suspension.py § REINSTATEMENT
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

_SUSPENSION_RECORD = {"measured": {"max_ms": 30016.6, "p50_ms": 30010.8, "n": 8}}


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Registered in sys.modules before exec so dataclass annotation resolution
    (sys.modules[cls.__module__]) finds a valid namespace on Python 3.14+.
    """
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_eol_rider_host_suspension_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(key, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


@pytest.fixture
def empty_common_dir(tmp_path, monkeypatch):
    """Point the probes' `git_common_dir` at an empty directory — no sentinel."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    import coordinator_core.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "git_common_dir", lambda *_a, **_k: tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "runner_name, probe_op",
    [
        ("_run_probe_eol_census", "eol.census"),
        ("_run_probe_eol_audit_producers", "eol.audit_producers"),
    ],
)
def test_sentinel_absent_names_the_suspended_host_op(
    runner_name, probe_op, empty_common_dir, monkeypatch
):
    mod = _require_module()
    monkeypatch.setattr(
        mod, "_eol_rider_host_suspension", lambda *_a, **_k: _SUSPENSION_RECORD
    )

    result = getattr(mod, runner_name)(_REPO_ROOT)

    assert result.skipped is True
    assert mod._EOL_RIDER_HOST_OP in result.detail
    assert "has not run yet" not in result.detail
    assert "Boot a session" not in (result.remediation or "")
    assert probe_op in (result.remediation or "")


@pytest.mark.parametrize(
    "runner_name",
    ["_run_probe_eol_census", "_run_probe_eol_audit_producers"],
)
def test_sentinel_absent_keeps_the_never_ran_wording_while_the_host_op_is_live(
    runner_name, empty_common_dir, monkeypatch
):
    """The suspension arm must not swallow the genuine never-had-a-slot case."""
    mod = _require_module()
    monkeypatch.setattr(mod, "_eol_rider_host_suspension", lambda *_a, **_k: None)

    result = getattr(mod, runner_name)(_REPO_ROOT)

    assert result.skipped is True
    assert "has not run yet on this machine" in result.detail
    assert "Boot a session" in (result.remediation or "")


def test_host_op_lookup_matches_the_live_suspension_table():
    """The probes' host-op name must resolve against the real table, not drift
    into a string that always reads live."""
    mod = _require_module()
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from coordinator_core import op_budget_suspension

    assert mod._eol_rider_host_suspension(_REPO_ROOT) == op_budget_suspension.suspension_record(
        mod._EOL_RIDER_HOST_OP
    )
