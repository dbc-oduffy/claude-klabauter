"""Engine-independent backstop: irreversible-external guards run in-process, with no warm engine.

`match(host_event)` returns a deny verdict when an irreversible-external bash guard refuses the
event's shell command, or when that command force-pushes main or master, else None. Guards are
imported lazily; one that raises is skipped (fails open). Nothing here imports `coordinator_core.warm` or `coordinator_core.ops`.
"""
from __future__ import annotations

import importlib
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.host_hooks.contract import HostEvent

# Pinned against deny_registry.GUARD_DENY_CLASS by tests/test_backstop.py.
GUARD_MODULES = {
    "block-push-pr-non-owned-remote": "coordinator_core.bash_guards.block_push_pr_non_owned_remote",
    "block-empty-source-push": "coordinator_core.bash_guards.block_empty_source_push",
    "block-perforce-submit": "coordinator_core.bash_guards.block_perforce_submit",
    "p4-verb-fence": "coordinator_core.bash_guards.p4_verb_fence",
}

_COMMAND_KEYS = ("command", "CommandLine", "cmd")


def _command(host_event: HostEvent) -> str:
    tool_input = host_event.tool_input or {}
    for key in _COMMAND_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _payload(host_event: HostEvent) -> Optional[Dict[str, Any]]:
    from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

    # Native patches and MCP arguments may also contain a `command` string. They
    # are data for another tool, never evidence that a shell will execute it.
    if host_event.tool_name not in (*COMMAND_TOOL_NAMES, "exec_command", "shell", "run_command"):
        return None
    command = _command(host_event)
    if not command:
        return None
    tool_name = host_event.tool_name if host_event.tool_name in COMMAND_TOOL_NAMES else COMMAND_TOOL_NAMES[0]
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "cwd": host_event.cwd,
        "session_id": host_event.session_id,
    }


def _reason(record: Any, guard: str) -> str:
    text = getattr(record, "em_text", None) or getattr(record, "summary", None)
    if text:
        return str(text)
    if isinstance(record, dict):
        return str(record.get("reason") or record.get("message") or guard)
    return "BLOCKED by %s" % guard


_MAIN_BRANCHES = ("main", "master")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n]")
_GIT_VALUE_FLAGS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")
_PUSH_VALUE_FLAGS = ("--repo", "-o", "--push-option", "--receive-pack", "--exec", "--signed")
_WRAPPERS = ("env", "command", "sudo", "time", "exec", "nohup", "nice")
_FORCE_LONG_FLAGS = ("--force", "--force-with-lease", "--force-if-includes", "--mirror")


def _current_branch(start: str) -> str:
    """The branch HEAD names for the repo containing `start`, else "". Reads files only; no git spawn."""
    if not start:
        return ""
    try:
        here = Path(start)
        for directory in (here, *here.parents):
            dot_git = directory / ".git"
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return ""
                gitdir = Path(text[len("gitdir:"):].strip())
                dot_git = gitdir if gitdir.is_absolute() else directory / gitdir
            if dot_git.is_dir():
                head = (dot_git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
                prefix = "ref: refs/heads/"
                return head[len(prefix):] if head.startswith(prefix) else ""
    except OSError:
        return ""
    return ""


def _push_segment(tokens: list) -> Optional[tuple]:
    """(git -C dir, push args) when `tokens` is a `git ... push ...` invocation, else None."""
    start = 0
    while start < len(tokens) and (tokens[start] in _WRAPPERS or ("=" in tokens[start] and not tokens[start].startswith("-"))):
        start += 1
    if start >= len(tokens) or os.path.basename(tokens[start]).lower() not in ("git", "git.exe"):
        return None
    chdir = ""
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if token in _GIT_VALUE_FLAGS:
            if token == "-C" and i + 1 < len(tokens):
                chdir = tokens[i + 1]
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return (chdir, tokens[i + 1:]) if token == "push" else None
    return None


def _force_push_to_main(command: str, cwd: str) -> bool:
    """True when a segment of `command` is a forcing `git push` whose target is main or master."""
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        found = _push_segment(tokens)
        if found is None:
            continue
        chdir, args = found
        forcing = False
        positionals = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in _PUSH_VALUE_FLAGS:
                i += 2
                continue
            if arg.startswith("--"):
                forcing = forcing or arg.startswith(_FORCE_LONG_FLAGS)
            elif arg.startswith("-"):
                forcing = forcing or "f" in arg[1:]
            else:
                positionals.append(arg)
                forcing = forcing or arg.startswith("+")
            i += 1
        if not forcing:
            continue
        everything = "--all" in args or "--mirror" in args
        refspecs = positionals[1:]
        current = ""
        targets = []
        for refspec in refspecs:
            dst = refspec.lstrip("+").rpartition(":")[2]
            if dst in ("HEAD", ""):
                current = current or _current_branch(os.path.join(cwd, chdir) if chdir else cwd)
                dst = current if dst == "HEAD" else dst
            targets.append(dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst)
        if not refspecs and not everything:
            targets.append(_current_branch(os.path.join(cwd, chdir) if chdir else cwd))
        if everything or any(t in _MAIN_BRANCHES for t in targets):
            return True
    return False


_FORCE_MAIN_REASON = (
    "BLOCKED: this 'git push' force-pushes main (--force / -f / +refspec / --force-with-lease), "
    "which rewrites shared history and can drop commits that exist only on the remote. "
    "Push main as a fast-forward."
)


def match(host_event: HostEvent) -> Optional[Dict[str, str]]:
    """The deny verdict `{"decision": "deny", "reason": ...}` for an irreversible command, else None."""
    if host_event.tool_name.startswith("mcp__"):
        try:
            module = importlib.import_module(GUARD_MODULES["block-perforce-submit"])
            record = module.check_mcp({"tool_name": host_event.tool_name,
                                       "tool_input": dict(host_event.tool_input or {})})
            if record:
                return {"decision": "deny", "reason": _reason(record, "block-perforce-submit")}
        except Exception:  # noqa: BLE001 -- same fail-open hook boundary as shell checks
            pass
        return None
    try:
        payload = _payload(host_event)
    except Exception:  # noqa: BLE001 -- the backstop never breaks the hook
        return None
    if payload is None:
        return None
    for guard, module_name in GUARD_MODULES.items():
        try:
            record = importlib.import_module(module_name).check(dict(payload))
        except Exception:  # noqa: BLE001 -- a raising guard is skipped
            continue
        if record:
            return {"decision": "deny", "reason": _reason(record, guard)}
    try:
        if _force_push_to_main(payload["tool_input"]["command"], payload.get("cwd") or ""):
            return {"decision": "deny", "reason": _FORCE_MAIN_REASON}
    except Exception:  # noqa: BLE001 -- the backstop never breaks the hook
        pass
    return None
