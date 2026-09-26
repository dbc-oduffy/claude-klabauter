from __future__ import annotations

import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

from coordinator_core.ops.doc_staleness import (
    build_doc_staleness_report,
    compute_doc_staleness,
)
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BASE_DATE = date(2026, 1, 1)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    email = os.environ.get("GIT_AUTHOR_EMAIL", "test@example.com")
    name = os.environ.get("GIT_AUTHOR_NAME", "Test")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", email)
    _git(repo, "config", "user.name", name)
    return repo


def _iso(day_offset: int) -> str:
    d = _BASE_DATE + timedelta(days=day_offset)
    return f"{d.isoformat()}T12:00:00+0000"


def _dated_commit(
    repo: Path, files: dict[str, str], msg: str, day_offset: int
) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    date_str = _iso(day_offset)
    env = {**os.environ, "GIT_AUTHOR_DATE": date_str, "GIT_COMMITTER_DATE": date_str}
    subprocess.run(
        ["git", "commit", "-q", "-m", msg, f"--date={date_str}"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _noise_commit(repo: Path, i: int, day_offset: int) -> str:
    return _dated_commit(repo, {f"noise/n{i}.txt": f"noise {i}"}, f"noise {i}", day_offset)


def test_fires_on_and(tmp_path):
    repo = _init_repo(tmp_path)
    last_sha = _dated_commit(repo, {"README.md": "Hello world.\n"}, "author readme", 0)
    for i in range(6):
        _noise_commit(repo, i, day_offset=1 + i)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=5,
        threshold_days=3,
        today=_BASE_DATE + timedelta(days=10),
    )

    assert result["status"] == "ok"
    assert result["last_touch_sha"] == last_sha
    assert result["commits_since"] >= 5
    assert result["days_since"] >= 3
    assert result["stale"] is True
    assert result["threshold_commits"] == 5
    assert result["threshold_days"] == 3


def test_commits_only_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "Hello world.\n"}, "author readme", 0)
    for i in range(10):
        _noise_commit(repo, i, day_offset=0)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=5,
        threshold_days=3,
        today=_BASE_DATE,
    )

    assert result["commits_since"] >= 5
    assert result["days_since"] == 0
    assert result["stale"] is False


def test_days_only_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "Hello world.\n"}, "author readme", 0)
    _noise_commit(repo, 0, day_offset=1)
    _noise_commit(repo, 1, day_offset=2)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=5,
        threshold_days=3,
        today=_BASE_DATE + timedelta(days=100),
    )

    assert result["commits_since"] == 2
    assert result["days_since"] >= 3
    assert result["stale"] is False


def test_busy_week_recent_touch_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "Hello world.\n"}, "author readme", 0)
    for i in range(20):
        _noise_commit(repo, i, day_offset=1)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=5,
        threshold_days=3,
        today=_BASE_DATE + timedelta(days=1),
    )

    assert result["commits_since"] >= 5
    assert result["days_since"] <= 1
    assert result["stale"] is False


def test_sweep_drive_by_does_not_reset_clock(tmp_path):
    repo = _init_repo(tmp_path)
    authored_sha = _dated_commit(
        repo, {"README.md": "Hello world.\nAuthored content.\n"}, "author readme", 0
    )

    sweep_files = {f"pkg/f{i}.txt": f"content {i}" for i in range(39)}
    sweep_files["README.md"] = "Hello world.\nAuthored content.\nx\n"
    _dated_commit(repo, sweep_files, "mechanical sweep", day_offset=5)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        sweep_files_threshold=10,
        sweep_lines_threshold=10,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["status"] == "ok"
    assert result["last_touch_sha"] == authored_sha


def test_whitespace_only_does_not_reset_clock(tmp_path):
    repo = _init_repo(tmp_path)
    authored_sha = _dated_commit(
        repo, {"README.md": "Hello world.\nAuthored content.\n"}, "author readme", 0
    )
    _dated_commit(
        repo,
        {"README.md": "Hello world.\nAuthored content.\n   \n"},
        "trailing whitespace",
        day_offset=5,
    )

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == authored_sha


def test_link_only_does_not_reset_clock(tmp_path):
    repo = _init_repo(tmp_path)
    authored_sha = _dated_commit(
        repo,
        {"README.md": "See [docs](https://example.com/old) for details.\n"},
        "author readme",
        0,
    )
    _dated_commit(
        repo,
        {"README.md": "See [docs](https://example.com/new) for details.\n"},
        "repoint link",
        day_offset=5,
    )

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == authored_sha


def test_sweep_boundary_lines_equal_threshold_not_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "hello\n"}, "author readme", 0)

    files = {f"pkg/f{i}.txt": f"content {i}" for i in range(5)}
    files["README.md"] = "hello\n" + "\n".join(f"new{i}" for i in range(5)) + "\n"
    boundary_sha = _dated_commit(repo, files, "boundary at L", day_offset=5)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        sweep_files_threshold=3,
        sweep_lines_threshold=5,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == boundary_sha


