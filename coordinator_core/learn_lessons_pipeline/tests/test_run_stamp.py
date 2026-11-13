
from __future__ import annotations

import pytest

from coordinator_core.learn_lessons_pipeline.run_stamp import stamp_run_complete
from coordinator_core.ops.learn_lessons_cutoff import derive_cutoff


def test_fresh_stamp_creates_dir_and_file(tmp_path):
    runs_dir = tmp_path / "tasks"
    sentinel = stamp_run_complete(runs_dir, "2026-09-19")

    assert sentinel == runs_dir / "learn-lessons-2026-09-19" / "COMPLETE"
    assert sentinel.is_file()
    assert sentinel.read_bytes() == b""


def test_restamp_is_a_noop(tmp_path):
    runs_dir = tmp_path / "tasks"
    sentinel = stamp_run_complete(runs_dir, "2026-09-19")
    first_stat = sentinel.stat()

    second = stamp_run_complete(runs_dir, "2026-09-19")
    second_stat = second.stat()

    assert second == sentinel
    assert second_stat.st_mtime == first_stat.st_mtime
    assert second_stat.st_size == first_stat.st_size == 0


def test_malformed_date_raises(tmp_path):
    runs_dir = tmp_path / "tasks"
    with pytest.raises(ValueError):
        stamp_run_complete(runs_dir, "2026-13-45-bogus")
    with pytest.raises(ValueError):
        stamp_run_complete(runs_dir, "not-a-date")
    assert not runs_dir.exists()


def test_roundtrip_with_derive_cutoff(tmp_path):
    runs_dir = tmp_path / "tasks"
    stamp_run_complete(runs_dir, "2026-09-19")

    assert derive_cutoff(runs_dir) == "2026-09-19"


def _stage(runs_dir, repo, run_date="2026-11-13"):
    from coordinator_core import run_watermark
    from coordinator_core.learn_lessons_pipeline import (
        lessons_run_dir,
        proposed_watermark,
    )

    run_watermark.stage(lessons_run_dir(runs_dir, run_date), proposed_watermark(run_date))
    return repo / "state" / "run-watermarks" / "learn-lessons.json"


def test_promotion_happens_only_inside_the_stamp_dispatch(tmp_path):
    from coordinator_core.learn_lessons_pipeline import apply as llp_apply

    runs_dir, repo = tmp_path / "tasks", tmp_path / "repo"
    target = _stage(runs_dir, repo)
    assert not target.exists()

    out = llp_apply._dispatch_stamp_run_complete([str(runs_dir), "2026-11-13"], repo)
    assert target.is_file() and out["watermark"] == str(target)


def test_failed_stamp_promotes_nothing(tmp_path):
    from coordinator_core.learn_lessons_pipeline import apply as llp_apply

    runs_dir, repo = tmp_path / "tasks", tmp_path / "repo"
    target = _stage(runs_dir, repo)
    with pytest.raises(ValueError):
        llp_apply._dispatch_stamp_run_complete([str(runs_dir), "bogus"], repo)
    assert not target.exists()


def test_since_follows_the_committed_watermark_else_the_cutoff(tmp_path):
    from coordinator_core.learn_lessons_pipeline import apply as llp_apply
    from coordinator_core.learn_lessons_pipeline import build_directives, lessons_since

    runs_dir, repo = tmp_path / "tasks", tmp_path / "repo"
    assert lessons_since(repo, "2026-09-19") == "2026-09-19"
    _stage(runs_dir, repo)
    assert lessons_since(repo, "2026-09-19") == "2026-09-19"  # staged, not promoted
    llp_apply._dispatch_stamp_run_complete([str(runs_dir), "2026-11-13"], repo)
    since = lessons_since(repo, "2026-09-19")
    assert since == "2026-11-13"

    args = build_directives(
        repo, roots=[], cutoff="2026-09-19", run_date="2026-11-14", since=since
    )
    assert args[0]["args"][args[0]["args"].index("--since") + 1] == "2026-11-13"
    sweep = next(d for d in args if d["id"] == "d-age-sweep")
    assert sweep["args"][sweep["args"].index("--before") + 1] == "2026-09-19"
