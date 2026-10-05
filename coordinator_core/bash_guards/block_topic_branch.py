"""coordinator_core.bash_guards.block_topic_branch -- PreToolUse(Bash) hard
deny: commit only to the session's day branch.

Denies a command that creates or pushes a branch which is neither `main` nor
a day branch:
  `git checkout -b|-B <ref>`, `git switch -c|-C|--create|--force-create <ref>`,
  `git branch <new-ref> [<start>]`, `git push <remote> <ref>|<src>:<dst>`
  (judged on the destination ref). `git -C <dir> ...` and other git global
  options are walked past. Listing, `-d`/`-D`/`-m`/`-c` and any other
  non-create `git branch` flag, tag pushes, ref deletions, and a push with no
  refspec are never denied.

Day branch, per `hooks.day_branch_assert` / `daily_branch`: the repo's
`coordinator.dayBranch` designation (exact), else `work/<machine>/<date>`
where `<machine>` is `machine_resolver.compute_machine()`. The date is NOT
matched against today: the boot invariant adopts or inherits a prior day's
branch, so any `work/<this-machine>/<YYYY-MM-DD[toDD]>[-N]` ref is a day
branch. `git push <remote> HEAD` resolves HEAD from `.git/HEAD` (no spawn);
an unresolvable HEAD or ref name allows.

Override: `COORDINATOR_OVERRIDE_TOPIC_BRANCH=<non-empty reason>`, as an inline
env prefix on the git segment only; the hook environment never overrides.
An empty value never overrides.

Zero git spawns: pure token parsing, `.git/HEAD` and `.git/config` file reads.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._rewrite_support import _GIT_GLOBAL_OPT_WITH_ARG
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.daily_branch import read_configured_day_branch

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 43

OVERRIDE_KEY = "COORDINATOR_OVERRIDE_TOPIC_BRANCH"

_PRE_FILTER_RE = re.compile(r"\bgit\b")
_DAY_SHAPE_RE = re.compile(r"^work/([^/]+)/\d{4}-\d{2}-\d{2}(?:to\d{2})?(?:-\d+)?$")
_CHECKOUT_CREATE_FLAGS = frozenset({"-b", "-B"})
_SWITCH_CREATE_FLAGS = frozenset({"-c", "-C", "--create", "--force-create"})
_BRANCH_CREATE_COMPATIBLE_FLAGS = frozenset({"-f", "--force", "-t", "--track", "--no-track"})
_PUSH_OPT_WITH_ARG = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_DELETE_FLAGS = frozenset({"-d", "--delete"})
_HEADS_PREFIX = "refs/heads/"


def _looks_unreadable(name: str) -> bool:
    return (
        not name.strip()
        or name != name.strip()
        or name.startswith(("$", "-"))
        or "`" in name
        or "$(" in name
        or name.startswith("/")
        or name.endswith("/")
        or "//" in name
    )


def _git_argv(tokens: List[str]) -> Optional[tuple]:
    """`(subcommand, args, c_dirs)` past git's global options, else None."""
    if len(tokens) < 2 or not token_matches_binary(tokens[0], "git"):
        return None
    i = 1
    c_dirs: List[str] = []
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        if opt == "-C" and i + 1 < len(tokens):
            c_dirs.append(tokens[i + 1])
            i += 2
        elif opt in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
        else:
            i += 1
    if i >= len(tokens):
        return None
    return tokens[i], tokens[i + 1:], c_dirs


def _create_flag_target(args: List[str], create_flags: frozenset) -> Optional[str]:
    for i, tok in enumerate(args):
        if tok in create_flags:
            return args[i + 1] if i + 1 < len(args) else None
    return None


def _branch_create_target(args: List[str]) -> Optional[str]:
    if any(t.startswith("-") and t not in _BRANCH_CREATE_COMPATIBLE_FLAGS for t in args):
        return None
    positional = [t for t in args if not t.startswith("-")]
    return positional[0] if positional else None


_COMMAND_ENDS = frozenset({"|", "||", "&&", ";", "&"})
# A shell redirection token: `2>&1`, `>file`, `2>/dev/null`, `&>log`, `<in`, or a bare `>`.
_REDIRECT_RE = re.compile(r"^(?:\d*|&)(?:>>?|<)&?")


