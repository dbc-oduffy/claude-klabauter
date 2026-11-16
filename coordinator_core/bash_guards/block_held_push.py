"""coordinator_core.bash_guards.block_held_push -- PreToolUse(Bash, PowerShell) hard deny:
a `git push` to a branch carrying a push hold (`coordinator_core.push_hold`).

A bare `git push`, `git push origin HEAD` and `git push origin <src>:<branch>` are judged
against the branch they update. A hold with an allowed SHA admits only an explicit refspec
whose source resolves to that SHA. A delete of a held branch and `--all`/`--mirror` while any
hold exists are denied. A repo-wide hold covers every branch. No override: clearing the hold
(`push-hold clear`) is the route.

The hold stores a note only; the denial names the branch and that note.

Zero git spawns: pure token parsing, `.git/HEAD` and `.git/config` file reads.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import resolve_command_positions
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_topic_branch import (
    _PUSH_DELETE_FLAGS,
    _QUOTED_RE,
    _cd_target,
    _git_argv,
    _git_root,
    _head_branch,
    _push_positional,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 45

_PRE_FILTER_RE = re.compile(r"\bpush\b")
_HEADS_PREFIX = "refs/heads/"
_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _resolve_sha(git_root: str, src: str) -> Optional[str]:
    """`src` lowercased when hex (a prefix is matched against the allowed SHA by the
    caller), else the SHA a loose/packed ref or HEAD names; None when unresolvable."""
    low = src.lower()
    if _HEX_RE.match(low):
        return low
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

        gd = str(resolve_git_common_dir(git_root))
        if src == "HEAD":
            branch = _head_branch(git_root)
            if branch is None:
                with open(os.path.join(str(resolve_git_dir(git_root)), "HEAD"), encoding="utf-8") as fh:
                    return fh.read().strip().lower()
            src = _HEADS_PREFIX + branch
        full = src if src.startswith("refs/") else None
        for cand in ([full] if full else [_HEADS_PREFIX + src, "refs/tags/" + src]):
            path = os.path.join(gd, *cand.split("/"))
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read().strip().lower()
            with open(os.path.join(gd, "packed-refs"), encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == cand:
                        return parts[0].lower()
    except Exception:  # noqa: BLE001 -- an unresolvable source is not the allowed SHA
        return None
    return None


def _held_push(tokens: List[str], cwd: Optional[str]) -> Optional[Any]:
    """Deny record when this `git push` targets a held branch not released by its
    allowed SHA, else None."""
    parsed = _git_argv(tokens)
    if parsed is None or parsed[0] != "push":
        return None
    _, args, c_dirs = parsed
    deleting = any(t in _PUSH_DELETE_FLAGS for t in args)
    git_root = _git_root(cwd, c_dirs)
    if not git_root:
        return None
    from coordinator_core import push_hold

    positional = _push_positional(args)
    if positional[1:2] == ["tag"]:
        return None
    if any(t in ("--all", "--mirror") for t in args):
        holds = push_hold.list_holds(git_root)
        note = holds["repo"] or next(iter(holds["branches"].values()), None)
        return _deny_record(None, note) if note else None
    specs = positional[1:]
    if not specs:
        head = _head_branch(git_root)
        pairs = [(None, head)] if head else []
    else:
        pairs = []
        for spec in specs:
            spec = spec.lstrip("+")
            src, sep, dst = spec.partition(":")
            ref = dst if sep else spec
            deleting_ref = True if sep and not src else deleting
            if ref == "HEAD":
                ref = _head_branch(git_root)
            if ref and ref.startswith(_HEADS_PREFIX):
                ref = ref[len(_HEADS_PREFIX):]
            elif ref and ref.startswith("refs/"):
                continue
            if ref:
                pairs.append((None if deleting_ref or not sep else src, ref))
    for src, branch in pairs:
        note, allow = push_hold.read_hold_allow(git_root, branch)
        if note is None:
            continue
        sha = _resolve_sha(git_root, src) if src and allow else None
        if sha and allow.startswith(sha):
            continue
        return _deny_record(branch, note)
    return None


def _deny_record(branch: Optional[str], note: str) -> Any:
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
        ReadyCommand,
    )

    where = f"branch `{branch}`" if branch else "repo"
    summary = f"Push hold on {where}: {note}."
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-held-push",
        summary=summary,
        em_text=(
            f"{summary} Alternative: `push-hold clear`, or push the hold's --allow-sha as an "
            "explicit `<sha>:refs/heads/<branch>` refspec."
        ),
        members=(
            ReadyCommand(text="push-hold clear"),
            AdaptableOffer(
                template="push the hold's --allow-sha as an explicit `<sha>:refs/heads/<branch>` refspec",
                what_to_adapt="the sha and branch",
            ),
        ),
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    cmd = cmd.replace("\r", "")
    cwd = payload.get("cwd")
    follow_cd = not any(c in _QUOTED_RE.sub("", cmd) for c in "()")
    for resolved in resolve_command_positions(cmd, preserve_windows_backslashes=True):
        if follow_cd and resolved.depth == 0 and resolved.tokens[:1] == ["cd"]:
            cwd = _cd_target(cwd, resolved.tokens[1:])
            continue
        held = _held_push(resolved.tokens, cwd)
        if held is not None:
            return held
    return None
