"""load_sizing resolves a sizing archived under archive/sizings/, and still refuses escapes."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing


def _write(path, text="size: S\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_hot_sizing_wins_over_archived(tmp_path):
    _write(tmp_path / "state" / "sizings" / "a.yaml", "size: hot\n")
    _write(tmp_path / "archive" / "sizings" / "2026-10" / "a.yaml", "size: old\n")
    assert load_sizing(tmp_path, "state/sizings/a.yaml") == {"size": "hot"}


def test_missing_hot_resolves_archived(tmp_path):
    _write(tmp_path / "archive" / "sizings" / "2026-10" / "a.yaml", "size: old\n")
    assert load_sizing(tmp_path, "state/sizings/a.yaml") == {"size": "old"}


def test_direct_archive_path_resolves(tmp_path):
    _write(tmp_path / "archive" / "sizings" / "2026-10" / "a.yaml", "size: old\n")
    assert load_sizing(tmp_path, "archive/sizings/2026-10/a.yaml") == {"size": "old"}


def test_absent_everywhere_refuses(tmp_path):
    (tmp_path / "archive" / "sizings").mkdir(parents=True)
    with pytest.raises(SizingFireRefused, match="not found on disk"):
        load_sizing(tmp_path, "state/sizings/a.yaml")


def test_ambiguous_archive_refuses(tmp_path):
    _write(tmp_path / "archive" / "sizings" / "2026-09" / "a.yaml")
    _write(tmp_path / "archive" / "sizings" / "2026-10" / "a.yaml")
    with pytest.raises(SizingFireRefused, match="not found on disk"):
        load_sizing(tmp_path, "state/sizings/a.yaml")


def test_path_outside_both_homes_refuses(tmp_path):
    _write(tmp_path / "docs" / "a.yaml")
    with pytest.raises(SizingFireRefused, match="escapes"):
        load_sizing(tmp_path, "docs/a.yaml")
    with pytest.raises(SizingFireRefused, match="escapes"):
        load_sizing(tmp_path, "state/sizings/../../docs/a.yaml")
