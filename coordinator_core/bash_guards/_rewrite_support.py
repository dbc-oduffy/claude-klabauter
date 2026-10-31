"""Primitives shared by the extracted BX-16 rewrite bodies and `dispatch_checks`.

Imports run one way: `dispatch_checks`, `find_exec_rewrite`,
`multiprobe_banner_rewrite` and `commit_scope_rewrite` import from here; this
module imports from none of them. Holds the response builders, the
caller-keyed override read, the interpreter-prefix resolver (with its
cross-process cache), the git-argv subcommand walk, the redirection-token
predicates and the grep-to-Python rewrite primitives.
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


def _em_stamped(text: str) -> str:
    """`text` with the provenance stamp a `_hook_envelope` builder puts on it, for
    an `em_text` that replaces an envelope those builders produced."""
    from coordinator_core._hook_envelope import _stamp

    return _stamp(text)


def _alt_record(
    verdict_kind: str,
    guard_name: str,
    summary: str,
    members: Any,
    override_env: Optional[str] = None,
    updated_input: Optional[Dict[str, Any]] = None,
    em_text: Optional[str] = None,
    rewrite_allow: bool = True,
    *,
    report: Optional[str] = None,
    authorize: Optional[str] = None,
    reason_shaped: bool = False,
) -> Any:
    """Build an `AlternativeRecord`; an `override_env` adds the `Override` and the
    `EscalationRoute` a non-EM render of that record requires. `report`/`authorize`
    name that route explicitly and add it even with no `override_env`. `em_text` is
    the legacy EM string, rendered verbatim. Imported lazily so a guard that never
    speaks pays no import."""
    from coordinator_core.guard_messages.alternative_record import (
        AlternativeRecord,
        EscalationRoute,
        Override,
    )

    out = list(members)
    if (report is None) != (authorize is None):
        raise ValueError("report and authorize name one EscalationRoute: pass both or neither")
    if report is not None:
        out.append(EscalationRoute(report, authorize))
    elif override_env:
        out.append(
            EscalationRoute(
                report_what="the blocked command and the alternative you need",
                authorize_what="the original command",
            )
        )
    if override_env:
        out.append(Override(override_env, reason_shaped))
    return AlternativeRecord(
        verdict_kind,
        summary,
        tuple(out),
        guard_name,
        updated_input=updated_input,
        em_text=em_text,
        rewrite_allow=rewrite_allow,
    )


def _advisory_record(
    guard_name: str,
    msg: str,
    members: Any,
    override_env: Optional[str] = None,
    em_text: Optional[str] = None,
) -> Any:
    """Advisory record: allow with `msg` as additional context."""
    return _alt_record(
        "advisory", guard_name, msg, members, override_env, em_text=em_text
    )


def _allow_rewrite_record(
    guard_name: str,
    new_cmd: str,
    ctx: str,
    members: Any = None,
    override_env: Optional[str] = None,
    tool_input: Optional[Dict[str, Any]] = None,
    em_text: Optional[str] = None,
    rewrite_allow: bool = True,
) -> Any:
    """Rewrite record: allow with the command replaced. `members` defaults to the
    rewritten command as a `ReadyCommand`. A `tool_input` is carried over with
    only `command` replaced, since `updatedInput` replaces the whole object.
    `rewrite_allow=False` renders no `permissionDecision`, as `rewrite_input` did."""
    from coordinator_core.guard_messages.alternative_record import ReadyCommand

    updated = dict(tool_input) if tool_input else {}
    updated["command"] = new_cmd
    return _alt_record(
        "rewrite",
        guard_name,
        ctx,
        members if members else (ReadyCommand(text=new_cmd),),
        override_env,
        updated_input=updated,
        em_text=em_text,
        rewrite_allow=rewrite_allow,
    )


def _deny_record(
    guard_name: str,
    msg: str,
    members: Any,
    override_env: Optional[str] = None,
    em_text: Optional[str] = None,
) -> Any:
    """Record-returning deny; `members` must hold a non-`Override` offer."""
    return _alt_record("deny", guard_name, msg, members, override_env, em_text=em_text)


def _offer(template: str, adapt: str) -> Any:
    """An `AdaptableOffer` member."""
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    return AdaptableOffer(template, adapt)


def _ready(text: str) -> Any:
    """A `ReadyCommand` member."""
    from coordinator_core.guard_messages.alternative_record import ReadyCommand

    return ReadyCommand(text=text)


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
    from coordinator_core.temp_layout import fleet_temp_root

    try:
        repo_root = Path(__file__).resolve().parents[2]
        state_dir = repo_root / "state" / "cache"
        state_dir.mkdir(parents=True, exist_ok=True)
        return str(state_dir / "bt-python3-invocation-cache.json")
    except OSError:
        return os.path.join(
            str(fleet_temp_root()), "coordinator-bt-python3-invocation-cache.json"
        )


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
_BT_INTERPRETER_RENDERING_VERSION = 3


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
    import ctypes

    try:
        home = None
        if os.name == "nt":
            # Process-token profile directory: HOME/USERPROFILE redirection
            # does not move it. In-process, no spawn.
            from ctypes import wintypes

            k32, userenv, adv = ctypes.WinDLL("kernel32"), ctypes.WinDLL("userenv"), ctypes.WinDLL("advapi32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            userenv.GetUserProfileDirectoryW.argtypes = [
                wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            tok = wintypes.HANDLE()
            if adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):
                size = wintypes.DWORD(0)
                userenv.GetUserProfileDirectoryW(tok, None, ctypes.byref(size))
                buf = ctypes.create_unicode_buffer(size.value or 1)
                if userenv.GetUserProfileDirectoryW(tok, buf, ctypes.byref(size)):
                    home = buf.value
                k32.CloseHandle(tok)
        else:
            import pwd

            home = pwd.getpwuid(os.getuid()).pw_dir
        # The env home still counts (a redirected one carries no real username);
        # the shallowest relative path across both homes wins.
        rels = []
        for h in (home, os.path.expanduser("~")):
            if not h:
                continue
            try:
                r = os.path.relpath(python_bin, h)
            except ValueError:
                continue
            if not (r.startswith(os.pardir) or os.path.isabs(r)):
                rels.append(r)
    # ArgumentError: a ctypes signature mismatch; KeyError: no passwd entry for the uid.
    except (OSError, KeyError, ctypes.ArgumentError):
        return shlex.quote(python_bin)
    if not rels:
        return shlex.quote(python_bin)
    rel = min(rels, key=len)
    return '"$HOME/%s"' % rel.replace(os.sep, "/").replace("\\", "/")


def _bt_ps_python_c(script: str) -> str:
    """``<interpreter> -c <script>`` as one PowerShell command: a quoted
    interpreter path needs the ``&`` call operator, and a single quote in the
    script is doubled, not bash-escaped."""
    py = _bt_python3_invocation()
    prefix = "& " + py if py[:1] in ("'", '"') else py
    return "%s -c '%s'" % (prefix, script.replace("'", "''"))


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


#: Task 4 fix (2026-07-29, live incident): a shell redirection operator
#: token (`2>`, `>`, `>>`, `<`, `<<`, and their glued-target spelling
#: `2>/dev/null`) is NOT a path/pattern operand -- before this fix, every
#: segment parser below (`_bt_parse_ls_segment`, `_bt_parse_find_census_
#: segment`, `_bt_grep_flags_and_operands`) walked its own non-flag tokens
#: and treated the LAST one as the path/target, so
#: `ls DIR 2>/dev/null | head -40` silently took `"2>/dev/null"` as the
#: directory to list, and the emitted rewrite crashed with
#: `FileNotFoundError: [Errno 2] No such file or directory: '2>/dev/null'`
#: -- an AUTO-APPLIED rewrite (`updatedInput`), not an advisory, so the
#: caller's command was silently replaced with one that meant something
#: different before it failed.
#:
#: `&`-involving forms (`2>&1`, `&>/dev/null`) are a SEPARATE, already-safe
#: case: the shared tokenizer's `punctuation_chars=";&|"` splits a bare `&`
#: out as its own chain-separator token (confirmed empirically -- `2>&1`
#: tokenizes to `['2>', '&', '1']`), which makes `_bt_segments_from_tokens_
#: with_pipe_flag` see 3+ segments instead of the 2 this rewrite requires,
#: so `check_head_tail_plumbing_rewrite`'s own segment-count guard already
#: falls through to its "longer chain" advisory for those forms -- this fix
#: does not need to (and, per the file-scope constraint, must not) touch
#: that tokenizer behavior.
#:
#: This detector matches only the OPERATOR's own leading characters (a
#: `re.match`, not `fullmatch`), so it catches both the glued form
#: (`2>/dev/null`, one token) and the separate-token form (`2>` followed by
#: `/dev/null` as its own token) -- for the separate form, seeing the bare
#: `2>` operator token anywhere in a segment is already sufficient to
#: decline the whole segment, so the target token that follows it never
#: needs its own check.
#:
#: A quoted operand that merely CONTAINS this shape (e.g. a `find -name`
#: pattern of literally `"2>foo"`) cannot be distinguished from a real
#: unquoted redirection once the shared tokenizer has stripped quoting --
#: this is a coverage loss (a rewrite that could have been offered isn't),
#: never a correctness risk, and is the same "prefer not rewriting over
#: rewriting approximately" bias this whole fix exists to apply.
_BT_REDIRECTION_TOKEN_RE = re.compile(r"^(?:\d*(?:>>|<<|>|<)|&>>?)")


def _bt_is_redirection_token(tok: str) -> bool:
    """`True` if `tok` alone looks like a shell redirection operator
    (`_BT_REDIRECTION_TOKEN_RE`) -- the single per-token predicate behind
    both `_bt_has_redirection` (segment-level "does this contain one at
    all") and `_bt_commit_operand_scan` (token-level "is THIS surviving
    token a redirection, not a git pathspec operand"). Factored out so
    there remains exactly one definition of "is a redirection token" in
    this module, per the 2026-08-05 cockpit incident fix: a surviving
    redirection token (`<<'MSG'`, `>/dev/null`, `2>&1`, ...) was being read
    as a git commit pathspec operand, which made an UNSCOPED `git commit`
    carrying any redirection look explicitly scoped and silenced the
    sweep-commit advisory/deny entirely."""
    return bool(_BT_REDIRECTION_TOKEN_RE.match(tok))


def _bt_has_redirection(tokens: List[str]) -> bool:
    """`True` if any token in `tokens` looks like a shell redirection
    operator (`_bt_is_redirection_token`) -- shared by every BX-16
    head/tail-upstream segment parser (`_bt_parse_ls_segment`,
    `_bt_parse_find_census_segment`, `_bt_grep_flags_and_operands`) so a
    redirection-bearing segment is declined identically everywhere, rather
    than three independently-hand-derived checks drifting apart."""
    return any(_bt_is_redirection_token(tok) for tok in tokens)


_GREP_FAMILY_BINARIES_BT = ("grep", "egrep", "fgrep", "rg")

#: grep-family short flags this rewrite considers "substitutable residue"
#: (BX-16 body's own phrase) -- a plain recursive/case-insensitive/count/
#: filenames-only/line-numbered/extended-regex/fixed-string search. A long
#: option or an unrecognized short flag (context lines, invert-match, etc.)
#: falls through to `None` -- that residue is BX-6's to police, not this
#: row's to guess a translation for.
#:
#: `E`/`F` (uppercase ONLY, matching real grep's own spelling of
#: `-E`/`-F`) are recognized here only to steer dialect selection below
#: (`_bt_grep_dialect`) -- they do not themselves change the generated
#: Python source the way `i`/`l`/`c` do. Deliberately NOT lowercase `e`/`f`:
#: real grep's own lowercase `-e PATTERN` (specify a pattern operand) and
#: `-f FILE` (read patterns from a file) are unrelated flags that TAKE AN
#: ARGUMENT -- treating them as dialect flags here would silently consume a
#: real grep operand as if it were a bare boolean switch. Membership is
#: checked case-sensitively BEFORE `ch.lower()` folds the recognized
#: uppercase `E`/`F` into the lowercase `"e"`/`"f"` dialect-flag entries
#: `_bt_grep_dialect` reads.
#:
#: Review: code-reviewer (Finding 4) -- `w` (whole-word match) is
#: deliberately NOT in this set. Neither `_bt_grep_python_rewrite` nor
#: `_bt_build_generator_lines`'s `"grep"` kind ever wraps the translated
#: pattern in a word-boundary (`\b...\b`), so accepting `-w` here while
#: silently dropping its semantics would make `grep -w foo` over-match
#: (`foobar` too) with no error signal -- exactly the "confident wrong
#: answer" class this row exists to refuse rather than guess at. Per this
#: file's own convention (refuse rather than guess), an unrecognized `-w`
#: now falls through to the advisory/refuse path like any other
#: untranslated flag instead of being silently accepted and dropped.
_GREP_SUBSTITUTABLE_SHORT_FLAGS = frozenset({"r", "R", "n", "i", "l", "c", "E", "F"})

#: A POSIX bracket-expression class/collating-symbol/equivalence-class
#: (`[[:alpha:]]`, `[.ch.]`, `[=a=]`) has no Python `re` equivalent at all --
#: any pattern containing one is refused outright, in every dialect.
_GREP_POSIX_BRACKET_CLASS_RE = re.compile(r"\[[:.=][a-zA-Z]*[:.=]\]")

#: Backslash-escapes that mean the identical literal character in BOTH a
#: POSIX ERE (egrep/`grep -E`/ripgrep's default dialect) and Python `re`.
#: Any OTHER escape (`\+`, `\d`, `\<`, ...) is dialect-ambiguous or a
#: Perl/Python-only shorthand absent from POSIX ERE -- refused, not guessed.
_GREP_EXTENDED_SAFE_ESCAPES = frozenset(".*^$[](){}?+|\\")

#: Same idea for POSIX BRE (bare `grep`, no `-E`/`-F`) -- BRE's own
#: metacharacter set is narrower (`+ ? ( ) { } |` are GNU *extensions* whose
#: escaped-vs-bare meaning flips relative to Python `re`; see
#: `_bt_grep_pattern_safe_for_dialect`'s own `dialect == "basic"` branch,
#: which refuses bare occurrences of that set directly rather than via this
#: escape whitelist).
_GREP_BASIC_SAFE_ESCAPES = frozenset(".*^$[]\\")


def _bt_grep_dialect(binary_token: str, flags: set) -> Optional[str]:
    """Classify a grep-family invocation's REGEX DIALECT -- "fixed"
    (`-F`/`fgrep`: no metacharacters at all), "extended" (`-E`/`egrep`, and
    ripgrep's own default -- Rust regex is ERE/Perl-like, not POSIX BRE), or
    "basic" (bare `grep`, POSIX BRE, the founding-incident dialect: `\\|`
    `\\+` `\\?` `\\{ \\}` are GNU-extension operators, and bare
    `| + ? { }` are ordinary literal characters -- the OPPOSITE of Python
    `re`, which is why compiling a BRE pattern as-is silently inverted this
    dispatch's own regression case, see the module-level negative-spec note
    on `check_grep_via_bash_rewrite`).

    Returns `None` for the `-E -F`/`-F -E` combination -- real grep lets the
    LAST flag on the command line win, and this function has no ordering
    information (`flags` is an unordered set), so guessing which one "wins"
    would be exactly the guess this row's whole convention refuses to make.
    """
    has_f = "f" in flags
    has_e = "e" in flags
    if has_f and has_e:
        return None
    if has_f or _bt_token_matches_binary(binary_token, "fgrep"):
        return "fixed"
    if has_e or _bt_token_matches_binary(binary_token, "egrep"):
        return "extended"
    if _bt_token_matches_binary(binary_token, "rg"):
        return "extended"
    return "basic"


def _bt_grep_pattern_safe_for_dialect(pattern: str, dialect: str, safe_escapes: frozenset) -> bool:
    """Bracket-aware scan over `pattern`: `True` only if every construct in
    it means the IDENTICAL thing under `dialect` and under Python `re`.
    Refuses (returns `False`) rather than guesses at anything ambiguous:

    - a POSIX bracket-expression class anywhere in the pattern (checked by
      the caller via `_GREP_POSIX_BRACKET_CLASS_RE` before this scan even
      starts -- not re-checked here);
    - a backslash appearing INSIDE a `[...]` bracket expression -- POSIX
      bracket expressions have no escape mechanism at all (backslash is an
      ordinary bracket member), while Python `re` treats it as an escape
      inside a character class too (`[\\.]` means two different things);
    - an escape of any character not in `safe_escapes` (a dialect-specific
      GNU/Perl shorthand like `\\d`, `\\w`, `\\<`, or a BRE/ERE-extension
      operator whose escaped-vs-bare meaning is dialect-dependent);
    - (basic dialect only) a BARE occurrence of `+ ? ( ) { } |` outside a
      bracket expression -- literal in BRE, a metacharacter in Python `re`;
    - (basic dialect only) a BARE `^` anywhere except the pattern's very
      first character, or a bare `$` anywhere except its very last
      character -- Review: code-reviewer (Finding 5) -- POSIX BRE treats
      `^`/`$` as anchors ONLY at those positions and as ordinary literal
      characters everywhere else, while Python `re` treats a bare `^`/`$`
      as an anchor UNCONDITIONALLY regardless of position. `grep -n 'a^b'`/
      `grep -n 'a$b'` (BRE: the literal substrings `a^b`/`a$b`) used to be
      classified "safe" and compiled unchanged under Python `re`, producing
      a pattern that can never match anything -- the identical silent-
      dialect-conflation failure this function already refuses for BRE
      alternation, just on the anchor-literalness axis instead.
    """
    in_bracket = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if in_bracket:
            if c == "\\":
                return False
            if c == "]":
                in_bracket = False
            i += 1
            continue
        if c == "\\":
            if i + 1 >= n or pattern[i + 1] not in safe_escapes:
                return False
            i += 2
            continue
        if dialect == "basic":
            if c in "+?(){}|":
                return False
            if c == "^" and i != 0:
                return False
            if c == "$" and i != n - 1:
                return False
        if c == "[":
            in_bracket = True
        i += 1
    return True


def _bt_grep_translate_pattern(pattern: str, dialect: str) -> Optional[str]:
    """Return a Python `re`-equivalent SOURCE STRING for `pattern` under
    `dialect`, or `None` if no translation is provably faithful -- this
    module's "refuse rather than guess" convention (see the `find -exec`
    unknown-verb case, above), applied to the regex-dialect axis instead of
    the exec'd-verb axis.

    `dialect == "fixed"` (`-F`/`fgrep`) is unconditionally translatable via
    `re.escape` -- there are no metacharacters to misinterpret when the
    ENTIRE pattern is a literal string, which is exactly what makes this the
    one dialect this function never has to refuse.
    """
    if dialect == "fixed":
        return re.escape(pattern)
    if _GREP_POSIX_BRACKET_CLASS_RE.search(pattern):
        return None
    safe_escapes = _GREP_EXTENDED_SAFE_ESCAPES if dialect == "extended" else _GREP_BASIC_SAFE_ESCAPES
    if not _bt_grep_pattern_safe_for_dialect(pattern, dialect, safe_escapes):
        return None
    return pattern


def _bt_grep_flags_and_operands(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """`tokens` is one already-tokenized segment whose first token is a
    grep-family binary. Returns
    ``{"flags": {...short flag chars...}, "pattern": str, "targets": [...]}``,
    or `None` if a long option (`--foo`) or an unrecognized short flag makes
    this non-substitutable, OR if the pattern's own regex dialect
    (`_bt_grep_dialect`) cannot be PROVABLY translated into the Python `re`
    syntax the generated rewrite compiles it with (`_bt_grep_translate_
    pattern`) -- the returned `"pattern"` is already the translated Python
    `re` source, not the raw operand, so every caller of this function
    (`_bt_grep_python_rewrite`, `_bt_build_generator_lines`'s `"grep"` kind)
    gets dialect-safety for free without needing its own translation step.

    Negative-spec: this function used to hand the RAW grep-family pattern
    straight through to `json.dumps(...)` in both callers, which then
    compiled it as Python `re` unconditionally -- silently WRONG for the
    default (POSIX BRE) dialect, where `\\|`/`\\+`/`\\?`/`\\{ \\}` are
    operators and bare `| + ? { }` are literals, the OPPOSITE of Python
    `re`. Hit live: `grep -n "^| AC-3 \\|^| AC-4 \\|^| AC-5 " <file>`
    (BRE alternation) silently became a single-character-class-free
    catch-some-lines-none pattern under Python `re`'s reading and matched
    every line of a 4000-line file instead of six.

    Also declines (returns `None`) on a shell redirection operator
    (`_bt_has_redirection`) among the operand tokens -- see that helper's
    own docstring for the live incident this guards against (a redirection
    token silently taken as a grep target/pattern instead of being
    recognized as shell syntax). Shared by both callers of this function
    (`check_grep_via_bash_rewrite`'s standalone-grep rewrite and
    `_bt_build_generator_lines`'s `"grep"` kind via `check_head_tail_
    plumbing_rewrite`), so both get the same protection from one fix.
    """
    if _bt_has_redirection(tokens[1:]):
        return None
    flags: set = set()
    operands: List[str] = []
    for tok in tokens[1:]:
        if tok.startswith("--"):
            return None
        if tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch not in _GREP_SUBSTITUTABLE_SHORT_FLAGS:
                    return None
                flags.add(ch.lower())
            continue
        operands.append(tok)
    if not operands:
        return None
    pattern, targets = operands[0], operands[1:]
    dialect = _bt_grep_dialect(tokens[0], flags)
    if dialect is None:
        return None
    translated = _bt_grep_translate_pattern(pattern, dialect)
    if translated is None:
        return None
    return {"flags": flags, "pattern": translated, "targets": [_bt_native_path(t) for t in targets] or ["."]}


def _bt_native_path(target: str) -> str:
    """Git Bash spells drive paths `/e/x`; the rewrite runs Windows Python, where `/e/x` names
    nothing and `os.walk` yields nothing -- a silent empty answer, not grep's error. Translate
    the MSYS drive prefix to its `<drive>:/` form on Windows; elsewhere `/e/x` is a real POSIX path."""
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(/|$)", target)
        if m:
            return "%s:/%s" % (m.group(1).upper(), target[m.end():])
    return target


def _bt_grep_python_rewrite(parsed: Dict[str, Any]) -> str:
    """Translate a parsed grep-family invocation into a single `python3 -c`
    `os.walk`/`re` one-liner. Honors `-i` (case-insensitive), `-l`
    (filenames only), `-c` (count only); the default output shape mirrors
    `grep -n` (`path:lineno:line`)."""
    flags = parsed["flags"]
    body_lines = [
        "import os, re",
        "pat = re.compile(%s%s)"
        % (json.dumps(parsed["pattern"]), ", re.IGNORECASE" if "i" in flags else ""),
        "targets = %s" % json.dumps(parsed["targets"]),
        "for base in targets:",
        "    walk = os.walk(base) if os.path.isdir(base) else "
        '[(os.path.dirname(base) or ".", [], [os.path.basename(base)])]',
        "    for root, dirs, files in walk:",
        "        for fn in files:",
        "            p = os.path.join(root, fn)",
        "            try:",
        '                with open(p, encoding="utf-8", errors="replace") as fh:',
        "                    lines = fh.readlines()",
        "            except OSError:",
        "                continue",
        "            hits = [(i + 1, ln) for i, ln in enumerate(lines) if pat.search(ln)]",
        "            if not hits:",
        "                continue",
    ]
    if "l" in flags:
        body_lines.append("            print(p)")
    elif "c" in flags:
        body_lines.append('            print(p + ":" + str(len(hits)))')
    else:
        body_lines.append("            for lineno, ln in hits:")
        body_lines.append(
            '                print(p + ":" + str(lineno) + ":" + ln.rstrip())'
        )
    return "%s -c %s" % (_bt_python3_invocation(), shlex.quote("\n".join(body_lines)))
