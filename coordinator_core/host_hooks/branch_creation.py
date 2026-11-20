"""Codex branch-creation refusal: a non-day-branch ref is never minted from a Codex session.

Why it lives in the host shim and not only in the shared bash guard chain: the chain's
`block-noncanonical-branch-creation` is an advisory, and a Codex hook that misses the warm engine
(the host-guard degrade log is mostly timeouts) answers neutral -- the cold chain costs ~800ms and is
skipped -- so a Codex session got no signal at all, and `git push` of a new ref was never covered by
anything. This module is stdlib plus `daily_branch` (pure regex), reads files only, spawns nothing.

Invariants:
- Scope: a repo whose root holds `.coordinator-local/` (the fleet's per-repo marker). A foreign clone is
  never judged.
- Canonical names, `main`/`master`, the sanctioned long-lived prefixes and a designated
  `coordinator.dayBranch` pass.
- A push creates a ref only when the remote-tracking ref is absent (loose or packed); fails open on
  anything unreadable.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.daily_branch import is_canonical_branch, read_configured_day_branch
from coordinator_core.host_hooks.backstop import _SEGMENT_SPLIT, _WRAPPERS, _command, _push_segment
from coordinator_core.host_hooks.contract import HostEvent

#: deny_registry.GUARD_DENY_CLASS name.
GUARD = "branch-creation"
SANCTIONED_PREFIXES = ("migration/", "release/", "feature/")
_MAIN = ("main", "master")
_CHECKOUT_CREATE = ("-b", "-B")
_SWITCH_CREATE = ("-c", "-C", "--create", "--force-create")
_BRANCH_CREATE_FLAGS = ("-f", "--force", "-t", "--track", "--no-track")
_PUSH_VALUE_FLAGS = ("--repo", "-o", "--push-option", "--receive-pack", "--exec", "--signed")


def _git_dirs(start: str) -> Optional[tuple]:
    """(repo root, git dir, common dir) for the repo holding `start`, else None."""
    here = Path(start or ".")
    for directory in (here, *here.parents):
        dot_git = directory / ".git"
        try:
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = Path(text[len("gitdir:"):].strip())
                gitdir = gitdir if gitdir.is_absolute() else directory / gitdir
                common = gitdir
                marker = gitdir / "commondir"
                if marker.is_file():
                    rel = Path(marker.read_text(encoding="utf-8", errors="replace").strip())
                    common = rel if rel.is_absolute() else gitdir / rel
                return directory, gitdir, common
            if dot_git.is_dir():
                return directory, dot_git, dot_git
        except OSError:
            return None
    return None


def _acceptable(name: str, day_branch: Optional[str]) -> bool:
    bare = name.removeprefix("refs/heads/")
    return (not bare or bare in _MAIN or bare.startswith(SANCTIONED_PREFIXES)
            or is_canonical_branch(bare, day_branch) or "$" in bare or "`" in bare)


def _remote_has(common: Path, remote: str, name: str) -> bool:
    try:
        if (common / "refs" / "remotes" / remote / name).is_file():
            return True
        packed = common / "packed-refs"
        if packed.is_file():
            needle = " refs/remotes/%s/%s" % (remote, name)
            return any(line.rstrip().endswith(needle) for line in packed.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return True  # unreadable: assume it exists, never refuse on a guess
    return False


def _tag_exists(common: Path, name: str) -> bool:
    """True when `refs/tags/<name>` is loose or packed: `git push origin v1.0` pushes a tag, not a branch."""
    try:
        if (common / "refs" / "tags" / name).is_file():
            return True
        packed = common / "packed-refs"
        if packed.is_file():
            needle = " refs/tags/%s" % name
            return any(line.rstrip().endswith(needle) for line in packed.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return True
    return False


def _git_tokens(tokens: List[str]) -> Optional[List[str]]:
    """Tokens after `git` with leading global flags removed, or None when not a git call."""
    i = 0
    while i < len(tokens) and (tokens[i] in _WRAPPERS or ("=" in tokens[i] and not tokens[i].startswith("-"))):
        i += 1
    if i >= len(tokens) or os.path.basename(tokens[i]).lower() not in ("git", "git.exe"):
        return None
    rest = tokens[i + 1:]
    j = 0
    while j < len(rest) and rest[j].startswith("-"):
        j += 2 if rest[j] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") else 1
    return rest[j:]


def _created_names(tokens: List[str], cwd: str, dirs: tuple) -> List[str]:
    rest = _git_tokens(tokens)
    if not rest:
        return []
    sub, args = rest[0], rest[1:]
    if sub == "checkout" or sub == "switch":
        flags = _CHECKOUT_CREATE if sub == "checkout" else _SWITCH_CREATE
        for i, arg in enumerate(args):
            if arg in flags and i + 1 < len(args):
                return [args[i + 1]]
        return []
    if sub == "branch":
        if any(a.startswith("-") and a not in _BRANCH_CREATE_FLAGS for a in args):
            return []
        positional = [a for a in args if not a.startswith("-")]
        return positional[:1]
    if sub == "push":
        found = _push_segment(tokens)
        if found is None:
            return []
        chdir, pargs = found
        return _pushed_new_refs(pargs, os.path.join(cwd, chdir) if chdir else cwd, dirs)
    return []


def _pushed_new_refs(args: List[str], cwd: str, dirs: tuple) -> List[str]:
    if any(a in ("--delete", "-d", "--tags", "--all", "--mirror", "--prune") for a in args):
        return []
    positionals: List[str] = []
    i = 0
    while i < len(args):
        if args[i] in _PUSH_VALUE_FLAGS:
            i += 2
            continue
        if not args[i].startswith("-"):
            positionals.append(args[i])
        i += 1
    remote = positionals[0] if positionals else "origin"
    if "/" in remote or ":" in remote or remote.startswith("."):
        return []  # a URL or path remote has no tracking namespace to compare against
    refspecs = positionals[1:]
    if not refspecs:
        from coordinator_core.host_hooks.backstop import _current_branch
        current = _current_branch(cwd)
        refspecs = [current] if current else []
    new: List[str] = []
    for refspec in refspecs:
        src, _, dst = refspec.lstrip("+").partition(":")
        if ":" in refspec and not dst:
            continue  # delete
        dst = dst or src
        if dst == "HEAD":
            from coordinator_core.host_hooks.backstop import _current_branch
            dst = _current_branch(cwd)
        if not dst or dst.startswith("refs/tags/"):
            continue
        bare = dst.removeprefix("refs/heads/")
        if not dst.startswith("refs/heads/") and _tag_exists(dirs[2], bare):
            continue
        if not _remote_has(dirs[2], remote, bare):
            new.append(bare)
    return new


def match(host_event: HostEvent) -> Optional[Dict[str, str]]:
    """The deny verdict for a Codex command that mints a non-canonical branch (local or remote), else None."""
    try:
        if host_event.tool_name != "Bash":
            return None
        command = _command(host_event)
        lowered = command.lower()
        if "git" not in lowered or not any(v in lowered for v in ("checkout", "switch", "branch", "push")):
            return None
        dirs = _git_dirs(host_event.cwd)
        if dirs is None or not (dirs[0] / ".coordinator-local").is_dir():
            return None
        day_branch = read_configured_day_branch(dirs[0])
        for segment in _SEGMENT_SPLIT.split(command.replace("\r", "")):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                continue
            for name in _created_names(tokens, host_event.cwd, dirs):
                if not _acceptable(name, day_branch):
                    return {"decision": "deny", "reason": (
                        "BLOCKED: `%s` is not a day branch; Codex sessions do not mint topic branches. "
                        "Commit on the current day branch (`<owner>/<machine>/<date>`)." % name)}
    except Exception:  # noqa: BLE001 -- the guard never breaks the hook
        return None
    return None
