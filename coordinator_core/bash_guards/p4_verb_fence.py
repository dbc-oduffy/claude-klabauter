"""coordinator_core.bash_guards.p4_verb_fence -- PreToolUse(Bash/PowerShell)
hard-deny guard closing D6 (one p4 verb fence, fail-closed), D7 (git
worktree-rewrite deny in a p4 workspace) and S4 (the fence is closed to
plugins) of docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md.

MARKER-GATED, so a git-only repo pays nothing beyond the flat
``coordinator.local.md`` read ``is_p4_repo`` already does (Anti-scope: "Every
Write/Edit/MultiEdit/NotebookEdit in every repo on the box pays one extra
coordinator.local.md read plus a frontmatter parse ... That price is far
below any bar, and is what 'pays nothing' means here"). ``check()`` resolves
the repo root by a ZERO-SPAWN upward filesystem walk for a
``coordinator.local.md`` file (mirrors ``guard_reap_stale_git_lock.py``'s own
``_walk_up_for_git_dir`` -- a bounded ``Path.exists()`` walk, never a ``git``
subprocess) and calls ``coordinator_core.p4.workspace.is_p4_repo`` on it --
never ``.p4config``, never ``p4 set``, never ambient ``P4*`` env (D1's own
anti-probe rule). A repo with no marker anywhere up the tree returns
``None`` (allow) immediately, before any command-text parsing runs.

ONE ALLOWLIST, module constant, cockpit's verb set verbatim (D6's own "one
allowlist ... as one list", S4's "no fragment, doc, or op can widen D6's
allowlist"). Nothing in this module reads a fragment file, a plugin
manifest, or an env var to extend it -- the constants below ARE the fence.

Covers both dialects (``p4``, ``p4.exe``, ``& p4``, ``cmd /c p4``) via
``_dialect.dialect_from_tool_name`` (never inferred from command text), and
BOTH ``git`` (D7's worktree-rewrite list) and ``attrib``/``chmod`` (the
read-only-strip deny) inside the SAME per-segment classifier, since a single
Bash/PowerShell call can chain any of the three (`p4 edit -c 41 -- foo &&
git checkout -- foo`).

DENY set (verbatim from the C6 plan-spine row body):
  - ``submit`` -- denied outright, distinct message ("no submit provider
    installed" -- D8: submit is example-game-repo's, this engine builds no gate).
  - Anything unparseable: a ``p4`` invocation carrying ``-x`` (p4's own
    "read args from a file" global flag, which makes the real verb
    unrecoverable from argv), ``P4ALIASES`` anywhere in the command text,
    ``p4vc`` as a command head, ``git p4`` as a git subcommand.
  - ``attrib -r`` / ``chmod +w`` on ANY path -- both strip the read-only bit
    p4 needs to keep the workspace and the depot consistent (D4a).
  - A bare ``reconcile -n`` (no path argument) -- allowed ONLY when scoped to
    an explicit path, since an unscoped ``-n`` still walks the whole tree
    (D6's own "the read path paying it too" -- CLAUDE.md § Load norm).
  - D7's git verb list in full: ``checkout`` (paths AND branch form),
    ``switch``, ``restore``, ``reset --hard``/``--keep``/``--merge``,
    ``stash`` (incl. ``pop``/``apply``), ``rebase``, ``merge``, ``pull``,
    ``cherry-pick``, ``revert``, ``am``, ``apply``, ``clean``,
    ``submodule update``, ``sparse-checkout``, ``read-tree -u``,
    ``checkout-index``, ``bisect``. ``reset --mixed``/``--soft`` stay
    ALLOWED -- refs and index only, never the worktree (D7's own carve-out).

D4b's own gain: ``reopen`` is a p4 session-CL write verb, allowed only as
``reopen -c <CL> <paths>`` (form only -- like ``edit``/``add``/``delete``/
``move`` above, neither this fence nor those verbs validate the CL NUMBER
against the session's own) -- moves D4b's own orphan-adoption remedy INTO
whatever CL is named. Denied bare or with no path argument. Without this
addition the fence would deny the floor's own remedy.

Deny, never ask -- every returned envelope is a hard
``permissionDecision: "deny"``, under every permission mode
(``bypassPermissions`` included, per D6's own "the verdict is deny, never
ask, under every permission mode").

NEGATIVE SPEC -- no override. Unlike ``bump_foreign_repo_write.py``'s
``COORDINATOR_OVERRIDE_*`` escape hatches, this module reads no env-var
override at all: S4 ("the fence is closed to plugins... not even 'for
Example-game-repo'") is a caller-discrimination rule as much as a widening rule, and a
subagent-settable env var would be exactly the caller-reachable bypass S4
exists to close.

Zero spawns on every path -- this module imports no subprocess machinery,
never imports ``coordinator_core.p4.runner`` (the one p4 spawn helper), and
its own ``check()`` never shells out. ``tests/test_p4_verb_fence.py`` asserts
this with a runner spy.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
plan-spine row C6 (D6, D7, S4).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.p4.workspace import is_p4_repo
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _real_git_subcommand,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    _strip_ps_quotes,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
    strip_powershell_prose_noise,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

#: own ``_MAX_UPWARD_WALK`` -- a plain filesystem bound, not a git-tree
_MAX_UPWARD_WALK = 50

_P4_BASENAMES = frozenset({"p4", "p4.exe"})

#: own "anything unparseable ... denied" posture (see `_GOVERNED_MENTION_RE`
_SHELL_DASH_C_BASENAMES = frozenset({"bash", "sh", "pwsh", "powershell"})
_SHELL_DASH_C_FLAGS = frozenset({"-c", "-command"})

_P4_SIMPLE_READ_VERBS = frozenset(
    {
        "info", "opened", "diff", "describe", "changes", "files", "fstat",
        "filelog", "annotate", "print", "where", "have", "status",
        "streams", "istat", "cstat", "interchanges", "help",
    }
)

_P4_ADD_LIKE_VERBS = frozenset({"edit", "add", "delete", "move"})

#: ``_P4_ADD_LIKE_VERBS`` -- neither this fence nor those verbs validate the
_P4_REOPEN_VERB = "reopen"

#: ``-x`` is deliberately EXCLUDED here -- it does not merely take a value,
_P4_GLOBAL_FLAGS_WITH_VALUE = frozenset(
    {"-p", "-u", "-c", "-d", "-H", "-C", "-I", "-Q", "-L", "-z", "-Z", "-s", "-F"}
)
_P4_GLOBAL_FLAGS_NO_VALUE = frozenset({"-G"})

_GIT_DENY_VERBS = frozenset(
    {
        "checkout", "switch", "restore", "stash", "rebase", "merge", "pull",
        "cherry-pick", "revert", "am", "apply", "clean", "bisect",
    }
)

_GIT_RESET_DENY_FLAGS = ("--hard", "--keep", "--merge")

#: UNPARSEABLE command when it plausibly names a surface this fence governs
#: D6's own "-x, P4ALIASES, p4vc, git p4" unparseable examples AND the
_GOVERNED_MENTION_RE = re.compile(r"(?i)\bp4(\.exe)?\b|p4vc|P4ALIASES|\bgit\b|\battrib\b|\bchmod\b")

#: D6: "P4ALIASES anywhere in the command text" denies outright -- a caller
_P4ALIASES_RE = re.compile(r"P4ALIASES")

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _find_repo_root_no_spawn(start_cwd: str) -> Optional[str]:
    """Zero-subprocess upward walk from ``start_cwd`` looking for an
    enclosing ``coordinator.local.md`` -- a plain ``Path.exists()`` at each
    hop, never a ``git rev-parse`` subprocess. Bounded by
    ``_MAX_UPWARD_WALK`` and the filesystem root; returns ``None`` if
    neither is reached with a hit."""
    if not start_cwd:
        return None
    current = Path(start_cwd)
    for _ in range(_MAX_UPWARD_WALK):
        if (current / "coordinator.local.md").is_file():
            return str(current)
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _is_p4_gated(cwd: str) -> bool:
    repo_root = _find_repo_root_no_spawn(cwd)
    if repo_root is None:
        return False
    return is_p4_repo(repo_root)


def _skip_env_and_call_operator(tokens: List[str]) -> List[str]:
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "&":
            i += 1
            continue
        if _ENV_ASSIGNMENT_RE.match(tok):
            i += 1
            continue
        break
    return tokens[i:]


def _p4_verb_and_args(rest: List[str]) -> Optional[Tuple[str, List[str]]]:
    i = 0
    n = len(rest)
    while i < n:
        tok = rest[i]
        if tok == "-x":
            return None
        if tok in _P4_GLOBAL_FLAGS_NO_VALUE:
            i += 1
            continue
        if tok in _P4_GLOBAL_FLAGS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            return None
        break
    if i >= n:
        return None
    return rest[i], rest[i + 1:]


def _classify_p4_segment(rest: List[str]) -> Optional[str]:
    resolved = _p4_verb_and_args(rest)
    if resolved is None:
        return "unparseable p4 invocation"
    verb, args = resolved

    if verb == "submit":
        return "submit"
    if verb in _P4_SIMPLE_READ_VERBS:
        return None
    if verb == "stream":
        return None if args[:1] == ["-o"] else "unrecognized p4 verb form (default-deny)"
    if verb == "client":
        return None if args[:1] == ["-o"] else "unrecognized p4 verb form (default-deny)"
    if verb == "login":
        return None if args[:1] == ["-s"] else "unrecognized p4 verb form (default-deny)"
    if verb == "set":
        return None if not args else "unrecognized p4 verb form (default-deny)"
    if verb == "reconcile":
        if "-n" not in args:
            return "unrecognized p4 verb form (default-deny)"
        positional = [a for a in args if a != "-n" and not a.startswith("-")]
        return None if positional else "bare `reconcile -n` (scope it to paths)"
    if verb in _P4_ADD_LIKE_VERBS:
        return None if "-c" in args else "unrecognized p4 verb form (default-deny)"
    if verb == "revert":
        return None if ("-a" in args or "-c" in args) else "unrecognized p4 verb form (default-deny)"
    if verb == "change":
        return None if (args[:1] == ["-o"] or args[:1] == ["-i"]) else "unrecognized p4 verb form (default-deny)"
    if verb == "shelve":
        return None if ("-c" in args or "-r" in args) else "unrecognized p4 verb form (default-deny)"
    if verb == _P4_REOPEN_VERB:
        if "-c" not in args:
            return "bare `reopen` (needs -c <CL> and paths)"
        idx = args.index("-c")
        if idx + 1 >= len(args):
            return "bare `reopen` (needs -c <CL> and paths)"
        remaining = args[:idx] + args[idx + 2 :]
        positional = [a for a in remaining if not a.startswith("-")]
        return None if positional else "bare `reopen` (needs -c <CL> and paths)"
    return "unrecognized p4 verb (default-deny)"


def _classify_git_segment(rest: List[str]) -> Optional[str]:
    if rest[:1] == ["p4"]:
        return "git p4 (unsupported)"

    subcmd, ambiguous, remaining = _real_git_subcommand(rest)
    if ambiguous:
        return "unparseable git invocation"
    if subcmd is None:
        return None

    if subcmd == "reset":
        for flag in _GIT_RESET_DENY_FLAGS:
            if flag in remaining:
                return "git reset %s" % flag
        return None
    if subcmd == "checkout-index":
        return "git checkout-index"
    if subcmd == "read-tree":
        return "git read-tree -u" if "-u" in remaining else None
    if subcmd == "submodule":
        return "git submodule update" if remaining[:1] == ["update"] else None
    if subcmd == "sparse-checkout":
        return "git sparse-checkout"
    if subcmd in _GIT_DENY_VERBS:
        return "git %s" % subcmd
    return None


def _classify_attrib_chmod(head_base: str, rest: List[str]) -> Optional[str]:
    if head_base == "attrib":
        return "attrib -r" if any(t.lower() == "-r" for t in rest) else None
    return "chmod +w" if any("+w" in t for t in rest) else None


def _classify_segment(tokens: List[str]) -> Optional[str]:
    working = _strip_leading_subshell_and_env(tokens)
    working = _skip_env_and_call_operator(working)
    if not working:
        return None

    head = working[0]
    head_base = _normalize_executable_basename(head)
    rest = working[1:]

    if head_base == "cmd" and rest and rest[0].lower() in ("/c", "-c"):
        inner = rest[1:]
        if not inner:
            return None
        inner_base = _normalize_executable_basename(inner[0])
        if inner_base in _P4_BASENAMES:
            return _classify_p4_segment(inner[1:])
        if inner_base == "git":
            return _classify_git_segment(inner[1:])
        return None

    # `_GOVERNED_MENTION_RE` already applies to the top-level unparseable
    if head_base in _SHELL_DASH_C_BASENAMES:
        for idx, tok in enumerate(rest):
            flag = tok.lower()
            if flag == "-encodedcommand":
                return "unparseable invocation (shell -EncodedCommand interpreter)"
            if flag in _SHELL_DASH_C_FLAGS:
                inner_text = " ".join(rest[idx + 1 :])
                if _GOVERNED_MENTION_RE.search(inner_text):
                    return "unparseable invocation (shell -c/-Command interpreter)"
                break

    if head_base in _P4_BASENAMES:
        return _classify_p4_segment(rest)
    if head_base == "git":
        return _classify_git_segment(rest)
    if head_base == "attrib":
        return _classify_attrib_chmod("attrib", rest)
    if head_base == "chmod":
        return _classify_attrib_chmod("chmod", rest)
    if head_base == "p4vc":
        return "p4vc (unsupported)"
    return None


def _evaluate_bash(cmd: str) -> Optional[str]:
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return "unparseable invocation" if _GOVERNED_MENTION_RE.search(cmd) else None
    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        verdict = _classify_segment(seg_tokens)
        if verdict is not None:
            return verdict
    return None


def _ps_normalize_token(tok: str) -> str:
    return _strip_ps_quotes(tok).replace("`", "")


def _evaluate_powershell(cmd: str) -> Optional[str]:
    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name="p4_verb_fence")
    if segments is None:
        scannable = strip_powershell_prose_noise(cmd)
        return "unparseable invocation" if _GOVERNED_MENTION_RE.search(scannable) else None
    for seg_tokens, _pipe_before in segments:
        if not seg_tokens:
            continue
        clean = [_ps_normalize_token(t) for t in seg_tokens]
        verdict = _classify_segment(clean)
        if verdict is not None:
            return verdict
    return None


def _deny_reason(deny_kind: str) -> str:
    if deny_kind == "submit":
        return (
            "BLOCKED: p4 submit -- agents do not submit. This repo mirrors "
            "Perforce for commit/shelve only; the shelved changelist is "
            "the deliverable. Report to your EM."
        )
    return (
        "BLOCKED: %s is outside this repo's p4 verb fence -- this repo "
        "mirrors Perforce (`vcs_mirror: p4`) and only cockpit's read / "
        "session-CL-write verb set is allowed here. See "
        "coordinator_core/bash_guards/p4_verb_fence.py for the allowed set."
        % deny_kind
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    if not _is_p4_gated(payload.get("cwd") or ""):
        return None

    if _P4ALIASES_RE.search(cmd):
        deny_kind: Optional[str] = "P4ALIASES override (unsupported)"
    else:
        dialect = dialect_from_tool_name(payload.get("tool_name") or "")
        if dialect is Dialect.POWERSHELL:
            deny_kind = _evaluate_powershell(cmd)
        else:
            deny_kind = _evaluate_bash(_strip_heredoc_bodies(cmd))

    if deny_kind is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(deny_kind),
        }
    }
