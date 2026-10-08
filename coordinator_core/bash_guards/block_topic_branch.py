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

A push of a registered `publish.mirrors.<key>` repo's declared `track_ref`
branch (the command publish.py prints) is allowed.

A cloud session (`CLAUDE_CODE_REMOTE=true`) may use its harness-assigned
`claude/...` branch.

Override: `COORDINATOR_OVERRIDE_TOPIC_BRANCH=<non-empty reason>`, as an inline
env prefix on the git segment only; the hook environment never overrides.
An empty value never overrides.

Push hold (`coordinator_core.push_hold`): a `git push` whose target branch is
held (repo-wide or per-branch) is denied, and the override above never clears
it, deletes included. A hold with an allowed SHA admits only an explicit refspec whose source
resolves to that SHA. `--all`/`--mirror` are denied when any hold exists.

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
from coordinator_core.hooks.day_branch_assert import (
    _CLOUD_SESSION_BRANCH_PREFIX,
    is_cloud_session,
)

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
# publish.py's commit-only round leaves the mirror's work on this branch.
_PUBLISH_BRANCH = "candidate"


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


def _push_positional(args: List[str]) -> List[str]:
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
    return positional


def _push_targets(args: List[str]) -> List[str]:
    """Destination refs of a `git push`; empty when nothing is judgeable."""
    if any(t in _PUSH_DELETE_FLAGS for t in args):
        return []
    positional = _push_positional(args)
    targets: List[str] = []
    if positional[1:2] == ["tag"]:
        return targets
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


def _is_local_tag(git_root: Optional[str], name: str) -> bool:
    """True when `name` is an existing local tag: loose ref or packed-refs line."""
    if not git_root:
        return False
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir

        gd = str(resolve_git_common_dir(git_root))
        if os.path.isfile(os.path.join(gd, "refs", "tags", *name.split("/"))):
            return True
        with open(os.path.join(gd, "packed-refs"), encoding="utf-8") as fh:
            suffix = " refs/tags/" + name
            return any(line.rstrip("\n").endswith(suffix) for line in fh)
    except Exception:  # noqa: BLE001 -- unreadable tag state keeps the block
        return False


def _machine() -> str:
    from coordinator_core.machine_resolver import compute_machine

    return compute_machine()


def _is_publish_push(sub: str, ref: str, git_root: Optional[str]) -> bool:
    if sub != "push" or ref != _PUBLISH_BRANCH or not git_root:
        return False
    from coordinator_core.engine_root import is_published_engine_mirror

    return is_published_engine_mirror(git_root)


def _is_day_branch(name: str, configured: Optional[str], machine: str) -> bool:
    if configured and name == configured:
        return True
    m = _DAY_SHAPE_RE.match(name)
    return bool(m) and m.group(1) == machine


def _is_publish_mirror_branch(git_root: Optional[str], ref: str) -> bool:
    """True when `git_root` is a registered `publish.mirrors.<key>` repo and
    `ref` is that mirror's declared `track_ref` branch (the command publish.py
    prints). Registry TOML reads only; runs on the deny path alone."""
    if not git_root:
        return False
    try:
        from coordinator_core.machine_resolver import _flatten, _load_toml, registry_dir

        merged: Dict[str, Any] = {}
        for fname in ("registry.toml", "registry.local.toml"):
            merged.update(_flatten(_load_toml(registry_dir() / fname)))
        root = os.path.normcase(os.path.realpath(git_root))
        prefix, suffix = "publish.mirrors.", ".path"
        for key, value in merged.items():
            if not (key.startswith(prefix) and key.endswith(suffix)) or not value:
                continue
            if os.path.normcase(os.path.realpath(str(value))) != root:
                continue
            track = merged.get(key[: -len(suffix)] + ".track_ref")
            if not isinstance(track, str):
                continue
            if track.startswith("origin/"):
                track = track[len("origin/"):]
            if track and track == ref:
                return True
    except Exception:  # noqa: BLE001 -- an unreadable registry keeps the block
        return False
    return False


