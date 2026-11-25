"""Host-neutral hook contract: HostEvent, per-host neutral fallback, Adapter protocol.

Stdlib only: the shim imports this on a cold interpreter, so nothing here may pull in the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Protocol, runtime_checkable

from coordinator_core.install.host_registry import FOREIGN

HOSTS = FOREIGN
HOST_CODEX, HOST_AGY, HOST_GROK = "codex", "agy", "grok"
assert {HOST_CODEX, HOST_AGY, HOST_GROK} <= set(HOSTS), "HOST_* constants must be registry FOREIGN ids"

# A verdict is the engine's decision for one event: {"decision": "allow"|"deny"|"ask", "reason": str}.
Verdict = Mapping[str, Any]


@dataclass(frozen=True)
class HostEvent:
    """One host hook event, normalised. `session_id` comes from the payload, never from env."""

    host: str
    event: str
    session_id: str
    tool_name: str = ""
    tool_input: Mapping[str, Any] = field(default_factory=dict)
    cwd: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


def neutral_fallback(host: str) -> Dict[str, Any]:
    """The answer that changes nothing on `host` when no verdict exists.

    Codex: `{}` lets the operation continue. agy: `{}` is fail-closed (the call is denied), and an
    `ask` stalls a subagent that cannot answer it, so the no-verdict answer is an explicit allow (DR-402:
    the backstop holds irreversible denies; no other guard denies recoverable work). Its reason keeps
    "guard did not run": the fail-open benchmarks detect a no-verdict answer by that phrase.
    grok: `{}` (Claude-shaped, fail-open).
    """
    if host in (HOST_CODEX, HOST_GROK):
        return {}
    if host == HOST_AGY:
        return {"decision": "allow", "reason": "coordinator: guard did not run, allowing"}
    raise ValueError("unknown host: %r" % (host,))


@runtime_checkable
class Adapter(Protocol):
    """Per-host translation of a raw hook payload to a HostEvent and a verdict back to the host shape."""

    def adapt_event(self, payload: Mapping[str, Any]) -> HostEvent: ...

    def render(self, host_event: HostEvent, verdict: Verdict) -> Dict[str, Any]: ...