def _push_targets(args: List[str]) -> List[str]:
    """Destination refs of a `git push`; empty when nothing is judgeable."""
    if any(t in _PUSH_DELETE_FLAGS for t in args):
        return []
    positional: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in _COMMAND_ENDS:
            break
        redirect = _REDIRECT_RE.match(tok)
        if redirect is not None:
            # A bare operator (`>`, `2>`) takes the next token as its target.
            i += 2 if redirect.end() == len(tok) else 1
            continue
        if tok in _PUSH_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positional.append(tok)
        i += 1
    targets: List[str] = []
    for spec in positional[1:]:
        spec = spec.lstrip("+")
        src, sep, dst = spec.partition(":")
        if sep and not src:
            continue
        targets.append(dst if sep else spec)
    return targets


def _head_branch(git_root: Optional[str]) -> Optional[str]:
    if not git_root:
        return None
    try:
        from coordinator_core.git.git_dir import resolve_git_dir

        with open(os.path.join(str(resolve_git_dir(git_root)), "HEAD"), encoding="utf-8") as fh:
            text = fh.read().strip()
    except Exception:  # noqa: BLE001 -- unreadable HEAD allows
        return None
    m = re.match(r"^ref:\s*refs/heads/(.+)$", text)
    return m.group(1) if m else None


def _machine() -> str:
    from coordinator_core.machine_resolver import compute_machine

    return compute_machine()


def _is_day_branch(name: str, configured: Optional[str], machine: str) -> bool:
    if configured and name == configured:
        return True
    m = _DAY_SHAPE_RE.match(name)
    return bool(m) and m.group(1) == machine


def _inline_reason(raw_tokens: List[str]) -> str:
    prefix = OVERRIDE_KEY + "="
    for tok in raw_tokens:
        if token_matches_binary(tok, "git"):
            break
        if tok.startswith(prefix):
            return tok[len(prefix):].strip()
    return ""


def _offending_ref(
    tokens: List[str], cwd: Optional[str]
) -> Optional[str]:
    parsed = _git_argv(tokens)
    if parsed is None:
        return None
    sub, args, c_dirs = parsed
    if sub == "checkout":
        refs = [_create_flag_target(args, _CHECKOUT_CREATE_FLAGS)]
    elif sub == "switch":
        refs = [_create_flag_target(args, _SWITCH_CREATE_FLAGS)]
    elif sub == "branch":
        refs = [_branch_create_target(args)]
    elif sub == "push":
        refs = _push_targets(args)
    else:
        return None

    from coordinator_core.subagent_sandbox.engine import resolve_git_root_cheap

    start = cwd or os.getcwd()
    for d in c_dirs:
        start = d if os.path.isabs(d) else os.path.join(start, d)
    git_root = resolve_git_root_cheap(start)
    configured = read_configured_day_branch(git_root) if git_root else None
    machine: Optional[str] = None
    for ref in refs:
        if ref is None or _looks_unreadable(ref):
            continue
        if ref == "HEAD" and sub == "push":
            ref = _head_branch(git_root)
            if ref is None:
                continue
        if ref.startswith(_HEADS_PREFIX):
            ref = ref[len(_HEADS_PREFIX):]
        elif ref.startswith("refs/"):
            continue
        if ref == "main":
            continue
        if machine is None:
            machine = _machine()
        if not _is_day_branch(ref, configured, machine):
            return ref
    return None


def _deny_reason(ref: str) -> str:
    return (
        f"BLOCKED: topic branch `{ref}` -- commit to the day branch "
        "(work/<machine>/<date>); the Group EM grants exceptions.\n\n"
        f"Exception: {OVERRIDE_KEY} with a non-empty reason, prefixed on the command."
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

    for resolved in resolve_command_positions(cmd):
        ref = _offending_ref(resolved.tokens, cwd)
        if ref is None:
            continue
        if _inline_reason(resolved.raw_tokens):
            continue
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _deny_reason(ref),
            }
        }
    return None