def _inline_reason(raw_tokens: List[str]) -> str:
    prefix = OVERRIDE_KEY + "="
    for tok in raw_tokens:
        if token_matches_binary(tok, "git"):
            break
        if tok.startswith(prefix):
            return tok[len(prefix):].strip()
    return ""


def _git_root(cwd: Optional[str], c_dirs: List[str]) -> Optional[str]:
    from coordinator_core.subagent_sandbox.engine import resolve_git_root_cheap

    from coordinator_core.bash_guards._write_bump_sink_shapes import translate_msys_path

    def _native(p: str) -> str:
        translated = translate_msys_path(p)
        return p if translated is None else translated

    start = _native(cwd or os.getcwd())
    for d in (_native(d) for d in c_dirs):
        start = d if os.path.isabs(d) else os.path.join(start, d)
    return resolve_git_root_cheap(start)


_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _resolve_sha(git_root: str, src: str) -> Optional[str]:
    """`src` lowercased when hex (a prefix is matched against the allowed SHA by the
    caller), else the SHA a loose/packed ref or HEAD names; None when unresolvable."""
    low = src.lower()
    if _HEX_RE.match(low):
        return low
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir

        gd = str(resolve_git_common_dir(git_root))
        if src == "HEAD":
            branch = _head_branch(git_root)
            if branch is None:
                with open(os.path.join(str(_git_dir(git_root)), "HEAD"), encoding="utf-8") as fh:
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


def _git_dir(git_root: str):
    from coordinator_core.git.git_dir import resolve_git_dir

    return resolve_git_dir(git_root)


def _held_push(tokens: List[str], cwd: Optional[str]) -> Optional[str]:
    """Deny reason when this `git push` targets a held branch not released by
    its allowed SHA, else None."""
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
        return _hold_reason(None, note) if note else None
    specs = positional[1:]
    head = None
    if not specs:
        head = _head_branch(git_root)
        pairs = [(None, head)] if head else []
    else:
        pairs = []
        for spec in specs:
            spec = spec.lstrip("+")
            src, sep, dst = spec.partition(":")
            ref = dst if sep else spec
            if sep and not src:
                deleting_ref = True
            else:
                deleting_ref = deleting
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
        return _hold_reason(branch, note)
    return None


def _hold_reason(branch: Optional[str], note: str) -> str:
    where = f"branch `{branch}`" if branch else "repo"
    return (
        f"BLOCKED: push hold on {where}: {note}. "
        "Alternative: `push-hold clear`, or push the hold's --allow-sha as an explicit "
        "`<sha>:refs/heads/<branch>` refspec."
    )


def _offending_ref(
    tokens: List[str], cwd: Optional[str], env: Optional[Dict[str, str]] = None
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

    git_root = _git_root(cwd, c_dirs)
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
        if sub == "push" and _is_local_tag(git_root, ref):
            continue
        if _is_publish_push(sub, ref, git_root):
            continue
        if is_cloud_session(env) and ref.startswith(_CLOUD_SESSION_BRANCH_PREFIX):
            continue
        if machine is None:
            machine = _machine()
        if sub == "push" and _is_publish_mirror_branch(git_root, ref):
            continue
        if not _is_day_branch(ref, configured, machine):
            return ref
    return None


def _deny_reason(ref: str) -> str:
    return (
        f"BLOCKED: topic branch `{ref}` -- commit to the day branch "
        "(work/<machine>/<date>); the Group EM grants exceptions. "
        "The whole command did not run: file writes chained before the "
        "blocked segment did not happen either.\n\n"
        f"Exception: {OVERRIDE_KEY} with a non-empty reason, prefixed on the command."
    )


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    cmd = cmd.replace("\r", "")
    cwd = payload.get("cwd")

    env = payload.get("env")
    if not isinstance(env, dict):
        env = None

    for resolved in resolve_command_positions(
        cmd, preserve_windows_backslashes=True
    ):
        held = _held_push(resolved.tokens, cwd)
        if held is not None:
            return _deny(held)
        ref = _offending_ref(resolved.tokens, cwd, env)
        if ref is None:
            continue
        if _inline_reason(resolved.raw_tokens):
            continue
        return _deny(_deny_reason(ref))
    return None
