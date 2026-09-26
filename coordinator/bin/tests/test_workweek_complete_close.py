# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workweek_complete_close",
        _BIN_DIR / "workweek-complete-close.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _setup_week_only_fixture(tmp_path: Path):
    week_changelog_dir = tmp_path / "state" / "week-changelog"
    archive_week_root = tmp_path / "archive" / "week-changelogs"
    review_trail_dir = tmp_path / "state" / "review-trail"
    review_trail_archive_root = tmp_path / "archive" / "review-trail"
    week_changelog_dir.mkdir(parents=True)
    review_trail_dir.mkdir(parents=True)

    week_starting = date(2026, 8, 17)
    (week_changelog_dir / "HEADER.md").write_text(
        "**Week starting:** 2026-08-17\n", encoding="utf-8"
    )
    shard_name = ".weekly-reviewer-scopes-abc123.json"
    (review_trail_dir / shard_name).write_text("{}", encoding="utf-8")

    return (
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        week_starting,
        shard_name,
    )


def test_week_only_leaves_shards_by_default(tmp_path: Path) -> None:
    (
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        week_starting,
        shard_name,
    ) = _setup_week_only_fixture(tmp_path)

    _mod.perform_archive_files(
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        week_starting.isoformat(),
        "v0.6.0",
        "deadbeef",
        "2026-08-24",
        week_only=True,
    )

    assert (review_trail_dir / shard_name).exists()


def test_week_only_clean_shards_deletes_shards(tmp_path: Path) -> None:
    (
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        week_starting,
        shard_name,
    ) = _setup_week_only_fixture(tmp_path)

    _mod.perform_archive_files(
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        week_starting.isoformat(),
        "v0.6.0",
        "deadbeef",
        "2026-08-24",
        week_only=True,
        clean_shards=True,
    )

    assert not (review_trail_dir / shard_name).exists()


def test_has_pathspec_content_empty_dir_is_false(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert _mod._has_pathspec_content(empty_dir) is False


def test_has_pathspec_content_dir_with_file_is_true(tmp_path: Path) -> None:
    populated_dir = tmp_path / "populated"
    populated_dir.mkdir()
    (populated_dir / "record.json").write_text("{}", encoding="utf-8")
    assert _mod._has_pathspec_content(populated_dir) is True


def test_has_pathspec_content_missing_path_is_false(tmp_path: Path) -> None:
    assert _mod._has_pathspec_content(tmp_path / "does-not-exist") is False
