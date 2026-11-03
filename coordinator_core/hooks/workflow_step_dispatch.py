"""
coordinator_core.hooks.workflow_step_dispatch — SubagentStart/SubagentStop advisory op.

DoE's hooks.json wires `hook-run --advisory hooks.workflow_step_dispatch` on both
subagent events. No workflow step is matched by this op today, so it answers the
no-match case: an advisory success with no injection.

Negative-spec:
    Spawns no process and performs no file I/O: a hook that fires on every
    subagent start and stop must cost one registry lookup, nothing more.
    Adding a matcher here must keep that: no subprocess, no git, no disk read
    on the no-match path.
"""

from __future__ import annotations

from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op


GENERATES: list = []


@register_op("hooks.workflow_step_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    return no_advisory()


run = _handler
