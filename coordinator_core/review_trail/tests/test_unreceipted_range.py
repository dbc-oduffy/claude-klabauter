"""Tests for review_trail.unreceipted_range on tmp_path git repos."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from coordinator_core.review_trail import unreceipted_range
from coordinator_core.review_trail.unreceipted_range import unreceipted_ancestors
from coordinator_core.session.machinery_paths import share_roots

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SESSION = "11112222-3333-4444-5555-666677778888"
_OTHER = "99998888-7777-6666-5555-444433332222"
_ENV_BASE = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, **_ENV_BASE, **(env or {})},
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def _commit(
    repo: Path,
    path: str,
    *,
    session: Optional[str] = _SESSION,
    deliverable: Optional[str] = None,
    day: int = 1,
) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{path}:{day}:{len(list(repo.rglob('*')))}\n", encoding="utf-8")
    _git(repo, "add", "--", path)
    message = f"touch {path}\n"
    trailers = []
    if session:
        trailers.append(f"Session-Id: {session}")
    if deliverable:
        trailers.append(f"Deliverable-Id: {deliverable}")
    if trailers:
        message += "\n" + "\n".join(trailers) + "\n"
    date = f"2026-09-{day:02d}T10:00:00+00:00"
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        message,
        env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return _git(repo, "rev-parse", "HEAD")


def _stamp_receipt(repo: Path, session: str, *, stamped_at: str) -> None:
    share = Path(share_roots(str(repo))[0]) / session
    share.mkdir(parents=True, exist_ok=True)
    (share / "code-reviewer.abc.md").write_text(
        "---\n"
        "agent_type: 'code-reviewer'\n"
        "review_receipt:\n"
        f"  session_id: '{session}'\n"
        "  agent_id: 'a1'\n"
        "  agent_type: 'code-reviewer'\n"
        f"  stamped_at: '{stamped_at}'\n"
        "---\n\n## Findings\n\nOne real finding.\n",
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def _all_members(sha: str, session_id: str, deliverable_id: str) -> bool:
    return True


def _run(repo: Path, tips: Optional[List[str]] = None, *, cap: int = 50, member=_all_members):
    head = _git(repo, "rev-parse", "HEAD")
    return unreceipted_ancestors(
        str(repo), head, exclude_tips=tips or [], member=member, cap=cap
    )


def test_names_a_code_commit_with_no_receipt(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    code = _commit(repo, "src/a.py", day=2)
    result = _run(repo, [tip])
    assert result.error is None
    assert result.uncovered == (code,)
    assert result.truncated is False
    assert result.unattributed == 0


def test_not_named_once_its_session_holds_a_later_receipt(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    _commit(repo, "src/a.py", day=2)
    _stamp_receipt(repo, _SESSION, stamped_at="2026-09-05T00:00:00+00:00")
    result = _run(repo, [tip])
    assert result.error is None
    assert result.uncovered == ()


def test_receipt_stamped_before_the_commit_does_not_credit(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    code = _commit(repo, "src/a.py", day=10)
    _stamp_receipt(repo, _SESSION, stamped_at="2026-09-05T00:00:00+00:00")
    assert _run(repo, [tip]).uncovered == (code,)


def test_bookkeeping_only_commit_is_outside_the_population(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    _commit(repo, "state/handoffs/x.md", day=2)
    result = _run(repo, [tip])
    assert result.error is None
    assert result.uncovered == ()
    assert result.unattributed == 0


def test_trailerless_commit_is_unattributed_not_named(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    _commit(repo, "src/a.py", session=None, day=2)
    result = _run(repo, [tip])
    assert result.uncovered == ()
    assert result.unattributed == 1


def test_member_predicate_filters_the_chain(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    mine = _commit(repo, "src/a.py", session=_SESSION, day=2)
    _commit(repo, "src/b.py", session=_OTHER, day=3)
    result = _run(repo, [tip], member=lambda sha, s, d: s == _SESSION)
    assert result.uncovered == (mine,)


def test_commit_at_or_behind_an_exclude_tip_is_not_walked(repo: Path) -> None:
    _commit(repo, "src/old.py", day=1)
    tip = _commit(repo, "src/tip.py", day=2)
    after = _commit(repo, "src/new.py", day=3)
    result = _run(repo, [tip])
    assert result.uncovered == (after,)
    assert _run(repo, []).uncovered != result.uncovered


def test_newest_first_ordering(repo: Path) -> None:
    first = _commit(repo, "src/a.py", day=1)
    second = _commit(repo, "src/b.py", day=2)
    assert _run(repo).uncovered == (second, first)


def test_cap_plus_one_commits_reports_truncated(repo: Path) -> None:
    for day in range(1, 5):
        _commit(repo, f"src/f{day}.py", day=day)
    result = _run(repo, cap=3)
    assert result.truncated is True
    assert len(result.uncovered) == 3
    exact = _run(repo, cap=4)
    assert exact.truncated is False
    assert len(exact.uncovered) == 4


@pytest.mark.parametrize("bad", ["abc1234", "HEAD", ""])
def test_short_or_symbolic_run_base_is_an_error_not_an_empty_set(repo: Path, bad: str) -> None:
    _commit(repo, "src/a.py")
    result = unreceipted_ancestors(
        str(repo), bad, exclude_tips=[], member=_all_members, cap=10
    )
    assert result.error is not None
    assert result.uncovered == ()


def test_short_exclude_tip_is_an_error(repo: Path) -> None:
    head = _commit(repo, "src/a.py")
    result = unreceipted_ancestors(
        str(repo), head, exclude_tips=["abc1234"], member=_all_members, cap=10
    )
    assert result.error is not None


def test_unknown_full_sha_is_an_error(repo: Path) -> None:
    _commit(repo, "src/a.py")
    result = unreceipted_ancestors(
        str(repo), "f" * 40, exclude_tips=[], member=_all_members, cap=10
    )
    assert result.error is not None
    assert result.uncovered == ()


def test_exactly_one_subprocess_spawn_per_call(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    _commit(repo, "src/a.py", day=2)
    _stamp_receipt(repo, _SESSION, stamped_at="2026-09-05T00:00:00+00:00")
    head = _git(repo, "rev-parse", "HEAD")
    real_run = subprocess.run
    calls: List[list] = []

    def counting_run(*args, **kwargs):
        calls.append(list(args[0]))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    result = unreceipted_ancestors(
        str(repo), head, exclude_tips=[tip], member=_all_members, cap=10
    )
    assert result.error is None
    assert len(calls) == 1
    assert calls[0][0] == "git"
    assert "--max-count=11" in calls[0]


def test_paths_in_result_are_shas_only(repo: Path) -> None:
    tip = _commit(repo, "src/base.py", day=1)
    _commit(repo, "src/a.py", day=2)
    result = _run(repo, [tip])
    assert all(len(s) == 40 and "\\" not in s and ":" not in s for s in result.uncovered)
    assert unreceipted_range.RangeResult().error is None
