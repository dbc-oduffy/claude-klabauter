"""coordinator_core.bash_guards.block_topic_branch -- PreToolUse(Bash) advisory:
remote-backed `main` commits.

The guard is not in `deny_registry`, so `apply_guard_level` records its verdict to the
advisory log and nothing reaches the caller; "denied" below names the verdict the guard
computes, never an effect. A push to a non-owned remote is
`block-push-pr-non-owned-remote`'s, not this guard's.

Verdict shapes: a `git commit` while the judged repo's HEAD is `main` and a remote is
configured (`git.main_commit_policy`). `git -C <dir> ...` and other git global options
are walked past; the repo judged is the one `-C`, or an earlier top-level `cd` in the
same command, enters. A topic-branch create or push emits nothing: that advisory named
no failure it prevented.

Override: `COORDINATOR_OVERRIDE_TOPIC_BRANCH=<non-empty reason>`, as an inline
env prefix on the git segment or on the `sh -c` wrapper that holds it; the hook environment never overrides.
An empty value never overrides.

A push to a held branch is `block-held-push`'s, a registered hard deny; this module
keeps the git-argv and repo-resolution helpers it and its siblings share.

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

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 43

OVERRIDE_KEY = "COORDINATOR_OVERRIDE_TOPIC_BRANCH"
MAIN_COMMIT_OVERRIDE_KEY = "COORDINATOR_OVERRIDE_BRANCH"

_PRE_FILTER_RE = re.compile(r"\bgit\b")
_PUSH_OPT_WITH_ARG = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_DELETE_FLAGS = frozenset({"-d", "--delete"})


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


_COMMAND_ENDS = frozenset({"|", "||", "&&", ";", "&"})
# A shell redirection token: `2>&1`, `>file`, `2>/dev/null`, `&>log`, `<in`, or a bare `>`.
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.S)
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


def _inline_reason(raw_tokens: List[str], key: str = OVERRIDE_KEY) -> str:
    prefix = key + "="
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


def _cd_target(cwd: Optional[str], args: List[str]) -> Optional[str]:
    """The directory `cd <args>` enters from `cwd`; `cwd` unchanged when the target is not
    one literal path."""
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 1 or "$" in positional[0] or "`" in positional[0] or positional[0] == "~":
        return cwd
    from coordinator_core.bash_guards._write_bump_sink_shapes import translate_msys_path

    target = translate_msys_path(positional[0]) or positional[0]
    return target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)


def _main_commit(
    tokens: List[str], raw_tokens: List[str], cwd: Optional[str], inherited: str = ""
) -> Optional[str]:
    """Refusal text when this `git commit` runs in a repo on a remote-backed `main`.

    `inherited` is an override reason prefixed on an enclosing `sh -c` wrapper."""
    parsed = _git_argv(tokens)
    if parsed is None or parsed[0] != "commit":
        return None
    git_root = _git_root(cwd, parsed[2])
    if not git_root:
        return None
    from coordinator_core.git.main_commit_policy import main_commit_refusal
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    override = _inline_reason(raw_tokens) or inherited
    if _inline_reason(raw_tokens, MAIN_COMMIT_OVERRIDE_KEY) == "1":
        override = "1"
    refusal = main_commit_refusal(git_root, override_reason=override)
    if refusal is None:
        return None
    return _record(
        f"Commit on remote-backed main: {refusal}",
        f"Commit on remote-backed main: {refusal} Alternative: commit to the day branch work/<machine>/<date>.",
        AdaptableOffer(template="commit to the day branch work/<machine>/<date>", what_to_adapt="the machine and date"),
        _escalation("the commit you need on main"),
    )


def _record(summary: str, em_text: str, *members: Any) -> Any:
    from coordinator_core.guard_messages.alternative_record import AlternativeRecord

    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-topic-branch",
        summary=summary,
        em_text=em_text,
        members=members,
    )


def _escalation(report_what: str) -> Any:
    from coordinator_core.guard_messages.alternative_record import EscalationRoute

    return EscalationRoute(report_what=report_what, authorize_what="the PM granting an exception")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    cmd = cmd.replace("\r", "")
    cwd = payload.get("cwd")

    segments = resolve_command_positions(cmd, preserve_windows_backslashes=True)
    # The repo a git segment acts on is the one an earlier top-level `cd` entered, exactly as
    # `git -C` names it. Trap: the tokenizer flattens a subshell, so a `cd` inside `( ... )`
    # would leak past its `)`; a command with any subshell keeps the payload cwd.
    # Only an unquoted paren opens a subshell: a quoted `python -c "...f(x)"` body is an argument.
    follow_cd = not any(c in _QUOTED_RE.sub("", cmd) for c in "()")
    # An override prefixed on a wrapper (`sh -c '...'`) covers the git segments inside it.
    wrapper_reason: Dict[int, str] = {}
    for resolved in segments:
        depth = resolved.depth
        inherited = next((wrapper_reason[d] for d in range(depth - 1, -1, -1) if d in wrapper_reason), "")
        own = _inline_reason(resolved.raw_tokens)
        for d in [d for d in wrapper_reason if d >= depth]:
            del wrapper_reason[d]
        if own or inherited:
            wrapper_reason[depth] = own or inherited
        if follow_cd and resolved.depth == 0 and resolved.tokens[:1] == ["cd"]:
            cwd = _cd_target(cwd, resolved.tokens[1:])
            continue
        main_commit = _main_commit(resolved.tokens, resolved.raw_tokens, cwd, inherited)
        if main_commit is not None:
            return main_commit
    return None
