"""An engine ref move writes a reflog line git can read.

Machine-a 2026-11-06: 3,837 of 8,482 `.git/logs/HEAD` lines carried `Name <email>` with no
`<epoch> <tz>`, so `git reflog` skipped every engine-made commit and reflog recovery could
not see them.
"""

from __future__ import annotations

import subprocess

from coordinator_core.git import git_objects


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "base")
    return repo


def test_cas_ref_reflog_line_is_shown_by_git_reflog(tmp_path):
    repo = _repo(tmp_path)
    old = _git(repo, "rev-parse", "HEAD")
    new = _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit-tree", f"{old}^{{tree}}", "-p", old, "-m", "x")
    gitdir = repo / ".git"
    assert git_objects.cas_ref(
        gitdir, "refs/heads/main", old, new,
        reflog_committer="Engine <e@x>", reflog_message="checkpoint(wave 0): 1 row\n\nTrailer: y",
        head_gitdir=gitdir,
    )
    for ref in ("HEAD", "main"):
        shown = _git(repo, "reflog", "show", "--format=%H %gs", ref).splitlines()
        assert shown[0] == f"{new} checkpoint(wave 0): 1 row Trailer: y", (ref, shown)
    last = (gitdir / "logs" / "HEAD").read_text(encoding="utf-8").splitlines()[-1]
    assert git_objects._DATED_IDENT_RE.search(last.split("\t", 1)[0]), last


def test_an_already_dated_identity_is_kept_verbatim():
    ident = "Engine <e@x> 1794007142 +0000"
    assert git_objects._reflog_identity(ident) == ident
