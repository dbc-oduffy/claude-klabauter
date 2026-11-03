"""Sync legs for the G-EM home: pull on entry, commit-pull-push at handoff and quick-wrap.

Both legs return `{sync, paths, remote_opt_out}`. `sync` is `ok`, `noop` (no remote; the opt-out
says why), `conflict` (rebase aborted, `paths` names the unmerged files) or `error` (git refused
a step; `detail` carries its stderr). A conflict is never resolved here.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git.run import run_git
from coordinator_core.group_em import gem_home


def _report(sync: str, paths=(), opt_out=None, detail: str = "") -> dict:
    rep = {"sync": sync, "paths": list(paths), "remote_opt_out": opt_out}
    if detail:
        rep["detail"] = detail
    return rep


def _has_origin(cwd: str) -> bool:
    return run_git(["remote", "get-url", "origin"], cwd=cwd).returncode == 0


def _branch(cwd: str) -> str:
    res = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    name = res.stdout.strip()
    if res.returncode != 0 or not name or name == "HEAD":
        return gem_home.DEFAULT_REMOTE_BRANCH
    return name


def _remote_has_branch(cwd: str, branch: str) -> bool:
    res = run_git(["ls-remote", "--exit-code", "--heads", "origin", branch], cwd=cwd, remote=True)
    return res.returncode == 0


def _no_remote_opt_out() -> str:
    _url, opt_out = gem_home.remote_status()
    return opt_out or "declined"


def _rebase(cwd: str, branch: str) -> dict | None:
    """`pull --rebase --autostash` from origin; None on success, else a conflict or error report.
    A conflict aborts the rebase and leaves the tree as it was."""
    res = run_git(["pull", "--rebase", "--autostash", "origin", branch], cwd=cwd, remote=True)
    if res.returncode == 0:
        return None
    unmerged = run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd).stdout.split()
    in_rebase = any(
        (Path(cwd) / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply")
    )
    if unmerged or in_rebase:
        run_git(["rebase", "--abort"], cwd=cwd)
        return _report("conflict", unmerged)
    return _report("error", detail=(res.stderr or res.stdout).strip())


def pull_on_entry(home) -> dict:
    """Pull the home's remote branch with `--rebase --autostash`; uncommitted edits survive.
    No remote, or a remote with no such branch yet: `noop`. Never raises on git failure."""
    cwd = str(home)
    if not _has_origin(cwd):
        return _report("noop", opt_out=_no_remote_opt_out())
    branch = _branch(cwd)
    if not _remote_has_branch(cwd, branch):
        return _report("noop")
    return _rebase(cwd, branch) or _report("ok")


def push_leg(home, message: str) -> dict:
    """Stage everything the home's ignore list lets through, commit when something is staged,
    `pull --rebase --autostash`, then push. No remote: `noop` and nothing is committed or pushed."""
    cwd = str(home)
    if not _has_origin(cwd):
        return _report("noop", opt_out=_no_remote_opt_out())
    branch = _branch(cwd)
    add = run_git(["add", "-A"], cwd=cwd)
    if add.returncode != 0:
        return _report("error", detail=(add.stderr or add.stdout).strip())
    if run_git(["diff", "--cached", "--quiet"], cwd=cwd).returncode != 0:
        commit = run_git(["commit", "-q", "-m", message], cwd=cwd)
        if commit.returncode != 0:
            return _report("error", detail=(commit.stderr or commit.stdout).strip())
    if _remote_has_branch(cwd, branch):
        failed = _rebase(cwd, branch)
        if failed:
            return failed
    push = run_git(["push", "-q", "-u", "origin", branch], cwd=cwd, remote=True)
    if push.returncode != 0:
        return _report("error", detail=(push.stderr or push.stdout).strip())
    return _report("ok")
