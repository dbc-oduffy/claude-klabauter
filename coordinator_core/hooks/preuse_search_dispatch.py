"""coordinator_core.hooks.preuse_search_dispatch — PreToolUse(Grep, Glob)
advisory: steer structural questions to example-retrieval-repo where it answers.

Two kinds of fire, both advisory (`context_only`, never a permission
decision), both fail open to `no_advisory()`:

  1. General — the first Grep/Glob per (session, agent) against a repo that
     example-retrieval-repo answers for names the structural tools once.
  2. Shape — a Grep whose pattern matches a `SHAPES` row names the exact
     tool for that shape, once per (session, agent, shape). The table is the
     extension point: add a row, not a branch.

"Answers for this repo" means a `.example-retrieval-repo/graph.db` found walking up from
the search root (`tool_input.path`, else `cwd`), via the same walk
`example_retrieval_repo_detect` uses. An MCP server being
configured is not enough; a repo without an index adds nothing to the call.

Hot path: fires on every Grep/Glob, so the budget is file stats only — no git
spawn, no staleness probe (the advisory names `project_staleness_check`
instead of computing it). Sentinels live in the indexed repo's git common dir
under `coordinator-sessions/<session_id>/`; when that cannot be resolved the
op stays silent rather than firing on every call.

Kill-switch: env COORDINATOR_HOOK_PREUSE_SEARCH_DISPATCH_DISABLED=1.

Op contract: `params` is the flat PreToolUse payload dict (`tool_name`,
`tool_input`, `session_id`, `cwd`, `agent_id`, …).

Spec backlink: state/cross-repo/inbox/2026-09-25-doe-claude-em-example-retrieval-repo-adoption-hooks.md
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from coordinator_core._hook_envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks.example_retrieval_repo_detect import find_example_retrieval_repo_dir
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.hooks.support.session_hub import (
    ensure_session_dir,
    session_id_is_real,
)
from coordinator_core.ipc import register_op

_KILL_SWITCH = "COORDINATOR_HOOK_PREUSE_SEARCH_DISPATCH_DISABLED"
_SEARCH_TOOLS = frozenset({"Grep", "Glob"})
_GENERAL_SENTINEL = "example-retrieval-repo-search-nudged"
_AGENT_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

_GENERAL_ADVICE = (
    "example-retrieval-repo indexes this repo. Who calls X, where X is defined, what "
    "depends on it: project_symbol_callers, project_symbol, "
    "project_semantic_search (check project_staleness_check first). Grep "
    "stays right for literal text."
)

# Grep patterns arrive as regex source: an optional literal `\b` wraps a word.
_WB = r"(?:\\b)?"
_IDENT = r"([A-Za-z_][A-Za-z0-9_]*)"
_DEFINITION_RE = re.compile(
    r"^\^?" + _WB + r"(?:async\s+def|def|class|function|fn|struct|interface)"
    r"(?:\\s[+*]|\s+)" + _IDENT + _WB + r"(?:\\?\(.*)?$"
)
_CALL_SITE_RE = re.compile(r"^" + _WB + _IDENT + r"\\?\($")
_BARE_IDENT_RE = re.compile(r"^" + _WB + _IDENT + _WB + r"$")


def _looks_like_code_symbol(name: str) -> bool:
    """A bare word is only a symbol lookup when it is shaped like one:
    snake_case or camelCase, not an English word or a TODO marker."""
    if len(name) < 4:
        return False
    return "_" in name.strip("_") or re.search(r"[a-z][A-Z]", name) is not None


def _match_definition(pattern: str) -> Optional[str]:
    m = _DEFINITION_RE.match(pattern)
    return m.group(1) if m else None


def _match_call_site(pattern: str) -> Optional[str]:
    m = _CALL_SITE_RE.match(pattern)
    return m.group(1) if m else None


def _match_bare_identifier(pattern: str) -> Optional[str]:
    m = _BARE_IDENT_RE.match(pattern)
    if m and _looks_like_code_symbol(m.group(1)):
        return m.group(1)
    return None


@dataclass(frozen=True)
class SearchShape:
    shape_id: str
    match: Callable[[str], Optional[str]]
    advice: str  # formatted with {symbol}


# First match wins; order runs most-specific first.
SHAPES: Tuple[SearchShape, ...] = (
    SearchShape(
        "definition",
        _match_definition,
        "`{symbol}` definition: project_symbol(symbol_name=\"{symbol}\") "
        "returns its location and signature in one call.",
    ),
    SearchShape(
        "call-site",
        _match_call_site,
        "Callers of `{symbol}`: project_symbol_callers(symbol_name=\"{symbol}\") "
        "resolves real call edges; a text grep misses indirect calls and "
        "names wrong callers.",
    ),
    SearchShape(
        "bare-identifier",
        _match_bare_identifier,
        "`{symbol}` is a symbol: project_symbol(symbol_name=\"{symbol}\") "
        "finds its definition, project_symbol_callers its callers.",
    ),
)


def _search_root(params: dict, tool_input: dict) -> Optional[str]:
    cwd = params.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return cwd
    root = path if os.path.isabs(path) else os.path.join(cwd, path)
    if os.path.isfile(root):
        return os.path.dirname(root)
    return root if os.path.isdir(root) else cwd


def _indexed_repo_root(search_root: str) -> Optional[str]:
    rag_dir = find_example_retrieval_repo_dir(search_root)
    if not rag_dir or not os.path.isfile(os.path.join(rag_dir, "graph.db")):
        return None
    return os.path.dirname(rag_dir)


def _claim_once(session_dir: Path, session_id: str, name: str) -> bool:
    """True the first time `name` is claimed in `session_dir`. A sentinel
    that cannot be written claims nothing — silence beats a per-call fire."""
    sentinel = session_dir / name
    try:
        if sentinel.exists():
            return False
        if not ensure_session_dir(session_dir, session_id):
            return False
        sentinel.touch()
    except OSError:
        return False
    return True


@register_op("hooks.preuse_search_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Grep, Glob) op: name example-retrieval-repo's structural tools once
    per agent, and the exact tool for a recognised pattern shape."""
    if os.environ.get(_KILL_SWITCH) == "1":
        return no_advisory()
    params = payload_of(params)
    if params.get("tool_name") not in _SEARCH_TOOLS:
        return no_advisory()
    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id_is_real(session_id):
        return no_advisory()

    search_root = _search_root(params, tool_input)
    indexed_root = _indexed_repo_root(search_root) if search_root else None
    if not indexed_root:
        return no_advisory()
    common_dir = resolve_git_common_dir(indexed_root)
    if not common_dir:
        return no_advisory()
    session_dir = Path(common_dir) / "coordinator-sessions" / session_id

    agent_id = params.get("agent_id")
    suffix = ""
    if isinstance(agent_id, str) and agent_id:
        suffix = "." + _AGENT_ID_SAFE_RE.sub("_", agent_id)

    parts: List[str] = []
    if _claim_once(session_dir, session_id, _GENERAL_SENTINEL + suffix):
        parts.append(_GENERAL_ADVICE)

    pattern = tool_input.get("pattern")
    if params.get("tool_name") == "Grep" and isinstance(pattern, str):
        pattern = pattern.strip()
        for shape in SHAPES:
            symbol = shape.match(pattern)
            if symbol is None:
                continue
            if _claim_once(session_dir, session_id, f"example-retrieval-repo-shape-{shape.shape_id}{suffix}"):
                parts.append(shape.advice.format(symbol=symbol))
            break

    if not parts:
        return no_advisory()
    return context_only("PreToolUse", "\n".join(parts))
