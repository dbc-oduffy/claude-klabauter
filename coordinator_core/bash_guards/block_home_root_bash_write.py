"""coordinator_core.bash_guards.block_home_root_bash_write -- hard-deny twin of
``write_guards.block_home_root_write`` for the Bash and PowerShell surfaces.

Denies a redirect, ``tee`` or write cmdlet that CREATES a file directly in the
home dir. Targets come from the parse-only sink extractors; the predicate and
deny text live in ``write_guards._home_root``. A substring prefilter runs ahead
of tokenising and admits a command only when it can name the home dir (``~``,
``$HOME``, ``USERPROFILE`` or a home literal in native, forward-slash or MSYS
spelling) or when ``cwd`` is itself the home dir, so a relative target is still
judged. Spawns no process. Never fails closed.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional

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
    extract_write_sink_targets_for_segment,
    extract_write_sink_targets_powershell,
    translate_msys_path,
)
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.write_guards._home_root import (
    DENY_REASON,
    creates_home_root_entry,
    home_roots,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 62

_HOME_TOKENS = ("~", "$home", "${home}", "userprofile", "$env:home")

# Leading home-variable spellings, expanded to the env value before the predicate sees the target.
_HOME_VAR_RE = re.compile(
    r"^(?:\$\{?HOME\}?|\$env:HOME|\$\{?USERPROFILE\}?|\$env:USERPROFILE|%USERPROFILE%)(?=[\\/]|$)",
    re.IGNORECASE,
)


def _home_spellings() -> List[str]:
    """Casefolded forward-slash and MSYS spellings of every home root."""
    out: List[str] = []
    for root in home_roots():
        fwd = root.replace("\\", "/").lower()
        out.append(fwd)
        if len(fwd) > 2 and fwd[1] == ":" and fwd[2] == "/":
            out.append("/" + fwd[0] + fwd[2:])
    return out


def _cwd_is_home(cwd: Optional[str]) -> bool:
    if not cwd:
        return False
    native = translate_msys_path(cwd)
    if native is None:
        return False
    return os.path.normcase(os.path.normpath(native)) in home_roots()


def _prefilter(cmd: str, cwd: Optional[str]) -> bool:
    if _cwd_is_home(cwd):
        return True
    low = cmd.lower()
    if any(tok in low for tok in _HOME_TOKENS):
        return True
    flat = low.replace("\\", "/")
    return any(sp in flat for sp in _home_spellings())


def _expand_home_var(target: str, dialect: Optional[Dialect]) -> str:
    target = target.strip("\"'")
    match = _HOME_VAR_RE.match(target)
    if match:
        name = match.group(0).lower()
        key = "HOME" if "home" in name else "USERPROFILE"
        base = os.environ.get(key) or os.environ.get("HOME") or os.environ.get("USERPROFILE")
        target = (base or "~") + target[match.end():]
    if dialect is Dialect.POWERSHELL:
        target = target.replace("\\", "/")
    return target


def _bash_targets(cmd: str) -> Iterable[str]:
    for rc in resolve_command_positions(cmd, preserve_windows_backslashes=_host_is_windows()):
        if rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        yield from extract_write_sink_targets_for_segment(
            rc.tokens, normalize_executable_basename(rc.tokens[0])
        )


def _powershell_targets(cmd: str) -> Iterable[str]:
    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name="block-home-root-bash-write")
    for tokens, _pipe in segments or ():
        if tokens:
            yield from extract_write_sink_targets_powershell(tokens, tokens[0].lower())


def _creates_home_entry(cmd: str, cwd: Optional[str], dialect: Optional[Dialect]) -> bool:
    targets = _powershell_targets(cmd) if dialect is Dialect.POWERSHELL else _bash_targets(cmd)
    for raw in targets:
        if creates_home_root_entry(_expand_home_var(raw, dialect), cwd):
            return True
    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None
        tool_input = payload.get("tool_input") or {}
        cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
        cwd = payload.get("cwd")
        if not cmd or not _prefilter(cmd, cwd):
            return None
        if not _creates_home_entry(cmd.replace("\r", ""), cwd, dialect_from_tool_name(tool_name)):
            return None
        return apply_guard_level(
            "block-home-root-bash-write",
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": DENY_REASON,
                }
            },
        )
    except Exception:
        return None
