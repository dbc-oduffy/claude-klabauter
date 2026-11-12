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

__all__ = [
    "read_hold", "read_hold_allow", "list_holds", "set_hold", "clear_hold", "REPO_KEY", "DEFAULT_NOTE",
    "COMPILE_HOLD_KEY", "COMPILE_GREEN_KEY", "CONTROLLED_PREFIX", "COMPILE_HOLD_REFUSAL",
    "read_compile_hold", "read_compile_green_tree", "set_compile_hold", "clear_compile_hold",
    "stamp_green_tree", "ladder_refusal",
]

REPO_KEY = "coordinator.pushHold"
COMPILE_HOLD_KEY = "coordinator.compileHold"
COMPILE_GREEN_KEY = "coordinator.compileGreenTree"
CONTROLLED_PREFIX = "control/plugin/"
COMPILE_HOLD_REFUSAL = "compile-hold"
_TREE_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
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
    return _scan(repo)[:4]


def _scan(repo: Union[str, Path]):
    """`_parse`'s 4-tuple plus (compile_hold, compile_green_tree)."""
    try:
        text = (resolve_git_common_dir(repo) / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}, None, {}, None, None
    compile_hold: Optional[str] = None
    compile_green: Optional[str] = None
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
        elif section == "coordinator" and sub is None and key == "compilehold":
            compile_hold = value
        elif section == "coordinator" and sub is None and key == "compilegreentree":
            compile_green = value.lower()
        elif section == "branch" and sub is not None and key == "coordinatorpushhold":
            branches[sub] = value
        elif section == "branch" and sub is not None and key == "coordinatorpushholdallow":
            allows[sub] = value.lower()
    return repo_hold, branches, repo_allow, allows, compile_hold, compile_green


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


def read_compile_hold(repo: Union[str, Path]) -> Optional[str]:
    """The compile-hold note, else `None`; independent of the push freeze."""
    return _scan(repo)[4]


def read_compile_green_tree(repo: Union[str, Path]) -> Optional[str]:
    """The lower-cased tree sha stamped green, else `None`."""
    return _scan(repo)[5]


def set_compile_hold(repo: Union[str, Path], reason: Optional[str] = None) -> bool:
    """Set the compile hold (CLI path only); a stale green stamp is dropped."""
    if _git_config(repo, ["--unset", COMPILE_GREEN_KEY]) not in (0, 5):
        return False
    return _git_config(repo, [COMPILE_HOLD_KEY, (reason or "").strip() or DEFAULT_NOTE]) == 0


def clear_compile_hold(repo: Union[str, Path]) -> bool:
    """True when the compile hold is gone (git exit 5, absent, is success)."""
    return _git_config(repo, ["--unset", COMPILE_HOLD_KEY]) in (0, 5)


def stamp_green_tree(repo: Union[str, Path], tree_sha: str) -> bool:
    """Record `tree_sha` (40/64-hex) as the compile-green tree; False on a malformed sha."""
    sha = (tree_sha or "").strip()
    if not _TREE_SHA.match(sha):
        return False
    return _git_config(repo, [COMPILE_GREEN_KEY, sha.lower()]) == 0


def ladder_refusal(repo: Union[str, Path], changed_paths, tree_sha: Optional[str]) -> Optional[str]:
    """A `compile-hold: ...` refusal when the hold is set, `changed_paths` touch `control/plugin/`
    and `tree_sha` is not the stamped green tree; else `None`. Zero spawns."""
    _, _, _, _, hold, green = _scan(repo)
    if hold is None:
        return None
    if tree_sha and green and tree_sha.strip().lower() == green:
        return None
    hit = [p for p in changed_paths if p.replace("\\", "/").removeprefix("./").startswith(CONTROLLED_PREFIX)]
    if not hit:
        return None
    return (
        f"{COMPILE_HOLD_REFUSAL}: {len(hit)} path(s) under {CONTROLLED_PREFIX} while the compile hold is set "
        f"({hold}): {', '.join(hit[:3])}; clear with `push-hold clear --compile` or stamp the merged "
        f"tree green with `push-hold stamp-green --tree <tree_sha>`"
    )
