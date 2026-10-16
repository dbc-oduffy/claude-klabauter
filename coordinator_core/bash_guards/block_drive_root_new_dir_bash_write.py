"""coordinator_core.bash_guards.block_drive_root_new_dir_bash_write -- hard-deny guard for the
Bash and PowerShell surfaces against a command that CREATES a new top-level entry on a drive
root or directly below the home dir.

Targets come from the parse-only sink extractors plus a local supplement (PowerShell
``mkdir``/``md``/``ni``, ``New-Item -Path/-Name``, and the ``-o``/``--out`` family for absolute
values only); the predicate and deny text live in ``write_guards._drive_root``. A single regex
lifts path-shaped candidates ahead of tokenising and one ``lexists`` per candidate decides whether
the command can create anything new at all; only then is it tokenised. The pre-pass never denies
by itself. Spawns no process. Never fails closed. A shell-parsed target set cannot see writes made
inside a script, an interpreter body or ``xargs`` (DR-258): a speedbump, the Stop-time leg stays
the backstop.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    ResolutionConfidence,
    normalize_executable_basename,
    resolve_command_positions,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._write_bump_sink_shapes import (
    _host_is_windows,
    _native_path,
    extract_write_sink_targets_for_segment,
    extract_write_sink_targets_powershell,
    translate_msys_path,
)
from coordinator_core.bash_guards.block_home_root_bash_write import _expand_home_var
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.write_guards._drive_root import (
    configured_drive_roots,
    deny_reason,
    new_top_level_entry,
    override_active,
)
from coordinator_core.write_guards._home_root import home_roots

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 63

_HOME_VAR = r"\$\{?HOME\}?|\$env:HOME|\$\{?USERPROFILE\}?|\$env:USERPROFILE|%USERPROFILE%"

# Path-shaped runs: drive-absolute, leading-slash (incl. MSYS) and home-spelled.
_CAND_RE = re.compile(
    r"(?:(?<![\w])[A-Za-z]:[\\/]|(?<![^\s'\"=>])(?:/|~|" + _HOME_VAR + r"))[^\s'\"`;|&<>()]*",
    re.IGNORECASE,
)
_ROOT_SPELLING_RE = re.compile(r"^(?:[A-Za-z]:|/[A-Za-z]?|~|" + _HOME_VAR + r")$", re.IGNORECASE)
_ABSOLUTE_VALUE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|~|\$|%USERPROFILE%)", re.IGNORECASE)

# Git Bash maps these into its own install root, not a drive root.
_MSYS_MOUNT_RE = re.compile(
    r"^/(?:tmp|usr|etc|bin|dev|proc|opt|var|mingw32|mingw64|ucrt64|clang64|cygdrive)(?:/|$)"
)

_OUT_FLAGS = frozenset({"-o", "--out", "--output", "--outdir", "--out-dir", "--output-dir"})
_PS_DIR_HEADS = frozenset({"new-item", "ni", "mkdir", "md"})


def _env(payload: Dict[str, Any]) -> Optional[Mapping[str, str]]:
    env = payload.get("env")
    return env if isinstance(env, dict) else None


def _is_root_dir(path: str, env: Optional[Mapping[str, str]]) -> bool:
    nt = _native_path()
    native = translate_msys_path(path)
    if native is None:
        return False
    norm = nt.normpath(native)
    if nt.isabs(norm) and nt.dirname(norm) == norm:
        return True
    key = nt.normcase(norm)
    return key in home_roots(env) or any(
        nt.normcase(nt.normpath(r)) == key for r in configured_drive_roots(env)
    )


def _is_root_spelling(cand: str, env: Optional[Mapping[str, str]]) -> bool:
    stripped = cand.rstrip("/\\")
    if not stripped:
        return True
    if _ROOT_SPELLING_RE.match(stripped):
        return True
    return _is_root_dir(_expand_home_var(cand, None), env)


def _judge(raw: str, cwd: Optional[str], dialect: Optional[Dialect], env: Optional[Mapping[str, str]]) -> Optional[str]:
    target = _expand_home_var(raw, dialect)
    bare = not (dialect is not Dialect.POWERSHELL and _MSYS_MOUNT_RE.match(target))
    return new_top_level_entry(target, cwd, bare_slash_is_drive_root=bare, env=env)


def _bash_targets(cmd: str) -> Iterable[str]:
    for rc in resolve_command_positions(cmd, preserve_windows_backslashes=_host_is_windows()):
        if rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        tokens = rc.tokens
        yield from extract_write_sink_targets_for_segment(tokens, normalize_executable_basename(tokens[0]))
        yield from _out_flag_values(tokens)


def _powershell_targets(cmd: str) -> Iterable[str]:
    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name="block-drive-root-new-dir-bash-write")
    for tokens, _pipe in segments or ():
        if not tokens:
            continue
        head = tokens[0].lower()
        yield from extract_write_sink_targets_powershell(tokens, head)
        if head in _PS_DIR_HEADS:
            yield from _ps_dir_target(tokens[1:])
        yield from _out_flag_values(tokens)


def _out_flag_values(tokens: List[str]) -> Iterable[str]:
    """Values of `-o`/`--out` style flags, only when absolute or home-spelled."""
    for i, tok in enumerate(tokens):
        flag, eq, value = tok.partition("=")
        if eq and flag in _OUT_FLAGS:
            pass
        elif tok in _OUT_FLAGS and i + 1 < len(tokens):
            value = tokens[i + 1]
        else:
            continue
        value = value.strip("\"'")
        if _ABSOLUTE_VALUE_RE.match(value):
            yield value


def _ps_dir_target(args: List[str]) -> Iterable[str]:
    """`mkdir`/`md`/`ni`/`New-Item` path, with `-Name` joined onto `-Path`."""
    path = name = None
    positional: List[str] = []
    skip = False
    for i, tok in enumerate(args):
        if skip:
            skip = False
            continue
        low = tok.lower()
        if low.startswith("-"):
            has_value = i + 1 < len(args)
            if low.startswith("-pa") and has_value:
                path, skip = args[i + 1], True
            elif low.startswith("-na") and has_value:
                name, skip = args[i + 1], True
            elif low.startswith(("-it", "-va", "-ty")) and has_value:
                skip = True
        else:
            positional.append(tok)
    if path is None and positional:
        path = positional[0]
    path = (path or "").strip("\"'")
    if name:
        yield (path + "/" + name.strip("\"'")) if path else name.strip("\"'")
    elif path:
        yield path


def _cwd_is_root(cwd: Optional[str], env: Optional[Mapping[str, str]]) -> bool:
    return bool(cwd) and _is_root_dir(cwd, env)


def _can_create_new(cmd: str, cwd: Optional[str], dialect: Optional[Dialect], env: Optional[Mapping[str, str]]) -> bool:
    """Existence pre-pass: False only when no candidate can be new and nothing is root-relative."""
    if _cwd_is_root(cwd, env):
        return True
    for cand in _CAND_RE.findall(cmd):
        if _is_root_spelling(cand, env) or _judge(cand, cwd, dialect, env):
            return True
    return False


def _new_entry(cmd: str, cwd: Optional[str], dialect: Optional[Dialect], env: Optional[Mapping[str, str]]) -> Optional[str]:
    targets = _powershell_targets(cmd) if dialect is Dialect.POWERSHELL else _bash_targets(cmd)
    for raw in targets:
        name = _judge(raw, cwd, dialect, env)
        if name:
            return name
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
        if not cmd or override_active(payload):
            return None
        cwd = payload.get("cwd")
        env = _env(payload)
        dialect = dialect_from_tool_name(tool_name)
        cmd = cmd.replace("\r", "")
        if not _can_create_new(cmd, cwd, dialect, env):
            return None
        name = _new_entry(cmd, cwd, dialect, env)
        if not name:
            return None
        return apply_guard_level(
            "block-drive-root-new-dir-bash-write",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": deny_reason(name),
                }
            },
        )
    except Exception:
        return None
