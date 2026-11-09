"""agent_pipe.grok -- Grok Build session driver: the generic ACP client over `grok agent stdio`."""
from __future__ import annotations

from typing import Optional

from coordinator_core.agent_pipe.acp import AcpDriver
from coordinator_core.agent_pipe.contract import KIND_GROK
from coordinator_core.agent_pipe.session_driver import ApprovalCallback

GROK_ACP_ARGV = ["grok", "agent", "stdio"]


class GrokAcpDriver(AcpDriver):
    """SessionDriver for grok; `argv` is overridable so tests drive a fake agent. Never passes an auto-approve flag."""

    def __init__(self, approve: Optional[ApprovalCallback] = None, argv: Optional[list[str]] = None, **kw) -> None:
        super().__init__(list(argv) if argv is not None else list(GROK_ACP_ARGV), approve, kind=KIND_GROK, **kw)
