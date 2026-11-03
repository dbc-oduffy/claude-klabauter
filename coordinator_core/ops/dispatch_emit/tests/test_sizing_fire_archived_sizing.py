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


def test_flat_cite_loads_a_sharded_sizing_and_flat_still_loads(tmp_path):
    _write(tmp_path / "state" / "sizings" / "2026-11" / "2026-11-03-a.yaml", "size: shard\n")
    assert load_sizing(tmp_path, "state/sizings/2026-11-03-a.yaml") == {"size": "shard"}
    _write(tmp_path / "state" / "sizings" / "2026-11-03-b.yaml", "size: flat\n")
    assert load_sizing(tmp_path, "state/sizings/2026-11-03-b.yaml") == {"size": "flat"}


def test_s_plan_path_names_the_shard_and_located_keeps_a_flat_plan(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_fire import PLAN_PATH_JS, s_plan_located, s_plan_path

    sz = "state/sizings/2026-11-03-foo.yaml"
    assert s_plan_path(sz) == "docs/plans/2026-11/2026-11-03-foo.md"
    assert s_plan_located(tmp_path, sz) == "docs/plans/2026-11/2026-11-03-foo.md"
    _write(tmp_path / "docs" / "plans" / "2026-11-03-foo.md", "x\n")
    assert s_plan_located(tmp_path, sz) == "docs/plans/2026-11-03-foo.md"
    assert "m[1] + '/'" in PLAN_PATH_JS


def test_path_outside_both_homes_refuses(tmp_path):
    _write(tmp_path / "docs" / "a.yaml")
    with pytest.raises(SizingFireRefused, match="escapes"):
        load_sizing(tmp_path, "docs/a.yaml")
    with pytest.raises(SizingFireRefused, match="escapes"):
        load_sizing(tmp_path, "state/sizings/../../docs/a.yaml")
