"""The follow-on's ignore answer hands git work proportional to its distinct directories, not to the
claimed-ignored file count, and never reports a tracked path ignored."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit import terminal_commit

BULK = "bulk_ignored"
LANDABLE = ["state/debt-backlog/d1.yaml", "state/debt-backlog/d2.yaml"]


def _git(repo: Path, *args: str) -> None:
    result = run_git(list(args), cwd=str(repo))
    assert result.returncode == 0, result.stderr


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, ".gitignore", f"{BULK}/\n*.log\n!keep/neg.log\n")
    _write(root, "README.md", "seed\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def spy(monkeypatch):
    calls: list = []
    real = terminal_commit.run_git

    def spying(args, *a, **k):
        calls.append((list(args), k.get("input")))
        return real(args, *a, **k)

    monkeypatch.setattr(terminal_commit, "run_git", spying)
    return calls


def _stdin_lines(calls: list) -> int:
    return len([s for s in (calls[0][1] or b"").split(b"\0") if s])


def _bulk(n: int) -> list:
    return [f"{BULK}/f{i:05d}.md" for i in range(n)]


def test_bulk_ignored_directory_answers_with_one_spawn_and_one_line(repo, spy):
    left = [*_bulk(3500), *LANDABLE, "keep/a.log", "keep/b.txt"]
    for rel in ("keep/a.log", "keep/b.txt", *LANDABLE):
        _write(repo, rel)

    ignored = terminal_commit._ignored_among_left(repo, left, None)

    assert sorted(ignored) == sorted([*_bulk(3500), "keep/a.log"])
    assert len(spy) == 1
    assert _stdin_lines(spy) == 1 + len(LANDABLE) + 2


def test_stdin_work_is_equal_at_350_and_3500_files(repo, spy):
    terminal_commit._ignored_among_left(repo, _bulk(350), None)
    small = _stdin_lines(spy)
    spy.clear()
    terminal_commit._ignored_among_left(repo, _bulk(3500), None)

    assert _stdin_lines(spy) == small == 1


def test_negation_reinclude_under_unignored_parent_answers_as_git(repo, spy):
    for rel in ("keep/neg.log", "keep/x.log"):
        _write(repo, rel)

    assert terminal_commit._ignored_among_left(repo, ["keep/neg.log", "keep/x.log"], None) == ["keep/x.log"]


def test_tracked_file_matching_a_pattern_is_not_ignored(repo):
    _write(repo, "keep/t.log")
    _git(repo, "add", "-f", "keep/t.log")

    assert terminal_commit._ignored_among_left(repo, ["keep/t.log"], None) == []


def test_force_added_file_under_ignored_directory_is_never_reported(repo):
    _write(repo, f"{BULK}/forced.txt")
    _git(repo, "add", "-f", f"{BULK}/forced.txt")

    small = terminal_commit._ignored_among_left(repo, [f"{BULK}/forced.txt", *_bulk(3)], None)
    assert small is not None and f"{BULK}/forced.txt" not in small

    big = terminal_commit._ignored_among_left(repo, [f"{BULK}/forced.txt", *_bulk(40)], None)
    assert big is None


def test_collapse_threshold_is_exclusive_at_the_minimum(repo, spy):
    terminal_commit._ignored_among_left(repo, _bulk(terminal_commit._COLLAPSE_MIN), None)
    at_min = _stdin_lines(spy)
    spy.clear()
    terminal_commit._ignored_among_left(repo, _bulk(terminal_commit._COLLAPSE_MIN + 1), None)

    assert at_min == terminal_commit._COLLAPSE_MIN
    assert _stdin_lines(spy) == 1


def test_collapsed_directory_git_does_not_report_refuses_to_answer(repo):
    left = [f"keep/f{i}.txt" for i in range(20)]

    assert terminal_commit._ignored_among_left(repo, left, None) is None


def test_unreported_collapsed_directory_with_ignored_and_force_added_member_makes_one_spawn_and_none(repo, spy):
    _write(repo, f"{BULK}/forced.txt")
    _git(repo, "add", "-f", f"{BULK}/forced.txt")
    left = [f"{BULK}/forced.txt", *_bulk(20)]

    assert terminal_commit._ignored_among_left(repo, left, None) is None
    assert len(spy) == 1
    assert _stdin_lines(spy) == 1


def test_spawn_failure_still_returns_none(repo, monkeypatch):
    class Failed:
        timed_out = True
        returncode = 0
        stdout_bytes = b""

    monkeypatch.setattr(terminal_commit, "run_git", lambda *a, **k: Failed())

    assert terminal_commit._ignored_among_left(repo, _bulk(40), None) is None
