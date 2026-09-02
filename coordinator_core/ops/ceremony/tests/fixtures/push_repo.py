"""
coordinator_core.ops.ceremony.tests.fixtures.push_repo

Shared `work/x` repo-with-remote construction for the push-retry test
suites. Hoisted out of `test_push.py` (Review: overengineering-reviewer --
`test_push_rule_violation_class.py` in the same directory carried a
near-identical `_git`/`_init_repo` pair; the seam for a shared fixture
already existed via `fixtures/` and was not used).

Builds a real bare `origin`, and by default pushes `-u` to it so
`branch.<name>.remote`/`.merge` genuinely exist in the consumer.

`set_upstream=False` re-introduces the no-upstream axis a 2026-08-30
overengineering review removed for want of consumers. It has three now, all
in `test_push_no_upstream_publish.py`, and the axis it expresses is the one
production defect that suite pins: a day branch that exists on disk with no
`branch.<name>.remote`/`.merge` at all. It differs from the removed version
in kind -- the remote is real and reachable, so a publish genuinely lands,
where the old `origin-unused.git` URL could only ever fail.

`branch` selects the branch name, because whether a name satisfies
`daily_branch.is_canonical_branch` is precisely what the publish path gates
on, and `work/x` does not.
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


def init_push_repo(
    tmp_path: Path, *, branch: str = "work/x", set_upstream: bool = True
) -> Path:
    """A `work/x` repo with a real bare `origin` remote, pushed `-u`.

    Enough for `_default_remote_name_local`/`branch_gate` to pass, and for
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
    _git(["checkout", "-q", "-b", branch], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    if set_upstream:
        _git(["push", "-q", "-u", "origin", branch], repo)
    return repo
