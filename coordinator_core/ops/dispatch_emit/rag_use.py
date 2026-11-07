"""Pinned interface and host-CLI adapter for example-retrieval-repo usage and reachability.

Reads `example-retrieval-repo usage --session <id> --since <iso> --until <iso> --json` and
`example-retrieval-repo probe-reachability --json`. Every reader returns None when the CLI is
absent, times out, or emits unparseable output: None means "unknown", never "zero".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

DANGER_CALLS = 0
# Provisional: recalibrate from host meter rows once example-retrieval-repo usage lands.
WARNING_BELOW = 5
DEBOUNCE_SECONDS = 180
# A probe is a real engine-corpus call, so it must not ride the 5s scan tick.
PROBE_INTERVAL_SECONDS = 60
HALT_RAG_CORPUS_DOWN = "rag_corpus_down"
DEGRADED = "degraded"
RESTORE_MESSAGE = (
    "example-retrieval-repo engine corpus is down: UE work will not succeed without it. "
    "Restore example-retrieval-repo, then refire this run."
)

_CLI = "project-rag"
_STATES = ("ok", "degraded", "down")

# The single seam: runner(argv) -> stdout text, or None when the CLI is absent or fails.
# No runner means unknown (None), never zero.
Runner = Callable[[list[str]], Optional[str]]


@dataclass(frozen=True)
class RagUsage:
    """Calls to example-retrieval-repo made by one session inside one window."""

    calls: int
    by_tool: dict[str, int] = field(default_factory=dict)
    timeouts: int = 0
    degraded: bool = False


@dataclass(frozen=True)
class ProbeState:
    """Reachability of the engine corpus: state is ok, degraded or down."""

    state: str
    per_source: dict[str, Any] = field(default_factory=dict)


def _json_object(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def read_usage(
    session_id: str, since: str, until: str, *, runner: Optional[Runner] = None
) -> Optional[RagUsage]:
    """Usage of `session_id` between ISO `since` and `until`; None when unreadable."""
    if runner is None:
        return None
    out = runner(
        [_CLI, "usage", "--session", session_id, "--since", since,
         "--until", until, "--json"]
    )
    data = _json_object(out)
    if data is None:
        return None
    by_tool_raw = data.get("by_tool")
    calls_raw = data.get("calls")
    if not isinstance(by_tool_raw, dict) and isinstance(calls_raw, dict):
        by_tool_raw = calls_raw
    by_tool = (
        {str(k): _as_int(v) for k, v in by_tool_raw.items()}
        if isinstance(by_tool_raw, dict)
        else {}
    )
    if isinstance(calls_raw, (int, float)) and not isinstance(calls_raw, bool):
        calls = int(calls_raw)
    elif by_tool:
        calls = sum(by_tool.values())
    elif "calls" in data or "by_tool" in data:
        calls = 0
    else:
        return None
    return RagUsage(
        calls=calls,
        by_tool=by_tool,
        timeouts=_as_int(data.get("timeouts")),
        degraded=bool(data.get("degraded")),
    )


def probe(*, runner: Optional[Runner] = None) -> Optional[ProbeState]:
    """Engine-corpus reachability; None when the CLI cannot answer."""
    if runner is None:
        return None
    data = _json_object(runner([_CLI, "probe-reachability", "--json"]))
    if data is None:
        return None
    state = data.get("state")
    if state not in _STATES:
        return None
    per_source = data.get("per_source")
    return ProbeState(
        state=state, per_source=per_source if isinstance(per_source, dict) else {}
    )


def is_ue_repo(root: str | Path) -> bool:
    """True when `root` declares UE or carries a UE marker (same predicate as the compose legs)."""
    from coordinator_core.ops.dispatch_emit.ue_route_source import is_ue_repo as _is_ue_repo

    return _is_ue_repo(root)


def rag_installed(repo_root: str | Path) -> bool:
    """True when a example-retrieval-repo install is discoverable from `repo_root`; False on any error."""
    try:
        from coordinator_core.hooks.example_retrieval_repo_detect import find_example_retrieval_repo_dir

        return find_example_retrieval_repo_dir(str(repo_root)) is not None
    except Exception:
        return False


def docs_check_rag_verdict(
    *,
    ue: bool,
    rag_installed: bool,
    rag_calls: int,
    rag_failed: int,
    rag_timeouts: int = 0,
    rag_refused: int = 0,
    engine_queryable: Optional[bool] = None,
) -> str:
    """Gate verdict: "not_applicable", "ok", DEGRADED or HALT_RAG_CORPUS_DOWN.

    Down is definite only: no example-retrieval-repo call at all, engine_queryable False, or every call
    failed with a refused connection. Any other failure set (timeouts, unclassified errors, or
    any failure while engine_queryable is True) is DEGRADED; a partial failure that includes a
    timeout is DEGRADED too. The gate op passes its `engine_queryable` param.
    """
    if not ue or not rag_installed:
        return "not_applicable"
    if rag_calls <= 0 or engine_queryable is False:
        return HALT_RAG_CORPUS_DOWN
    if rag_failed >= rag_calls:
        if rag_refused > 0:
            return HALT_RAG_CORPUS_DOWN
        return DEGRADED
    if rag_timeouts > 0:
        return DEGRADED
    return "ok"
