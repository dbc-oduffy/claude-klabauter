"""agent_pipe.session_driver -- vendor-neutral session-driver protocol and value types (no I/O).

Adapters (ACP for grok/agy, codex app-server) implement SessionDriver; the approval callback is passed
to the adapter at construction and answers each PermissionRequest with a Decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Protocol

ALLOW_ONCE = "allow_once"
ALLOW_ALWAYS = "allow_always"
REJECT_ONCE = "reject_once"


@dataclass(frozen=True)
class SessionHandle:
    """One live vendor session: durable `session_id` plus the `cwd` it runs in."""

    session_id: str
    cwd: str


@dataclass(frozen=True)
class Update:
    """One streamed prompt update. `kind` is the vendor-neutral class (e.g. message, thought, tool_call, done); `raw` is the vendor payload."""

    kind: str
    text: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionRequest:
    """A tool-permission ask from the agent; `options` are the vendor's option ids (ALLOW_ONCE/ALLOW_ALWAYS/REJECT_ONCE or vendor-specific)."""

    tool: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """The approval callback's answer: the chosen `option_id`."""

    option_id: str


ApprovalCallback = Callable[[PermissionRequest], Decision]


class SessionDriver(Protocol):
    """Start, resume, prompt-stream, cancel and close a vendor agent session; approval is the constructor's ApprovalCallback."""

    def start(self, cwd: str, prompt: Optional[str] = None) -> SessionHandle: ...

    def resume(self, session_id: str) -> SessionHandle: ...

    def prompt(self, handle: SessionHandle, text: str) -> Iterator[Update]: ...

    def cancel(self, handle: SessionHandle) -> None: ...

    def close(self, handle: SessionHandle) -> None: ...
