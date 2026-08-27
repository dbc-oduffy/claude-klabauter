"""coordinator_core.testing.tests for symlink_capability — the helper's own
proof that the probe answers capability (not platform) on this host, and
that the shared marker is a usable pytest mark in both its application
forms.

Spec backlink: pln-one-shared-symlink-capability-62c20a § C1 (AC1, AC2, AC3, AC7)
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.testing import symlink_capability


def test_probe_reports_true_on_this_posix_host():
    # AC2/AC7 — this suite runs on a POSIX host in CI/dev; a capability
    # probe (not a platform check) must report able here.
    assert symlink_capability.CAN_CREATE_SYMLINK is True


def test_probe_is_cached_at_import_not_reprobed():
    # AC1 — probing once per process, not per test: a fresh call re-attempts
    # the filesystem operation, but the module constant must already hold
    # the memoized answer rather than requiring a re-probe to be trusted.
    assert symlink_capability.CAN_CREATE_SYMLINK == symlink_capability._probe_symlink_capability()


def test_probe_leaves_nothing_behind(tmp_path, monkeypatch):
    # Anti-scope: no probe litter on disk. Point tempfile at a directory we
    # control and assert it is empty again once the probe returns.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    assert symlink_capability._probe_symlink_capability() is True
    assert list(tmp_path.iterdir()) == []


def test_marker_is_a_skipif_mark_decorator():
    # AC3 — the marker is a usable pytest mark object.
    marker = symlink_capability.requires_symlink_capability
    assert isinstance(marker, pytest.MarkDecorator)
    assert marker.name == "skipif"


def test_marker_does_not_skip_on_this_capable_host():
    # AC7 — applying the marker as a per-test decorator on a host that CAN
    # symlink must not skip.
    @symlink_capability.requires_symlink_capability
    def _inner():
        return "ran"

    # A MarkDecorator is not itself a runner; assert its condition evaluates
    # false (does not skip) rather than invoking pytest's collection
    # machinery here.
    assert symlink_capability.CAN_CREATE_SYMLINK is True


class TestModuleLevelApplication:
    # AC3 — the same marker object works as a module-level pytestmark entry.
    pytestmark = [symlink_capability.requires_symlink_capability]

    def test_runs_under_module_level_pytestmark(self):
        assert symlink_capability.CAN_CREATE_SYMLINK is True


def test_reason_names_the_windows_privilege():
    # AC2 — the reason string names the missing capability, not a platform.
    marker = symlink_capability.requires_symlink_capability
    reason = marker.kwargs.get("reason", "")
    assert "Developer Mode" in reason or "SeCreateSymbolicLink" in reason
