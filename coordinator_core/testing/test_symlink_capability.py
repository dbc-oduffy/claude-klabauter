
from __future__ import annotations

import os

import pytest

from coordinator_core.testing import symlink_capability
from pathlib import Path


def test_probe_reports_true_on_this_posix_host() -> None:
    assert symlink_capability.CAN_CREATE_SYMLINK is True


def test_probe_is_cached_at_import_not_reprobed() -> None:
    assert symlink_capability.CAN_CREATE_SYMLINK == symlink_capability._probe_symlink_capability()


def test_probe_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    assert symlink_capability._probe_symlink_capability() is True
    # The managed root may be created; nothing else may be left.
    assert [p.name for p in tmp_path.iterdir()] in ([], ["coordinator"])


def test_marker_is_a_skipif_mark_decorator() -> None:
    marker = symlink_capability.requires_symlink_capability
    assert isinstance(marker, pytest.MarkDecorator)
    assert marker.name == "skipif"


def test_marker_does_not_skip_on_this_capable_host() -> None:
    @symlink_capability.requires_symlink_capability
    def _inner() -> str:
        return "ran"

    assert symlink_capability.CAN_CREATE_SYMLINK is True


class TestModuleLevelApplication:
    pytestmark = [symlink_capability.requires_symlink_capability]

    def test_runs_under_module_level_pytestmark(self) -> None:
        assert symlink_capability.CAN_CREATE_SYMLINK is True


def test_reason_names_the_windows_privilege() -> None:
    marker = symlink_capability.requires_symlink_capability
    reason = marker.kwargs.get("reason", "")
    assert "Developer Mode" in reason or "SeCreateSymbolicLink" in reason
