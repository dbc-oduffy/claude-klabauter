"""coordinator_core.bash_guards.block_unreal_engine_resave -- PreToolUse(Bash,
PowerShell) hard deny: no Unreal commandlet rewrites the installed engine.

A `-run=ResavePackages` without `-projectonly` resaves engine content too; one
such run rewrote 564 packages under the engine install and needed a launcher
Verify to repair. Denied:
  - `UnrealEditor[-Cmd] ... -run=ResavePackages` unless it carries
    `-projectonly`, or every `-package=`/`-packagefolder=` (at least one) lies
    under the `.uproject`'s directory;
  - any `-run=<commandlet>` whose arguments name a path under an engine
    install root (machine-local `unreal.engine_roots`; the editor binary's
    own path is not an argument).
Other commandlets, and resaves scoped to the project, are allowed.

No override.
"""

from __future__ import annotations

import ntpath
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

ENGINE_ROOTS_KEY = "unreal.engine_roots"
_EDITOR_BINARIES = ("UnrealEditor-Cmd", "UnrealEditor")
_SCOPE_FLAGS = ("-package=", "-packagefolder=")


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path.strip("\"'")))


def _under(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root).rstrip("\\")
    return p == r or p.startswith(r + "\\")


def _engine_roots() -> List[str]:
    from coordinator_core.machine_resolver import load_flat_registry_file, registry_dir

    try:
        reg_dir = registry_dir()
        for fname in ("registry.local.toml", "registry.toml"):
            value = load_flat_registry_file(reg_dir / fname).get(ENGINE_ROOTS_KEY)
            if value:
                return [value] if isinstance(value, str) else [str(v) for v in value]
    except Exception:  # noqa: BLE001 -- unresolvable roots leave only the resave leg
        return []
    return []


def _flag_value(arg: str, prefix: str) -> Optional[str]:
    return arg[len(prefix):] if arg.lower().startswith(prefix) else None


def _offence(tokens: List[str]) -> Optional[str]:
    if not tokens or not any(token_matches_binary(tokens[0], b) for b in _EDITOR_BINARIES):
        return None
    args = [a.strip("\"'") for a in tokens[1:]]
    commandlet = next((v for a in args if (v := _flag_value(a, "-run=")) is not None), None)
    if commandlet is None:
        return None
    for root in _engine_roots():
        for arg in args:
            candidate = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if candidate and _under(candidate, root):
                return f"-run={commandlet} names `{candidate}` under the engine install"
    if commandlet.lower() != "resavepackages":
        return None
    if any(a.lower() == "-projectonly" for a in args):
        return None
    project = next((a for a in args if a.lower().endswith(".uproject")), None)
    scopes = [v for a in args for f in _SCOPE_FLAGS if (v := _flag_value(a, f)) is not None]
    if project and scopes and all(_under(s, ntpath.dirname(project)) for s in scopes):
        return None
    return "-run=ResavePackages without -projectonly"


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if "-run=" not in cmd.lower():
        return None
    for resolved in resolve_command_positions(cmd.replace("\r", "")):
        offence = _offence(resolved.tokens)
        if offence:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"BLOCKED: {offence} rewrites installed engine content. "
                        "Add -projectonly, or scope -package=/-packagefolder= to the project."
                    ),
                }
            }
    return None
