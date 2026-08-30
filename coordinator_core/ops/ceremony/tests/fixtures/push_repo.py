"""
coordinator_core.ops.ceremony.tests.fixtures.push_repo

Shared `work/x` repo-with-remote construction for the push-retry test
suites. Hoisted out of `test_push.py` (Review: overengineering-reviewer --
`test_push_rule_violation_class.py` in the same directory carried a
near-identical `_git`/`_init_repo` pair; the seam for a shared fixture
already existed via `fixtures/` and was not used).

Always builds a real bare `origin` and pushes `-u` to it, so
`branch.<name>.remote`/`.merge` genuinely exist in every consumer -- the
former `with_upstream=False` branch (a deliberately-unusable
`origin-unused.git` URL) was a config axis with a single true consumer and
did not earn its keep (Review: overengineering-reviewer, speculative-
generality finding).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def init_push_repo(tmp_path: Path) -> Path:
    """A `work/x` repo with a real bare `origin` remote, pushed `-u`.

    Enough for `_remote_configured_locally`/`branch_gate` to pass, and for
    `_resolve_upstream_local` to read genuine `branch.<name>.remote`/
    `.merge` keys out of `.git/config` -- required by any test that reaches
    the fetch/rebase ladder. `push` itself is mocked in every push-retry
    test, so the real push here never talks to what the mock returns.
    """
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)
    return repo
