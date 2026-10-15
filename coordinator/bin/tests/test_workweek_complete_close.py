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


def test_default_mode_buckets_daily_files_by_own_week(tmp_path: Path) -> None:
    (
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        _,
        _,
    ) = _setup_week_only_fixture(tmp_path)
    (week_changelog_dir / "2026-08-17.md").write_text("a", encoding="utf-8")
    (week_changelog_dir / "2026-08-26.md").write_text("b", encoding="utf-8")
    touched: list[Path] = []

    _mod.perform_archive_files(
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        "2026-08-17",
        "v0.6.0",
        "deadbeef",
        "2026-08-31",
        touched_out=touched,
    )

    assert (archive_week_root / "2026-08-17" / "2026-08-17.md").exists()
    assert (archive_week_root / "2026-08-24" / "2026-08-26.md").exists()
    assert (week_changelog_dir / "HEADER.md").exists()
    assert archive_week_root / "2026-08-24" / "2026-08-26.md" in touched


def test_expand_to_files_yields_posix_files_not_directories(tmp_path: Path) -> None:
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "d" / "sub" / "b.md").write_text("b", encoding="utf-8")

    def rel_id(p: Path, root: Path) -> str:
        return p.relative_to(root).as_posix()

    got = _mod._expand_to_files([tmp_path / "d"], tmp_path, rel_id)

    assert got == ["d/a.md", "d/sub/b.md"]


def test_commit_archive_returns_1_and_prints_refusal_on_commit_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from types import SimpleNamespace

    _mod._ensure_claude_klabauter_root_on_path()
    from coordinator_core.git import git_state
    from coordinator_core.workstream_complete import directives_commit_tail

    seen: dict = {}

    def fake_close_commit(repo_root, **kw):
        seen.update(kw)
        return SimpleNamespace(commit_failed=True, diagnostics=["refused: scope"])

    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {})
    monkeypatch.setattr(directives_commit_tail, "run_close_commit", fake_close_commit)
    (tmp_path / "kept.md").write_text("k", encoding="utf-8")

    rc = _mod.commit_archive(tmp_path, ["kept.md", "kept.md", "gone.md"], "subj", "sid")

    err = capsys.readouterr().err
    assert rc == 1
    assert "archive: refused: scope" in err
    assert "commit refused" in err
    assert seen["stage_paths"] == ["kept.md"]
    assert seen["deleted_paths"] == []


def test_commit_archive_declares_tracked_absent_path_as_deletion(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    _mod._ensure_claude_klabauter_root_on_path()
    from coordinator_core.git import git_state
    from coordinator_core.workstream_complete import directives_commit_tail

    seen: dict = {}

    def fake_close_commit(repo_root, **kw):
        seen.update(kw)
        return SimpleNamespace(commit_failed=False, diagnostics=[])

    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {"gone.md": "blob"})
    monkeypatch.setattr(directives_commit_tail, "run_close_commit", fake_close_commit)
    (tmp_path / "kept.md").write_text("k", encoding="utf-8")

    assert _mod.commit_archive(tmp_path, ["gone.md", "kept.md"], "subj", "sid") == 0
    assert seen["stage_paths"] == ["gone.md", "kept.md"]
    assert seen["deleted_paths"] == ["gone.md"]


def _forbid_spawn(*_a, **_kw):
    raise AssertionError("push spawned a process")


def test_push_current_branch_detached_head_returns_1_without_spawning(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _mod._ensure_claude_klabauter_root_on_path()
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "head_branch", lambda root: "HEAD")
    monkeypatch.setattr(_mod.subprocess, "run", _forbid_spawn)

    assert _mod.push_current_branch(tmp_path) == 1
    assert "could not resolve current branch" in capsys.readouterr().err


def test_push_current_branch_nonzero_push_returns_rc_without_raising(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from types import SimpleNamespace

    _mod._ensure_claude_klabauter_root_on_path()
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "head_branch", lambda root: "main")
    monkeypatch.setattr(
        _mod.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=128)
    )

    assert _mod.push_current_branch(tmp_path) == 128
    err = capsys.readouterr().err
    assert "128" in err
    assert "commit landed" in err


