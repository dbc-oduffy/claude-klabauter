"""coordinator_core.bash_guards.guard_deploy_dirty_tree -- PreToolUse(Bash,
PowerShell) deny: a deploy command does not run while the working tree holds
uncommitted edits.

`firebase deploy` (webframeworks), `vercel deploy`, `netlify deploy`,
`wrangler deploy` and an `npm run deploy` script build the WORKING TREE, so a
peer session's half-finished edits ship to production with the deploy.

Matched shapes, on the resolved command word (never the raw string, so
`echo firebase deploy` and a quoted mention pass):
  - `firebase deploy`, `netlify deploy`, `wrangler deploy|publish`
  - `vercel deploy` and `vercel --prod`
  - `npm|pnpm|yarn|bun run deploy[:variant]`, and `yarn|bun deploy`
  - the same behind `npx|pnpx|bunx`

A repo whose `coordinator.local.md` frontmatter lists a package script under
`deploy_ships_head` (inline `["deploy:hosted"]` or a block list) declares that
script ships a `git archive HEAD`; that script alone passes on a dirty tree.
Non-script shapes and unlisted scripts still deny.

Cost: no git work unless a shape matched. A match costs one
`git status --porcelain` spawn through `session_facts._dirty_paths`; no
in-process reader sees untracked files.

TRAP -- fail-open: not a git repo, a failed `git status`, or any exception
passes the command. The guard exists to catch a known hazard, not to become a
second way for a deploy to be blocked by tooling. `fail_closed=False` in
dispatch.py agrees. Only the payload's cwd repo is read; `cd other && deploy`
is checked against the cwd the session started in.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

_MAX_PATHS = 10
_PRE_FILTER = ("deploy", "publish", "vercel")
_RUNNER_WRAPPERS = ("npx", "pnpx", "bunx")
_DEPLOY_VERB_TOOLS = {
    "firebase": ("deploy",),
    "netlify": ("deploy",),
    "wrangler": ("deploy", "publish"),
}
_SCRIPT_RUNNERS = ("npm", "pnpm", "yarn", "bun")
_SHIPS_HEAD_KEY = "deploy_ships_head"


def _is_deploy_script(name: str) -> bool:
    return name == "deploy" or name.startswith("deploy:")


def _deploy_shape(tokens: List[str]) -> Optional[str]:
    """The deploy this resolved command word runs, or None."""
    if not tokens:
        return None
    if any(token_matches_binary(tokens[0], w) for w in _RUNNER_WRAPPERS):
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return _deploy_shape(rest)
    args = tokens[1:]
    for tool, verbs in _DEPLOY_VERB_TOOLS.items():
        if token_matches_binary(tokens[0], tool):
            hit = next((v for v in verbs if v in args), None)
            return f"{tool} {hit}" if hit else None
    if token_matches_binary(tokens[0], "vercel"):
        if "deploy" in args:
            return "vercel deploy"
        return "vercel --prod" if "--prod" in args else None
    for runner in _SCRIPT_RUNNERS:
        if token_matches_binary(tokens[0], runner):
            positional = [a for a in args if not a.startswith("-")]
            if positional[:1] in (["run"], ["run-script"]) and len(positional) > 1:
                script = positional[1]
                return f"{runner} run {script}" if _is_deploy_script(script) else None
            if runner in ("yarn", "bun") and positional[:1] == ["deploy"]:
                return f"{runner} deploy"
            return None
    return None


def _deploy_in(cmd: str, exempt_scripts: Sequence[str] = ()) -> Optional[str]:
    """First deploy shape in `cmd` whose package script is not in `exempt_scripts`.

    A compound command stays denied when any non-exempt deploy rides along.
    """
    for resolved in resolve_command_positions(cmd):
        found = _deploy_shape(resolved.tokens)
        if found and _script_of(found) not in exempt_scripts:
            return found
    return None


def _script_of(shape: str) -> Optional[str]:
    """The package-script name a script-runner shape runs, else None."""
    parts = shape.split(" ")
    if parts[0] not in _SCRIPT_RUNNERS:
        return None
    if len(parts) == 3 and parts[1] == "run":
        return parts[2]
    return parts[1] if len(parts) == 2 else None


def _unquote(item: str) -> str:
    return item.strip().strip("\"'")


def _declared_ships_head(cwd: str) -> List[str]:
    """`deploy_ships_head` from the `coordinator.local.md` at or above `cwd`."""
    here = Path(cwd).resolve()
    for d in (here, *here.parents):
        config = d / "coordinator.local.md"
        if config.is_file():
            break
    else:
        return []
    text = config.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    lines = (text[3:end] if end != -1 else text[3:]).splitlines()
    for i, line in enumerate(lines):
        if not line.startswith(_SHIPS_HEAD_KEY + ":"):
            continue
        value = line.partition(":")[2].split("#", 1)[0].strip()
        if value:
            return [_unquote(v) for v in value.strip("[]").split(",") if v.strip()]
        items = []
        for nxt in lines[i + 1:]:
            stripped = nxt.split("#", 1)[0].strip()
            if stripped.startswith("- "):
                items.append(_unquote(stripped[2:]))
            elif stripped:
                break
        return items
    return []


def _dirty_paths(cwd: str) -> List[str]:
    from coordinator_core.session.session_facts import _dirty_paths as _served

    result = _served(Path(cwd), untracked_files="normal")
    if result.get("degraded"):
        return []
    return sorted(result["value"]["paths"])


def _deny_reason(shape: str, paths: List[str]) -> str:
    shown = ", ".join(paths[:_MAX_PATHS])
    more = f" (+{len(paths) - _MAX_PATHS} more)" if len(paths) > _MAX_PATHS else ""
    return (
        f"BLOCKED: `{shape}` builds the working tree, which holds {len(paths)} "
        f"uncommitted path(s) that would ship: {shown}{more}. "
        "Run `git status`, then commit or clean the paths you own; leave a peer's."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not any(w in cmd for w in _PRE_FILTER):
        return None
    try:
        cmd = cmd.replace("\r", "")
        if _deploy_in(cmd) is None:
            return None
        cwd = payload.get("cwd") or os.getcwd()
        shape = _deploy_in(cmd, _declared_ships_head(cwd))
        if shape is None:
            return None
        paths = _dirty_paths(cwd)
    except Exception:  # noqa: BLE001 -- fail-open, see module TRAP
        return None
    if not paths:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(shape, paths),
        }
    }
