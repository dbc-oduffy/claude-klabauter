"""Primitives shared by the extracted BX-16 rewrite bodies and `dispatch_checks`.

Imports run one way: `dispatch_checks`, `find_exec_rewrite`,
`multiprobe_banner_rewrite` and `commit_scope_rewrite` import from here; this
module imports from none of them. Holds the response builders, the
caller-keyed override read, the interpreter-prefix resolver (with its
cross-process cache) and the git-argv subcommand walk.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    token_matches_binary as _bt_token_matches_binary,
)


def _crlf_strip(s: str) -> str:
    return s.replace("\r", "") if s else s


def _advisory(msg: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": msg,
        }
    }


def _allow_rewrite(new_cmd: str, ctx: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": new_cmd},
        }
    }
    if ctx:
        out["hookSpecificOutput"]["additionalContext"] = ctx
    return out


def _override(name: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    """Inline-per-call env read -- NEVER hoist to module scope: each call site
    calls `_override("COORDINATOR_ALLOW_X", payload=...)` fresh, matching bash
    `${VAR:-0}` at that guard's own call site.

    Prefers `payload["env"]` (the caller's own resolved environment) over
    ambient `os.environ`: guard evaluation may run in a long-lived warm server
    whose environ is frozen at start and shared by every session, so an
    operator's per-session override would otherwise go silently dead. A
    payload that is absent, not a dict, or carries no `env` mapping falls back
    to `os.environ`.
    """
    env = None
    if isinstance(payload, dict):
        candidate = payload.get("env")
        if isinstance(candidate, dict):
            env = candidate
    if env is None:
        env = os.environ
    return env.get(name, "0") == "1"


#: git global options taking a SPACE-SEPARATED value, which must be consumed
#: with their operand when walking argv to the real subcommand. Kept in step
#: with the same options `dispatch_checks._GR_BASE_RE` enumerates, plus
#: `--super-prefix` (used by `_git_reset_invocation`'s prose-vs-invocation
#: walk; `_GR_BASE_RE` has no `--super-prefix` leg).
_GIT_GLOBAL_OPT_WITH_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)

#: git global options KNOWN to take no operand at all. Closed and small on
#: purpose -- anything absent resolves as "unknown shape" and fails closed.
_GIT_GLOBAL_OPT_NO_ARG_SIMPLE = frozenset(
    {
        "-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects",
        "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
        "--icase-pathspecs", "--no-optional-locks",
    }
)


def _bt_python3_invocation_cache_path() -> str:
    """On-disk location for `_bt_python3_invocation`'s cross-process cache.
    Prefers claude-klabauter's own `state/cache/` (this repo's disk-truth substrate,
    never `~/.claude` -- a plane this repo owns none of, see CLAUDE.md
    § What this repo is); falls back to the OS temp dir if `state/` cannot
    be created (read-only checkout, permissions), matching the fail-open
    discipline `_bt_python3_invocation` itself already promises."""
    import tempfile

    try:
        repo_root = Path(__file__).resolve().parents[2]
        state_dir = repo_root / "state" / "cache"
        state_dir.mkdir(parents=True, exist_ok=True)
        return str(state_dir / "bt-python3-invocation-cache.json")
    except OSError:
        return os.path.join(tempfile.gettempdir(), "coordinator-bt-python3-invocation-cache.json")


def _bt_python3_invocation_cache_key() -> Optional[List[Any]]:
    """Build the cache key this advisory's resolution actually depends on.

    Mirrors `pyresolve._machine_local_get`'s own in-process memo, which keys
    on ``(key, resolved impl path)`` rather than the lookup key alone --
    the impl path already folds in every env var that steers *which*
    `_machine_local.py` gets consulted. This cross-process cache widens that
    same idea to cover every input `resolve_python_bin(prefer_windowless=
    False)` can observe: the resolved impl path PLUS its mtime+size (so an
    edited/rebuilt `_machine_local.py` invalidates the entry even though its
    path string is unchanged), and the four env vars that steer or
    short-circuit which store is read (`MACHINE_LOCAL_IMPL`, `CLAUDE_HOME`,
    `COORDINATOR_SETTINGS_HOME`, `COORDINATOR_PYTHON`).

    Returns ``None`` (never cache) on `_machine_local_impl` import failure -- the same
    condition `_bt_python3_invocation` itself falls open to `"python3"` on."""
    try:
        from coordinator_core._claude_klabauter_root import _machine_local_impl
    except ImportError:
        return None
    impl = _machine_local_impl()
    try:
        st = os.stat(impl)
        impl_sig: Optional[List[Any]] = [st.st_mtime_ns, st.st_size]
    except OSError:
        impl_sig = None
    return [
        impl,
        impl_sig,
        os.environ.get("MACHINE_LOCAL_IMPL", ""),
        # USERPROFILE is the Windows rung, not a nicety: PowerShell and cmd.exe
        # never set HOME or CLAUDE_HOME, so a bare read degrades to "" on every
        # Windows host and two different machines hash to the same cache key.
        os.environ.get("CLAUDE_HOME", os.environ.get("USERPROFILE", "")),
        os.environ.get("COORDINATOR_SETTINGS_HOME", ""),
        os.environ.get("COORDINATOR_PYTHON", ""),
        # Rendering version. The key covers every input to WHICH interpreter
        # resolves; this covers HOW the resolved path is rendered, so a warm
        # cache never serves a string rendered by a previous scheme.
        # Bump on any change to `_bt_render_interpreter_path`.
        _BT_INTERPRETER_RENDERING_VERSION,
    ]


#: Bumped whenever `_bt_render_interpreter_path` changes shape; folded into
#: `_bt_python3_invocation_cache_key` so a warm cross-process cache cannot serve
#: a string rendered by the previous scheme.
_BT_INTERPRETER_RENDERING_VERSION = 2


def _bt_render_interpreter_path(python_bin: str) -> str:
    """Render a resolved interpreter path for an AGENT-FACING advisory, with no
    operator username in it.

    Three guards' rewrite advisories embed the resolved absolute interpreter,
    which on a stock Windows install is
    ``C:/Users/<username>/AppData/Local/Programs/Python/Python313/python.exe``
    (backslashes in the real value; written with forward slashes here so this
    docstring carries no escape sequences).
    The username enters from the box running the suite, never from a committed
    byte; what a guard puts in front of an agent must not carry it.
    The requirement is *no username in the message*; this function is the
    mechanism, and it is the ONE choke point all three advisories render through.

    NEGATIVE SPEC -- what this deliberately does NOT do: it does not fall back to
    a bare ``python3``: ``python3`` is frequently absent on the Windows hosts
    ``resolve_python_bin`` exists to serve, and an advisory that does not run is
    worse than none.

    ``$HOME`` rather than ``~`` or ``%LOCALAPPDATA%``, because the rendered string
    has to survive being pasted into either host this fleet runs:

    - ``$HOME`` is defined in Git Bash AND is an automatic variable in PowerShell,
      so one rendering covers both. ``%LOCALAPPDATA%`` expands in cmd.exe only,
      and ``~`` does not expand inside the quotes the path needs for its spaces.
    - Double quotes, not `shlex.quote`'s single quotes: single quotes would make
      ``$HOME`` literal and the advisory would not run.
    - Forward slashes, which Windows accepts throughout and which avoid the
      backslash-as-escape trap inside a double-quoted Bash string.

    An interpreter outside the user's home carries no username to remove, so it is
    returned through `shlex.quote` exactly as before."""
    try:
        home = os.path.expanduser("~")
        rel = os.path.relpath(python_bin, home)
    except (OSError, ValueError):
        # ValueError: relpath across drives on Windows -- not under home.
        return shlex.quote(python_bin)
    if rel.startswith(os.pardir) or os.path.isabs(rel):
        return shlex.quote(python_bin)
    return '"$HOME/%s"' % rel.replace(os.sep, "/").replace("\\", "/")


def _bt_python3_invocation() -> str:
    """Resolve the shell-ready interpreter prefix (e.g. ``python3``, or on a
    python.org Windows install with no `python3.exe` on PATH, ``py -3`` or an
    absolute ``python.exe`` path) for the BX-16 rewrite/advisory payloads
    below, instead of hardcoding ``python3`` -- a bare ``python3`` is
    frequently absent on stock Windows (the interpreter there is
    ``python.exe``, or the ``py``/``pyw`` launcher; a bare ``python3`` can
    also hit the WindowsApps Store-Python stub, see
    ``claude-code-platform-gotchas.md``'s "orphan AppX stub" entry).

    Reuses ``coordinator_core.pyresolve`` (the existing Windows-safe
    interpreter-resolution precedent already used for the same pin-precedence
    contract elsewhere in this package) rather than inventing a second
    resolver. ``prefer_windowless=False`` is mandatory here -- every payload
    this helper prefixes prints to stdout for the harness to read, and
    ``pythonw.exe`` (the windowless preference) silently swallows stdout (see
    that same wiki's "pythonw.exe swallows stdout/stderr" entry).

    Lazy-imported and fails open to the literal ``"python3"`` (today's
    behavior, unconditionally regenerated as an on-host verification item
    since it cannot be executed from macOS) on ANY resolution failure --
    ImportError, empty ``python_bin`` (nothing found), or
    ``PythonPinInvalid`` -- mirroring this module's existing lazy-import
    discipline: a broken resolver must never crash the whole dispatcher, only this one advisory
    rewrite's quality.

    The import and the resolution call are two SEPARATE `try`/`except`
    blocks, not one -- `PythonPinInvalid` is itself a name bound BY the
    import this function is trying to fail open around. A single combined
    `try: from ... import PythonPinInvalid, resolve_python_bin; ... except
    (ImportError, PythonPinInvalid, OSError)` has to evaluate its own except
    tuple to decide whether a raised `ImportError` matches it -- at which
    point `PythonPinInvalid` is UNBOUND, so Python raises `UnboundLocalError`
    instead of falling open to `"python3"`, contradicting this docstring's
    own "on ANY resolution failure" promise. Splitting the import into its
    own `except ImportError` (builtin name only, never unbound) guarantees
    `PythonPinInvalid` is bound by the time the second block's `except`
    clause can ever reference it.

    CROSS-PROCESS CACHE. This fires on the fleet's highest-firing advisory,
    so the resolution below is memoized to `_bt_python3_invocation_cache_
    path()` keyed by `_bt_python3_invocation_cache_key()` (see that
    function's docstring for exactly what the key covers). The read/write
    wraps the two try/except blocks below WITHOUT touching them -- a cache
    miss or any read failure falls straight through to the same live
    resolution this function has always performed, and a write failure is
    swallowed the same way: this helper's fail-open discipline is load-
    bearing and applies identically to the cache path. The write is an
    atomic replace (`os.replace` from a pid-suffixed temp file in the same
    directory), never a truncate-then-write -- at 50-70 concurrent sessions
    a torn write from a truncate is the norm, not an edge case, and a torn
    or unreadable cache file must fall through to live resolution rather
    than ever raise or return garbage.
    """
    cache_path = _bt_python3_invocation_cache_path()
    cache_key = _bt_python3_invocation_cache_key()
    if cache_key is not None:
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if isinstance(cached, dict) and cached.get("key") == cache_key:
                cached_value = cached.get("value")
                if isinstance(cached_value, str) and cached_value:
                    return cached_value
        except (OSError, ValueError):
            # Missing, torn, or unreadable cache -- fall through to live
            # resolution below, same as any other cache miss.
            pass

    try:
        from coordinator_core.pyresolve import PythonPinInvalid, resolve_python_bin
    except ImportError:
        return "python3"
    try:
        python_bin, python_args = resolve_python_bin(prefer_windowless=False)
    except (PythonPinInvalid, OSError):
        return "python3"
    if not python_bin:
        return "python3"
    result = " ".join(
        [_bt_render_interpreter_path(python_bin)]
        + [shlex.quote(tok) for tok in python_args]
    )

    if cache_key is not None:
        try:
            tmp_path = "%s.%d.tmp" % (cache_path, os.getpid())
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"key": cache_key, "value": result}, fh)
            os.replace(tmp_path, cache_path)
        except OSError:
            # Best-effort cache write -- a failure here just means the next
            # firing resolves live again, not a correctness issue.
            pass

    return result


#: BX-13 peel, reused token-wise rather than re-derived (C1a Fix 3): mirrors
#: `_GC_CLEAN_CMD_RE`'s wrapper-prefix vocabulary (`sudo`/`command`/`time`/
#: `exec`/`nice`/`nohup`/`ionice`/`timeout`/`stdbuf`/`which`/`type`, an `env`
#: token, or a bare `NAME=value` assignment) -- `_bt_git_resolved_subcommand`
#: previously required `tokens[0]` to be the git binary outright, so
#: `GIT_INDEX_FILE=/tmp/i git commit -m x` and `nice git commit -m x` were
#: never recognized as a git invocation at all and silently bypassed
#: `check_git_commit_safe_commit_advise`.
_GIT_WRAPPER_PREFIX_WORDS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup",
        "ionice", "timeout", "stdbuf", "which", "type", "env",
    }
)
# Known limitation: `\S*` cannot match an embedded space, so a value like
# `GIT_INDEX_FILE="/tmp/my index"` (a single shlex token containing a
# literal space) fails this regex and is not recognized as an env
# assignment -- a silent under-fire on that low-likelihood, space-containing
# path shape, not widened here.
_ENV_ASSIGNMENT_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _bt_peel_wrapper_prefix(tokens: List[str]) -> List[str]:
    """Drop leading env-var-assignment and wrapper-binary tokens (BX-13
    shape, see `_GIT_WRAPPER_PREFIX_WORDS`) so a caller resolving the
    git binary at position 0 sees the same command a shell would actually
    exec. Stops at the first token that is neither -- never guesses past an
    ambiguous token."""
    i = 0
    n = len(tokens)
    while i < n and (
        any(_bt_token_matches_binary(tokens[i], w) for w in _GIT_WRAPPER_PREFIX_WORDS)
        or _ENV_ASSIGNMENT_TOKEN_RE.match(tokens[i])
    ):
        i += 1
    return tokens[i:]


def _bt_git_subcommand_start_index(tokens: List[str]) -> Optional[int]:
    """Positional walk locating the git subcommand token, returning the
    index of the first token AFTER it in the ORIGINAL (unpeeled) `tokens`
    list -- so a caller that needs to keep scanning past the subcommand
    (e.g. `_bt_commit_operand_scan`) doesn't have to re-derive the
    wrapper-prefix offset itself. `None` on anything unresolvable; never
    guesses.

    Peels a leading env-var-assignment/wrapper-binary prefix (BX-13 shape,
    C1a Fix 3) before resolving the git binary -- see
    `_bt_peel_wrapper_prefix` -- and re-adds that offset to the returned
    index. `_bt_git_resolved_subcommand` is a thin wrapper over this that
    returns the subcommand token itself instead of an index."""
    peeled = _bt_peel_wrapper_prefix(tokens)
    offset = len(tokens) - len(peeled)
    if not peeled or not _bt_token_matches_binary(peeled[0], "git"):
        return None
    i = 1
    n = len(peeled)
    while i < n:
        tok = peeled[i]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            return None
        return offset + i + 1
    return None


def _bt_git_resolved_subcommand(tokens: List[str]) -> Optional[str]:
    """Positional git-subcommand walk over an already-tokenized segment
    (mirrors `_seg_resolved_git_subcommand`'s walk, operating on tokens
    directly rather than re-shlex-splitting a rejoined string -- avoids
    re-tokenizing a `-m "multi word"` operand that the caller has no need
    to re-parse). `None` on anything unresolvable; never guesses.

    Peels a leading env-var-assignment/wrapper-binary prefix (BX-13 shape,
    C1a Fix 3) before resolving the git binary -- see
    `_bt_peel_wrapper_prefix`. Thin wrapper over
    `_bt_git_subcommand_start_index`, which does the actual walk."""
    idx = _bt_git_subcommand_start_index(tokens)
    if idx is None:
        return None
    return tokens[idx - 1]
