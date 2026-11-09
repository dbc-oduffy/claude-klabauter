"""Host-neutral hook contract: HostEvent, per-host neutral fallback, Adapter protocol.

Stdlib only: the shim imports this on a cold interpreter, so nothing here may pull in the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Protocol, runtime_checkable

HOST_CODEX = "codex"
HOST_AGY = "agy"
HOSTS = (HOST_CODEX, HOST_AGY)

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

    Codex: `{}` lets the operation continue. agy: `{}` is fail-closed (the call is denied),
    so the no-verdict answer is an explicit `ask`.
    """
    if host == HOST_CODEX:
        return {}
    if host == HOST_AGY:
        return {"decision": "ask", "reason": "coordinator: guard did not run"}
    raise ValueError("unknown host: %r" % (host,))


@runtime_checkable
class Adapter(Protocol):
    """Per-host translation of a raw hook payload to a HostEvent and a verdict back to the host shape."""

    def adapt_event(self, payload: Mapping[str, Any]) -> HostEvent: ...

    def render(self, host_event: HostEvent, verdict: Verdict) -> Dict[str, Any]: ...
