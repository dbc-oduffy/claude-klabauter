"""bin.tests.test_claude_klabauter_doctor_root_channels_reconciled_probe -- Behavioural
coverage for `claude-klabauter.root.channels_reconciled`
(`bin/claude-klabauter-doctor-probe.py::_run_probe_root_channels_reconciled`).

Slice-C code review (2026-08-22, sidecar
state/subagent-share/d2952754-76cb-4b4e-bacc-7ed39744b6d7/
2026-08-22-codereview-sliceC-doctor-probes.md, Finding 3) flagged this probe
as the one sibling in its slice with no direct behavioural test: only a
selector-registration check existed
(`coordinator_core/tests/test_claude_klabauter_doctor_probe_selectors.py`), never a
test that drives its own PASS / DEGRADED / exception branches. The reviewer
verified the probe's field usage (`report.root`, `report.channels`,
`channel.origin/value/exists`) matches
`coordinator_core/root_channel_reconcile.py` -- this file closes the missing
test, not a bug.

WHAT THIS COVERS
    - PASS: `disagreement_message` returns None (every channel agrees).
    - DEGRADED: `disagreement_message` returns a message (a real
      disagreement or an absent target).
    - Exception: `reconcile_all` raises -- probe degrades to INFO with
      `skipped=True`, never crashes or leaves the exception unhandled.

NEGATIVE SPEC
    - Does NOT exercise `coordinator_core.root_channel_reconcile` itself
      (channel resolution, registry/pointer reads) -- that module's own
      tests own that surface. This probe's `reconcile_all` /
      `disagreement_message` imports are monkeypatched at the module level
      so this file tests only the probe's own branch selection and result
      shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Mirrors `test_claude_klabauter_doctor_new_probes.py::_load_probe_module` -- see
    that file for why the module is pre-registered in `sys.modules`.
    """
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_root_channels_reconciled_unit"
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


def _fake_report(root: str = "claude_klabauter") -> SimpleNamespace:
    channel = SimpleNamespace(origin="registry repos.claude_klabauter", value="/some/path", exists=True)
    return SimpleNamespace(root=root, channels=(channel,))


class TestRootChannelsReconciledProbe:
    def test_all_channels_agree_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()
        fake_reconcile_module = SimpleNamespace(
            reconcile_all=lambda: (_fake_report(),),
            disagreement_message=lambda reports: None,
        )
        monkeypatch.setitem(sys.modules, "coordinator_core.root_channel_reconcile", fake_reconcile_module)

        result = mod._run_probe_root_channels_reconciled(None)

        assert result.probe == mod._ROOT_CHANNELS_PROBE
        assert result.status == mod._PASS
        assert "agrees" in result.detail
        assert "claude_klabauter" in result.data

    def test_disagreement_is_degraded_not_broken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A channel split is an operator data condition, never a hard FAIL --
        see the probe's own docstring: which path is real is not this
        probe's call to make."""
        mod = _require_module()
        message = "registry repos.claude_klabauter and pointer .claude-klabauter-root disagree."
        fake_reconcile_module = SimpleNamespace(
            reconcile_all=lambda: (_fake_report(),),
            disagreement_message=lambda reports: message,
        )
        monkeypatch.setitem(sys.modules, "coordinator_core.root_channel_reconcile", fake_reconcile_module)

        result = mod._run_probe_root_channels_reconciled(None)

        assert result.probe == mod._ROOT_CHANNELS_PROBE
        assert result.status == mod._DEGRADED
        assert result.detail == message
        assert result.required is False

    def test_reconcile_failure_degrades_to_skipped_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The oracle raising (e.g. an unreadable registry) must never crash
        the doctor run -- INFO + skipped=True, per every other probe's
        bootstrap-failure invariant."""
        mod = _require_module()

        def _raise():
            raise RuntimeError("machine-local registry unreadable")

        fake_reconcile_module = SimpleNamespace(
            reconcile_all=_raise,
            disagreement_message=lambda reports: None,
        )
        monkeypatch.setitem(sys.modules, "coordinator_core.root_channel_reconcile", fake_reconcile_module)

        result = mod._run_probe_root_channels_reconciled(None)

        assert result.probe == mod._ROOT_CHANNELS_PROBE
        assert result.status == mod._INFO
        assert result.skipped is True
        assert "RuntimeError" in result.detail
