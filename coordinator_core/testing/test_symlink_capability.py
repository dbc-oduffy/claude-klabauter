
from __future__ import annotations

import os

import pytest

from coordinator_core.testing import symlink_capability


def test_probe_reports_true_on_this_posix_host():
    assert symlink_capability.CAN_CREATE_SYMLINK is True


def test_probe_is_cached_at_import_not_reprobed():
    assert symlink_capability.CAN_CREATE_SYMLINK == symlink_capability._probe_symlink_capability()


def test_probe_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    assert symlink_capability._probe_symlink_capability() is True
    assert list(tmp_path.iterdir()) == []


def test_marker_is_a_skipif_mark_decorator():
    marker = symlink_capability.requires_symlink_capability
    assert isinstance(marker, pytest.MarkDecorator)
    assert marker.name == "skipif"


def test_marker_does_not_skip_on_this_capable_host():
    @symlink_capability.requires_symlink_capability
    def _inner():
        return "ran"

    assert symlink_capability.CAN_CREATE_SYMLINK is True


class TestModuleLevelApplication:
    pytestmark = [symlink_capability.requires_symlink_capability]

    def test_runs_under_module_level_pytestmark(self):
        assert symlink_capability.CAN_CREATE_SYMLINK is True


def test_reason_names_the_windows_privilege():
    marker = symlink_capability.requires_symlink_capability
    reason = marker.kwargs.get("reason", "")
    assert "Developer Mode" in reason or "SeCreateSymbolicLink" in reason
