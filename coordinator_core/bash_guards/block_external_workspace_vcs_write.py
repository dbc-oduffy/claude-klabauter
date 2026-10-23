"""coordinator_core.bash_guards.block_external_workspace_vcs_write -- PreToolUse(Bash,
PowerShell) hard deny: no git history write in or into a registered external workspace.

A registered external workspace (`external_workspaces.<name>`) holds someone else's code;
nothing here may commit to it, branch it, tag it, push from it or initialise a repository
in it. The effective directory is the payload cwd (followed through a leading `cd`), each
`git -C <dir>`, `--git-dir`, `--work-tree`, `GIT_DIR` / `GIT_WORK_TREE`, and the
destination of `git init <dir>`, `git clone <src> <dest>` and `git worktree add <path>`.
Read verbs (`status`, `log`, `diff`, `show`, `ls-files`, `rev-parse`, `blame`) pass.

Fails closed: a hidden (`$VAR`, `$( )`, backtick) directory while the cwd is inside a
workspace denies; an unreadable registry denies a git write verb and never a read.

No override, marker or level: the PM made "no commit" a hard requirement, so the guard
is a `FLOOR_GUARDS` member.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 43

_PRE_FILTER_RE = re.compile(r"(?i)\bgit")
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.S)
_ENV_ASSIGN_RE = re.compile(r"^(GIT_DIR|GIT_WORK_TREE)=(.*)$")

#: Verbs that always write history or refs.
_ALWAYS_WRITE = frozenset(
    {
        "init", "clone", "commit", "merge", "rebase", "am", "cherry-pick", "revert",
        "push", "reset", "update-ref", "pull", "commit-tree", "filter-branch",
        "fast-import", "replace",
    }
)
#: git global options that consume the next token.
_GLOBAL_OPT_WITH_ARG = frozenset({"-c", "--namespace", "--exec-path-arg", "--super-prefix", "--config-env"})
_GLOBAL_DIR_OPTS = ("--git-dir", "--work-tree")
_CLONE_OPT_WITH_ARG = frozenset(
    {
        "-b", "--branch", "--depth", "-o", "--origin", "--reference", "--reference-if-able",
        "--template", "-c", "--config", "--separate-git-dir", "-j", "--jobs", "--filter",
        "--server-option", "-u", "--upload-pack", "--shallow-since", "--shallow-exclude",
        "--bundle-uri", "--ref-format", "--revision",
    }
)
_INIT_OPT_WITH_ARG = frozenset(
    {"--template", "--separate-git-dir", "-b", "--initial-branch", "--object-format", "--ref-format"}
)
_WORKTREE_OPT_WITH_ARG = frozenset({"-b", "-B", "--reason"})
_BRANCH_MUTATING = frozenset(
    {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move", "--copy", "--force",
     "--set-upstream-to", "--unset-upstream", "--edit-description"}
)
_BRANCH_LISTING = frozenset(
    {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
     "--show-current", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
)
_TAG_MUTATING = frozenset({"-d", "--delete", "-a", "-s", "-f", "--force", "-m", "-u", "-F"})
_TAG_LISTING = frozenset(
    {"-l", "--list", "-v", "--verify", "--contains", "--no-contains", "--merged", "--no-merged",
     "--points-at"}
)
_STASH_READ = frozenset({"list", "show"})


def _hidden(tok: str) -> bool:
    return tok.startswith("$") or "$(" in tok or "`" in tok or tok.startswith("%")


def _abs(base: Optional[str], tok: str) -> str:
    from coordinator_core.bash_guards._write_bump_sink_shapes import translate_msys_path

    tok = os.path.expanduser(translate_msys_path(tok) or tok)
    return tok if os.path.isabs(tok) else os.path.join(base or os.getcwd(), tok)


def _cd_target(cwd: Optional[str], args: List[str]) -> Tuple[Optional[str], bool]:
    """(directory after `cd <args>`, hidden). Hidden when the target is not one literal path."""
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 1 or positional[0] == "~":
        return cwd, bool(positional) and positional[0] != "~"
    if _hidden(positional[0]):
        return cwd, True
    return _abs(cwd, positional[0]), False


def _is_write_verb(verb: str, args: List[str]) -> bool:
    if verb in _ALWAYS_WRITE:
        return True
    flags = {a for a in args if a.startswith("-")}
    positional = [a for a in args if not a.startswith("-")]
    if verb == "stash":
        return not positional or positional[0] not in _STASH_READ
    if verb == "worktree":
        return bool(positional) and positional[0] == "add"
    if verb == "branch":
        if flags & _BRANCH_MUTATING:
            return True
        return bool(positional) and not (flags & _BRANCH_LISTING)
    if verb == "tag":
        if flags & _TAG_MUTATING:
            return True
        return bool(positional) and not (flags & _TAG_LISTING)
    if verb == "checkout":
        return bool(flags & {"-b", "-B", "--orphan"})
    if verb == "switch":
        return bool(flags & {"-c", "-C", "--create", "--force-create", "--orphan"})
    return False


def _positionals(args: List[str], with_arg: frozenset) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i + 1 :])
            break
        if a.startswith("-"):
            i += 2 if a in with_arg else 1
            continue
        out.append(a)
        i += 1
    return out


def _opt_values(args: List[str], names: Tuple[str, ...]) -> List[str]:
    vals: List[str] = []
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                vals.append(args[i + 1])
            elif a.startswith(n + "="):
                vals.append(a[len(n) + 1 :])
    return vals


def _clone_dest(args: List[str]) -> Optional[str]:
    pos = _positionals(args, _CLONE_OPT_WITH_ARG)
    if len(pos) >= 2:
        return pos[1]
    if pos:
        base = re.split(r"[/\\:]", pos[0].rstrip("/\\"))[-1]
        return base[:-4] if base.endswith(".git") else base
    return None


def _carried_assignments(raw_tokens: List[str]) -> Optional[List[str]]:
    """GIT_DIR / GIT_WORK_TREE values a statement leaves set for later statements.

    Applies to `export A=b ...` and a bare `A=b` statement; None for any other statement.
    Never unset: a later `unset` keeps the earlier value, which is the deny-safe answer.
    """
    toks = list(raw_tokens)
    if toks[:1] == ["export"]:
        toks = toks[1:]
    elif not toks:
        return None
    assigns = [t for t in toks if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
    if not assigns or len(assigns) != len([t for t in toks if not t.startswith("-")]):
        return None
    out: List[str] = []
    for t in assigns:
        m = _ENV_ASSIGN_RE.match(t)
        if m and m.group(2):
            out.append(m.group(2))
    return out


def _git_write(
    tokens: List[str], raw_tokens: List[str], cwd: Optional[str], carried: Optional[List[str]] = None
) -> Optional[Tuple[str, List[str], bool]]:
    """(verb, directories the write lands in, a directory was hidden) for a git write, else None."""
    if not tokens or not token_matches_binary(tokens[0], "git"):
        return None
    eff = cwd or os.getcwd()
    extra: List[str] = list(carried or [])
    hidden = False
    for t in raw_tokens:
        m = _ENV_ASSIGN_RE.match(t)
        if not m:
            break
        extra.append(m.group(2))
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        t = tokens[i]
        if t == "-C" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if _hidden(nxt):
                hidden = True
            else:
                eff = _abs(eff, nxt)
            i += 2
            continue
        if t in _GLOBAL_DIR_OPTS and i + 1 < len(tokens):
            extra.append(tokens[i + 1])
            i += 2
            continue
        for n in _GLOBAL_DIR_OPTS:
            if t.startswith(n + "="):
                extra.append(t[len(n) + 1 :])
        i += 2 if t in _GLOBAL_OPT_WITH_ARG else 1
    if i >= len(tokens):
        return None
    verb = tokens[i]
    args = tokens[i + 1 :]
    if _hidden(verb):
        return f"git {verb}", [eff], True
    if not _is_write_verb(verb, args):
        return None
    dirs: List[str] = [eff]
    if verb in ("init", "clone", "worktree"):
        dest: Optional[str] = None
        if verb == "init":
            pos = _positionals(args, _INIT_OPT_WITH_ARG)
            dest = pos[0] if pos else None
        elif verb == "clone":
            dest = _clone_dest(args)
        else:
            pos = _positionals(args[1:], _WORKTREE_OPT_WITH_ARG)
            dest = pos[0] if pos else None
        extra.extend(_opt_values(args, ("--separate-git-dir",)))
        if dest is not None:
            if verb != "worktree":
                dirs = []
            if _hidden(dest):
                hidden = True
            else:
                dirs.append(_abs(eff, dest))
        elif verb == "clone":
            dirs = []
    for e in extra:
        if _hidden(e):
            hidden = True
        else:
            dirs.append(_abs(eff, e))
    return f"git {verb}", dirs, hidden


class _Scope:
    """cwd, carried GIT_DIR/GIT_WORK_TREE values and hidden-cd flag in force at one subshell level."""

    __slots__ = ("cwd", "carried", "cd_hidden")

    def __init__(self, cwd: Optional[str], carried: List[str], cd_hidden: bool) -> None:
        self.cwd = cwd
        self.carried = carried
        self.cd_hidden = cd_hidden

    def child(self) -> "_Scope":
        return _Scope(self.cwd, list(self.carried), self.cd_hidden)


def _paren_shape(
    masked: str, tokens: List[str], raw_tokens: List[str]
) -> Tuple[int, int, List[str], List[str]]:
    """(subshells opened, subshells closed, tokens, raw tokens) with the grouping parens removed.

    The tokenizer leaves a statement's `(` on its first raw token and `)` on its last token.
    `masked` is the command with quoted spans blanked: a paren the command quotes (`-m 'x)'`)
    is prose, so it counts only where the unquoted text carries it too.
    """
    toks, raws = list(tokens), list(raw_tokens)
    opens = 0
    if raws and raws[0].startswith("("):
        stripped = raws[0].lstrip("(")
        if ("(" + stripped[:3]) in masked:
            opens = len(raws[0]) - len(stripped)
            raws[0] = stripped
            if not stripped:
                raws.pop(0)
    closes = 0
    if toks and toks[-1].endswith(")"):
        stripped = toks[-1].rstrip(")")
        if (stripped[-3:] + ")") not in masked:
            stripped = toks[-1]
        closes = len(toks[-1]) - len(stripped)
    if closes:
        toks[-1] = stripped
        if not stripped:
            toks.pop()
        if raws and raws[-1].endswith(")"):
            raws[-1] = raws[-1].rstrip(")")
            if not raws[-1]:
                raws.pop()
    return opens, closes, toks, raws


def _offence(cmd: str, payload_cwd: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """(what, workspace key) for the first git write landing in a registered workspace."""
    from coordinator_core.machine_resolver import (
        external_workspace_containing,
        external_workspace_registry_readable,
    )

    cwd = payload_cwd if isinstance(payload_cwd, str) and payload_cwd else None
    stack: List[_Scope] = [_Scope(cwd, [], False)]
    sub_base: Optional[int] = None
    after_substitution = False
    masked = _QUOTED_RE.sub(lambda m: "\0" * len(m.group(0)), cmd)
    for resolved in resolve_command_positions(cmd, preserve_windows_backslashes=True):
        if resolved.depth > 0 and sub_base is None:
            sub_base = len(stack)
            stack.append(stack[-1].child())
        elif resolved.depth == 0 and sub_base is not None:
            del stack[sub_base:]
            sub_base = None
            after_substitution = True
        opens, closes, tokens, raw_tokens = _paren_shape(masked, resolved.tokens, resolved.raw_tokens)
        for _ in range(opens):
            stack.append(stack[-1].child())
        scope = stack[-1]
        was_after, after_substitution = after_substitution, False
        try:
            if tokens[:1] == ["cd"]:
                scope.cwd, h = _cd_target(scope.cwd, tokens[1:])
                # The tokenizer splices a `$( )` result into the cd argument, so a cd that
                # directly follows its own substitution has a hidden target.
                scope.cd_hidden = scope.cd_hidden or h or was_after
                continue
            exported = _carried_assignments(raw_tokens) if raw_tokens else None
            if exported is not None:
                scope.carried.extend(exported)
                continue
            found = _git_write(tokens, raw_tokens, scope.cwd, scope.carried)
        finally:
            # Pop the scopes this statement closes; never below the enclosing substitution's own scope.
            floor = sub_base + 1 if resolved.depth > 0 and sub_base is not None else 1
            del stack[max(len(stack) - closes, floor) :]
        if found is None:
            continue
        cd_hidden = scope.cd_hidden
        what, dirs, hidden = found
        if not external_workspace_registry_readable():
            return what, None
        for d in dirs:
            key = external_workspace_containing(d)
            if key is not None:
                return what, key
        if hidden or cd_hidden:
            key = external_workspace_containing(payload_cwd) if payload_cwd else None
            if key is not None:
                return what, key
    return None


ANCHOR = "NO-VCS-WRITE-IN-AN-EXTERNAL-WORKSPACE"


def _deny_reason(what: str, key: Optional[str]) -> str:
    if key is None:
        return (
            f"BLOCKED: {what} while the external-workspace registry is unreadable. {ANCHOR}. "
            "Alternative: run read verbs only, or fix the machine-local registry."
        )
    return (
        f"BLOCKED: {what} in external workspace `{key}`. {ANCHOR}. "
        "Alternative: leave the change uncommitted; a human makes it via the workspace's VCS."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    try:
        offence = _offence(cmd.replace("\r", ""), payload.get("cwd"))
    except Exception:  # noqa: BLE001 -- a command that cannot be read denies only from inside a workspace
        from coordinator_core.machine_resolver import external_workspace_containing

        cwd = payload.get("cwd")
        key = external_workspace_containing(cwd) if isinstance(cwd, str) and cwd else None
        offence = ("a git command that cannot be read", key) if key else None
    if offence is None:
        return None
    return _deny_record(*offence)


def _deny_record(what: str, key: Optional[str]) -> Any:
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
        EscalationRoute,
    )

    legacy = _deny_reason(what, key)
    summary, _, _alt = legacy.partition(" Alternative: ")
    if key is None:
        member: Any = AdaptableOffer(
            template="run read verbs only, or fix the machine-local registry",
            what_to_adapt="the command to a read verb",
        )
    else:
        member = EscalationRoute(
            report_what="the uncommitted change you left in the workspace",
            authorize_what="a human making it through the workspace's own VCS",
        )
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-external-workspace-vcs-write",
        summary=summary,
        em_text=legacy,
        members=(member,),
    )
