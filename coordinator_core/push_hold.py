"""coordinator_core.push_hold -- a PM freeze on pushing, stored in the
target repo's own git config.

Repo hold:   `[coordinator] pushHold = <reason>`
Branch hold: `[branch "<name>"] coordinatorPushHold = <reason>`

Reads parse `<common-dir>/config` in-process (zero spawns), so
`push_outstanding` can consult them on every sweep. Writes (`set`/`clear`)
run `git config`, CLI-only, never on a sweep path. A key with an empty value
is treated as absent; a hold's note is never empty (`set` defaults it).

A hold may name its own release: `coordinator.pushHoldAllow` /
`branch.<name>.coordinatorPushHoldAllow` hold the full SHA whose explicit-refspec
push the manual-push guard admits. `push_outstanding` ignores it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from coordinator_core.git.git_dir import resolve_git_common_dir

__all__ = ["read_hold", "read_hold_allow", "list_holds", "set_hold", "clear_hold", "REPO_KEY", "DEFAULT_NOTE"]

REPO_KEY = "coordinator.pushHold"
REPO_ALLOW_KEY = "coordinator.pushHoldAllow"
DEFAULT_NOTE = "held"
_SECTION = re.compile(r'^\[\s*([^\s\]"]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(.*)$')
_KEYVAL = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")


def _branch_key(branch: str) -> str:
    return f"branch.{branch}.coordinatorPushHold"


def _allow_key(branch: Optional[str]) -> str:
    return f"branch.{branch}.coordinatorPushHoldAllow" if branch else REPO_ALLOW_KEY


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value.endswith('"'):
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _parse(repo: Union[str, Path]):
    """(repo_hold, {branch: hold}, repo_allow, {branch: allow}) from the common-dir
    config; empty on any read failure."""
    try:
        text = (resolve_git_common_dir(repo) / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}, None, {}
    repo_hold: Optional[str] = None
    repo_allow: Optional[str] = None
    branches: dict[str, str] = {}
    allows: dict[str, str] = {}
    section = sub = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line[0] == "[":
            m = _SECTION.match(line)
            if not m:
                section = sub = None
                continue
            section, sub = m.group(1).lower(), m.group(2)
            line = m.group(3).strip()
            if not line or line[0] in "#;":
                continue
        m = _KEYVAL.match(line)
        if not m or section is None:
            continue
        key, value = m.group(1).lower(), _unquote(m.group(2) or "")
        if not value:
            continue
        if section == "coordinator" and sub is None and key == "pushhold":
            repo_hold = value
        elif section == "coordinator" and sub is None and key == "pushholdallow":
            repo_allow = value.lower()
        elif section == "branch" and sub is not None and key == "coordinatorpushhold":
            branches[sub] = value
        elif section == "branch" and sub is not None and key == "coordinatorpushholdallow":
            allows[sub] = value.lower()
    return repo_hold, branches, repo_allow, allows


def read_hold(repo: Union[str, Path], branch: Optional[str]) -> Optional[str]:
    """The hold note governing `branch` in `repo` (repo hold wins), else `None`."""
    return read_hold_allow(repo, branch)[0]


def read_hold_allow(repo: Union[str, Path], branch: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """`(note, allowed_sha)` for the hold governing `branch`; `(None, None)` when unheld."""
    repo_hold, branches, repo_allow, allows = _parse(repo)
    if repo_hold is not None:
        return repo_hold, repo_allow
    if branch and branch in branches:
        return branches[branch], allows.get(branch)
    return None, None


def list_holds(repo: Union[str, Path]) -> dict:
    """`{"repo": note|None, "branches": {name: note}}`."""
    repo_hold, branches, repo_allow, allows = _parse(repo)
    return {
        "repo": repo_hold,
        "branches": dict(sorted(branches.items())),
        "repo_allow": repo_allow,
        "branch_allow": dict(sorted(allows.items())),
    }


def _git_config(repo: Union[str, Path], args: list[str]) -> int:
    from coordinator_core.git.run import run_git

    return run_git(["config", *args], cwd=str(repo)).returncode


def set_hold(
    repo: Union[str, Path],
    branch: Optional[str] = None,
    reason: Optional[str] = None,
    allow_sha: Optional[str] = None,
) -> bool:
    """Set the hold; `allow_sha` (resolved to a full SHA) names the one commit a manual push may release.
    Without it any prior allowance is dropped."""
    key = _branch_key(branch) if branch else REPO_KEY
    akey = _allow_key(branch)
    if allow_sha:
        from coordinator_core.git.run import run_git

        res = run_git(["rev-parse", "--verify", "--quiet", f"{allow_sha}^{{commit}}"], cwd=str(repo))
        full = (res.stdout or "").strip() if res.returncode == 0 else ""
        if not full:
            return False
        if _git_config(repo, [akey, full]) != 0:
            return False
    elif _git_config(repo, ["--unset", akey]) not in (0, 5):
        return False
    return _git_config(repo, [key, (reason or "").strip() or DEFAULT_NOTE]) == 0


def clear_hold(repo: Union[str, Path], branch: Optional[str] = None) -> bool:
    """True when the hold is gone (git exits 5 for an absent key, which is also success)."""
    key = _branch_key(branch) if branch else REPO_KEY
    allow_gone = _git_config(repo, ["--unset", _allow_key(branch)]) in (0, 5)
    return _git_config(repo, ["--unset", key]) in (0, 5) and allow_gone
