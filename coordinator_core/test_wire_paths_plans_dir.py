from __future__ import annotations

from pathlib import Path

from coordinator_core.wire_paths import plans_dir


def test_plans_dir_resolves_docs_plans_under_root(tmp_path: Path) -> None:
    assert plans_dir(tmp_path) == tmp_path / "docs" / "plans"


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


def test_archived_plan_path_dated_and_undated(tmp_path: Path) -> None:
    from coordinator_core.wire_paths import archived_plan_path

    assert archived_plan_path(tmp_path, "docs/plans/2026-10-01-a.md") == (
        tmp_path / "archive" / "specs" / "2026-10" / "2026-10-01-a.md"
    )
    assert archived_plan_path(tmp_path, Path("docs/plans/undated.md")) is None


def test_resolve_plan_pointer_literal_wins_over_archive(tmp_path: Path) -> None:
    from coordinator_core.wire_paths import resolve_plan_pointer

    live = _touch(tmp_path / "docs/plans/2026-10-01-a.md")
    _touch(tmp_path / "archive/specs/2026-10/2026-10-01-a.md")
    assert resolve_plan_pointer(tmp_path, "docs/plans/2026-10-01-a.md") == live


def test_resolve_plan_pointer_archive_only_and_neither(tmp_path: Path) -> None:
    from coordinator_core.wire_paths import resolve_plan_pointer

    arch = _touch(tmp_path / "archive/specs/2026-10/2026-10-01-a.md")
    assert resolve_plan_pointer(tmp_path, "docs/plans/2026-10-01-a.md") == arch
    assert resolve_plan_pointer(tmp_path, "tasks/plans/2026-10-01-a.md") == arch
    assert resolve_plan_pointer(tmp_path, "docs/plans/2026-10-02-b.md") is None


def test_resolve_plan_pointer_absolute(tmp_path: Path) -> None:
    from coordinator_core.wire_paths import resolve_plan_pointer

    live = _touch(tmp_path / "docs/plans/2026-10-01-a.md")
    assert resolve_plan_pointer(tmp_path, str(live)) == live
    arch = _touch(tmp_path / "archive/specs/2026-10/2026-10-03-c.md")
    gone = tmp_path / "docs/plans/2026-10-03-c.md"
    assert resolve_plan_pointer(tmp_path, str(gone)) == arch


def test_resolve_plan_pointer_outside_plan_dirs_not_redirected(tmp_path: Path) -> None:
    from coordinator_core.wire_paths import resolve_plan_pointer

    _touch(tmp_path / "archive/specs/2026-10/2026-10-01-a.md")
    assert resolve_plan_pointer(tmp_path, "docs/other/2026-10-01-a.md") is None
    assert resolve_plan_pointer(tmp_path, "state/2026-10-01-a.md") is None


def test_is_archived_plan_path(tmp_path: Path) -> None:
    from coordinator_core.wire_paths import is_archived_plan_path

    assert is_archived_plan_path(tmp_path, tmp_path / "archive/specs/2026-10/x.md")
    assert not is_archived_plan_path(tmp_path, tmp_path / "docs/plans/x.md")


def test_workstream_complete_leg_a_calls_the_shared_emitter() -> None:
    from coordinator_core.workstream_complete import _plans_dir
    from coordinator_core.wire_paths import plans_dir as canonical

    assert _plans_dir is canonical


def test_plan_dir_prefixes_match_governing_plan_glob_dirs() -> None:
    from coordinator_core import wire_paths
    from coordinator_core.workstream_complete.directives_lessons_plan import _GOVERNING_PLAN_GLOB_DIRS

    assert wire_paths._PLAN_DIR_PREFIXES == tuple(f"{d}/" for d in _GOVERNING_PLAN_GLOB_DIRS)