def test_sweep_boundary_lines_one_below_threshold_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    authored_sha = _dated_commit(repo, {"README.md": "hello\n"}, "author readme", 0)

    files = {f"pkg/f{i}.txt": f"content {i}" for i in range(5)}
    files["README.md"] = "hello\n" + "\n".join(f"new{i}" for i in range(4)) + "\n"
    _dated_commit(repo, files, "boundary at L-1", day_offset=5)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        sweep_files_threshold=3,
        sweep_lines_threshold=5,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == authored_sha


def test_sweep_boundary_files_equal_threshold_not_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "hello\n"}, "author readme", 0)

    files = {f"pkg/f{i}.txt": f"content {i}" for i in range(4)}
    files["README.md"] = "hello\nx\n"
    boundary_sha = _dated_commit(repo, files, "boundary at K", day_offset=5)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        sweep_files_threshold=5,
        sweep_lines_threshold=100,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == boundary_sha


def test_sweep_boundary_files_one_above_threshold_excluded(tmp_path):
    repo = _init_repo(tmp_path)
    authored_sha = _dated_commit(repo, {"README.md": "hello\n"}, "author readme", 0)

    files = {f"pkg/f{i}.txt": f"content {i}" for i in range(5)}
    files["README.md"] = "hello\nx\n"
    _dated_commit(repo, files, "boundary at K+1", day_offset=5)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        sweep_files_threshold=5,
        sweep_lines_threshold=100,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == authored_sha


def test_large_commit_that_substantially_rewrites_doc_is_authored(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "old\n"}, "init readme", 0)

    big_commit_files = {f"pkg/f{i}.txt": f"content {i}" for i in range(50)}
    big_commit_files["README.md"] = "\n".join(f"line {i}" for i in range(50)) + "\n"
    rewrite_sha = _dated_commit(
        repo, big_commit_files, "large sweep that also rewrites README", day_offset=5
    )

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        today=_BASE_DATE + timedelta(days=6),
    )

    assert result["last_touch_sha"] == rewrite_sha


def test_empty_registry_clean(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "hi\n"}, "init", 0)

    report = build_doc_staleness_report(repo, [], threshold_commits=5, threshold_days=3)

    assert report == {"docs": []}


def test_absent_declared_path_clean(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "hi\n"}, "init", 0)

    report = build_doc_staleness_report(
        repo, ["NONEXISTENT.md"], threshold_commits=5, threshold_days=3
    )

    assert report == {"docs": [{"path": "NONEXISTENT.md", "status": "absent"}]}


def test_absent_path_single_call_clean(tmp_path):
    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "hi\n"}, "init", 0)

    result = compute_doc_staleness(
        repo, "NONEXISTENT.md", threshold_commits=5, threshold_days=3
    )

    assert result == {"path": "NONEXISTENT.md", "status": "absent"}


def test_threshold_override_honoured(tmp_path):
    from coordinator_core.ops.doc_registry import (
        DEFAULT_DOC_STALENESS_COMMITS,
        DEFAULT_DOC_STALENESS_DAYS,
    )

    repo = _init_repo(tmp_path)
    _dated_commit(repo, {"README.md": "hi\n"}, "init", 0)
    for i in range(4):
        _noise_commit(repo, i, day_offset=5)

    default_result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=DEFAULT_DOC_STALENESS_COMMITS,
        threshold_days=DEFAULT_DOC_STALENESS_DAYS,
        today=_BASE_DATE + timedelta(days=30),
    )
    assert default_result["stale"] is False

    override_result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=3,
        threshold_days=5,
        today=_BASE_DATE + timedelta(days=30),
    )
    assert override_result["threshold_commits"] == 3
    assert override_result["threshold_days"] == 5
    assert override_result["stale"] is True


def test_evidence_fields_present(tmp_path):
    repo = _init_repo(tmp_path)
    last_sha = _dated_commit(repo, {"README.md": "hi\n"}, "init", 0)
    _dated_commit(repo, {"src/a.py": "x"}, "touch src", day_offset=1)
    _dated_commit(repo, {"src/b.py": "x"}, "touch src again", day_offset=2)
    _dated_commit(repo, {"docs/c.md": "x"}, "touch docs", day_offset=3)

    result = compute_doc_staleness(
        repo,
        "README.md",
        threshold_commits=1,
        threshold_days=1,
        today=_BASE_DATE + timedelta(days=4),
    )

    assert result["last_touch_sha"] == last_sha
    assert "last_touch_date" in result
    assert result["commits_since"] == 3
    assert result["days_since"] == 4
    assert result["changed_areas"][0] == "src"
    assert "docs" in result["changed_areas"]
    assert result["threshold_commits"] == 1
    assert result["threshold_days"] == 1
