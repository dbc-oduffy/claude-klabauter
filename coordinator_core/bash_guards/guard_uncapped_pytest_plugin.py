"""coordinator_core.bash_guards.guard_uncapped_pytest_plugin -- PreToolUse(Bash,
PowerShell) hard deny: a direct ``pytest -p <ad-hoc plugin>`` run carries no process cap.

An ad-hoc plugin can spawn pytest from inside a pytest session; with no Job Object or depth
sentinel the recursion exhausts the box. Denied: ``pytest`` / ``py.test`` / ``python -m pytest``
whose argv carries ``-p <module>`` (``-p<module>``, ``-p=<module>``) where the module is neither
``no:<x>`` nor a packaged or engine plugin. Admitted: the same args under
``python -m coordinator_core.testing.scoped_pytest`` (its head is not pytest). Pure token work,
no spawn.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    normalize_executable_basename,
    resolve_command_positions,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
GUARD_NAME = "guard-uncapped-pytest-plugin"

_PRE_FILTER = re.compile(r"pytest|py\.test", re.IGNORECASE)
_PYTHON_HEAD = re.compile(r"^(?:python[\d.]*|py)$")
_PYTEST_HEADS = frozenset({"pytest", "py.test"})
# Interpreter options that take a separate value (`python -X dev -m pytest`); the value is not the script.
_PYTHON_VALUE_OPTS = frozenset({"-X", "-W"})
# Packaged pytest11 plugins in common use; anything else named by -p is ad-hoc.
# Trap: a closed list refuses an unlisted packaged plugin; add it here when that happens.
_PACKAGED_PLUGINS = frozenset(
    {
        "xdist", "pytest_xdist", "cov", "pytest_cov", "timeout", "pytest_timeout", "pytester",
        "asyncio", "pytest_asyncio", "anyio", "mock", "pytest_mock", "randomly", "pytest_randomly",
        "rerunfailures", "pytest_rerunfailures", "benchmark", "pytest_benchmark", "hypothesis",
        "env", "pytest_env", "django", "pytest_django", "html", "pytest_html", "sugar",
        "pytest_sugar", "instafail", "pytest_instafail", "socket", "pytest_socket", "forked",
        "pytest_forked", "metadata", "pytest_metadata", "split", "pytest_split",
    }
)
_ENGINE_PLUGIN_PREFIX = "coordinator_core.testing."


def _pytest_args(toks: List[str]) -> Optional[List[str]]:
    """Args after the pytest entry of a resolved segment, or None when it is not pytest."""
    if not toks:
        return None
    head = normalize_executable_basename(toks[0])
    if head in _PYTEST_HEADS:
        return toks[1:]
    if _PYTHON_HEAD.match(head):
        skip = False
        for i, tok in enumerate(toks[1:], start=1):
            if tok == "-m":
                if i + 1 < len(toks) and normalize_executable_basename(toks[i + 1]) == "pytest":
                    return toks[i + 2:]
                return None
            if tok in _PYTHON_VALUE_OPTS:
                skip = True
                continue
            if skip:
                skip = False
                continue
            if not tok.startswith("-"):
                return None
    return None


def _adhoc_plugins(args: List[str]) -> List[str]:
    found: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            break
        name = None
        if tok == "-p":
            if i + 1 < len(args):
                name = args[i + 1]
                i += 1
        elif tok.startswith("-p=") or (tok.startswith("-p") and not tok.startswith("--") and len(tok) > 2):
            name = tok[3:] if tok.startswith("-p=") else tok[2:]
        if name:
            name = name.strip("'\"")
            if (
                name
                and not name.startswith("no:")
                and name not in _PACKAGED_PLUGINS
                and not name.startswith(_ENGINE_PLUGIN_PREFIX)
            ):
                found.append(name)
        i += 1
    return found


def _offence(cmd: str) -> Any:
    if not _PRE_FILTER.search(cmd):
        return None
    for resolved in resolve_command_positions(cmd.replace("\r", "")):
        args = _pytest_args(resolved.tokens)
        if args is None:
            continue
        plugins = _adhoc_plugins(args)
        if plugins:
            return _record(plugins[0])
    return None


def check(payload: Dict[str, Any]) -> Optional[Any]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    try:
        return _offence(cmd)
    except Exception:
        return None


def _record(plugin: str) -> Any:
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer, AlternativeRecord

    summary = (
        f"BLOCKED: `pytest -p {plugin}` loads an ad-hoc plugin with no process cap: a plugin "
        "that spawns pytest recurses until the box dies."
    )
    tail = (
        " Run it under the capped launcher: `python -m coordinator_core.testing.scoped_pytest "
        "--allow <target> -- <same pytest args>`."
    )
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name=GUARD_NAME,
        summary=summary,
        em_text=summary + tail,
        members=(
            AdaptableOffer(
                template="python -m coordinator_core.testing.scoped_pytest --allow <target> -- <same pytest args>",
                what_to_adapt="the allowed target and the pytest args",
            ),
        ),
    )