def test_archive_without_session_id_exits_2_before_moving(tmp_path: Path, monkeypatch, capsys) -> None:
    (
        week_changelog_dir,
        _archive_week_root,
        _review_trail_dir,
        _review_trail_archive_root,
        _,
        _,
    ) = _setup_week_only_fixture(tmp_path)
    (week_changelog_dir / "2026-08-17.md").write_text("a", encoding="utf-8")
    monkeypatch.setattr(_mod, "_resolve_session_id_for_relocate", lambda: "")

    rc = _mod.main(
        ["archive", "--repo-root", str(tmp_path), "--version", "v1", "--merge-sha", "abc"]
    )

    assert rc == 2
    assert "COORDINATOR_SESSION_ID" in capsys.readouterr().err
    assert (week_changelog_dir / "2026-08-17.md").exists()


def test_buckets_follow_week_starting_weekday_chain(tmp_path: Path) -> None:
    """A Tuesday week_starting (DoE's chain) buckets earlier weeks on Tuesdays."""
    (
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        _,
        _,
    ) = _setup_week_only_fixture(tmp_path)
    (week_changelog_dir / "2026-09-14.md").write_text("a", encoding="utf-8")  # Mon, in the 09-08 Tue week
    (week_changelog_dir / "2026-09-23.md").write_text("b", encoding="utf-8")  # Wed, in the closing 09-22 week

    _mod.perform_archive_files(
        week_changelog_dir,
        archive_week_root,
        review_trail_dir,
        review_trail_archive_root,
        "2026-09-22",
        "v0.6.0",
        "deadbeef",
        "2026-09-28",
    )

    assert (archive_week_root / "2026-09-08" / "2026-09-14.md").exists()
    assert (archive_week_root / "2026-09-22" / "2026-09-23.md").exists()


def _annotated_header_repo(tmp_path: Path, header_value: str) -> Path:
    wc = tmp_path / "state" / "week-changelog"
    wc.mkdir(parents=True)
    (tmp_path / "state" / "review-trail").mkdir(parents=True)
    (wc / "HEADER.md").write_text(f"**Week starting:** {header_value}\n", encoding="utf-8")
    (wc / "2026-09-29-host.md").write_text("a", encoding="utf-8")
    return wc


def _archive_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "archive", "--repo-root", str(tmp_path), "--version", "v1", "--merge-sha", "abc",
        "--no-git", *extra,
    ]


def test_archive_default_sweep_annotated_header_without_week_starting(tmp_path: Path, monkeypatch) -> None:
    wc = _annotated_header_repo(
        tmp_path, "2026-09-28 (rolled forward by backfill 2026-09-27 from 2026-09-21)"
    )
    monkeypatch.setattr(_mod, "_resolve_session_id_for_relocate", lambda: "")

    rc = _mod.main(_archive_argv(tmp_path))

    assert rc == 0
    assert (tmp_path / "archive" / "week-changelogs" / "2026-09-28" / "2026-09-29-host.md").exists()
    assert not (wc / "2026-09-29-host.md").exists()


def test_archive_week_only_annotated_header_without_week_starting(tmp_path: Path, monkeypatch) -> None:
    wc = _annotated_header_repo(tmp_path, "2026-09-28 (ISO 2026-W40)")
    monkeypatch.setattr(_mod, "_resolve_session_id_for_relocate", lambda: "")

    rc = _mod.main(_archive_argv(tmp_path, "--week-only"))

    assert rc == 0
    assert (tmp_path / "archive" / "week-changelogs" / "2026-09-28" / "2026-09-29-host.md").exists()
    assert "(not yet set" in (wc / "HEADER.md").read_text(encoding="utf-8")


def test_archive_sentinel_header_without_week_starting_moves_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    wc = _annotated_header_repo(tmp_path, "(not yet set — run /workweek-start to initialise)")
    monkeypatch.setattr(_mod, "_resolve_session_id_for_relocate", lambda: "")

    rc = _mod.main(_archive_argv(tmp_path))

    assert rc == 2
    assert "--week-starting" in capsys.readouterr().err
    assert (wc / "2026-09-29-host.md").exists()
    assert not (tmp_path / "archive").exists()
