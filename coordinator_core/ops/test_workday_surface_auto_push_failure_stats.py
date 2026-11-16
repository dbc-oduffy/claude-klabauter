
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import workday_surface_auto_push_failure_stats as mod
from pathlib import Path


def _stamped_line(when: datetime, suffix: str = "PUSH FAILED on work/x (ssh/ref-lock after 3) :: err :: stderr=<empty>") -> str:
    return f"[{when.strftime('%Y-%m-%dT%H:%M:%SZ')}] {suffix}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _write_log(repo, lines) -> None:
    (repo / ".git" / "push-failures.log").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def test_absent_log_returns_zeros_not_error(repo) -> None:
    result = mod.surface_auto_push_failure_stats(str(repo))
    assert result == {"total": 0, "recent_24h": 0, "last_line": None}


def test_gitfile_worktree_layout_returns_zeros(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    result = mod.surface_auto_push_failure_stats(str(root))
    assert result == {"total": 0, "recent_24h": 0, "last_line": None}


def test_window_and_totals(repo) -> None:
    now = datetime.now(timezone.utc)
    old = _stamped_line(now - timedelta(hours=48))
    recent_a = _stamped_line(now - timedelta(hours=1))
    recent_b = _stamped_line(now - timedelta(minutes=5))
    _write_log(repo, [old, recent_a, recent_b])

    result = mod.surface_auto_push_failure_stats(str(repo))
    assert result["total"] == 3
    assert result["recent_24h"] == 2
    assert result["last_line"] == recent_b


def test_malformed_line_counts_toward_total_excluded_from_recent_never_raises(repo) -> None:
    now = datetime.now(timezone.utc)
    garbage = "hook wrote something without a stamp"
    invalid_calendar = "[2026-13-99T99:99:99Z] PUSH FAILED on work/x"
    recent = _stamped_line(now - timedelta(minutes=30))
    _write_log(repo, [garbage, invalid_calendar, recent])

    result = mod.surface_auto_push_failure_stats(str(repo))
    assert result["total"] == 3
    assert result["recent_24h"] == 1
    assert result["last_line"] == recent


def test_all_malformed_log_is_still_never_an_error(repo) -> None:
    _write_log(repo, ["junk one", "junk two"])
    result = mod.surface_auto_push_failure_stats(str(repo))
    assert result == {"total": 2, "recent_24h": 0, "last_line": "junk two"}


def test_empty_log_file(repo) -> None:
    _write_log(repo, [])
    result = mod.surface_auto_push_failure_stats(str(repo))
    assert result == {"total": 0, "recent_24h": 0, "last_line": None}


def test_double_invocation_identical_results_no_state(repo) -> None:
    now = datetime.now(timezone.utc)
    _write_log(repo, [_stamped_line(now - timedelta(hours=2)), "malformed"])
    log = repo / ".git" / "push-failures.log"
    before = log.read_bytes()

    first = mod.surface_auto_push_failure_stats(str(repo))
    second = mod.surface_auto_push_failure_stats(str(repo))
    assert first == second
    assert log.read_bytes() == before


def test_missing_repo_root_raises_structured_error(tmp_path: Path) -> None:
    with pytest.raises(mod.PushFailureStatsError, match="repo_root"):
        mod.surface_auto_push_failure_stats(str(tmp_path / "no-such-repo"))


def test_op_registered_and_handler_requires_param(repo) -> None:
    handler = get_op_handler("workday.surface_auto_push_failure_stats")
    assert handler is not None
    with pytest.raises(mod.PushFailureStatsError, match="repo_root"):
        handler({}, None)
    result = handler({"repo_root": str(repo)}, None)
    assert result == {"total": 0, "recent_24h": 0, "last_line": None}
