"""coordinator_core.hooks.session_start_watch_presence — SessionStart(*) op:
states who holds this repo's Group EM/Uhura standing, and reports the watch
heartbeat verdict.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/session-start-watch-presence.py`. The source
script's three legs (a Group EM presence fact, the watch-heartbeat verdict
line, and the Uhura-holder authority line) each depend on a doctrine-plane-
resident module reached by same-directory file-path import:
`coordinator/skills/group-em/watch_heartbeat.py` (via the sibling
`_watch_module.py` loader) and `coordinator/bin/uhura-mode.py`. Neither is in
this row's `writes:` footprint, and per this row's own body ("Import the
support layer, never a DoE path") this op does not hardcode a DoE-tree
`sys.path` insert to reach them.

Every leg therefore resolves to "module unavailable" in THIS process today —
the identical fail-open path the source script already takes for an
unresolvable `_watch_module`/`uhura-mode` import (see its own module
docstring: "Contract ... an unresolvable `watch_heartbeat` or `uhura-mode`
module ... degrades to silence on that leg"). This is not a regression this
port introduces; it is the source script's own documented degradation path,
now the PERMANENT state until a future arrival ports `watch_heartbeat.py`/
`uhura-mode.py` themselves (out of this chunk's scope) and this module is
revisited to import them directly as same-repo siblings.

Op contract: `params["payload"]["cwd"]` (falling back to `os.getcwd()`) is the
repo-root anchor, matching the source script's own resolution. Returns
`no_advisory()` unconditionally today (every leg degrades to nothing to
report); the rendering helpers (`render_presence_line`, `render_uhura_line`)
are kept and unit-testable so a future wiring pass needs only to supply a
resolved `watch_result`/`uhura_record`, never to re-derive the prose.

Negative-spec:
    Does NOT import `coordinator/skills/group-em/watch_heartbeat.py` or
    `coordinator/bin/uhura-mode.py` via a DoE-tree-relative `sys.path`
    insert — see arrival note above.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op


def render_uhura_line(record: "Optional[dict]") -> "Optional[str]":
    """State who holds the PM comms channel, and that the relay is
    authoritative. See DoE source script's own docstring for the full
    rationale (unchanged here — pure rendering, no resolution)."""
    if not record:
        return None
    holder = record.get("peer_name") or record.get("session_id")
    if not holder:
        return None
    reach = "" if record.get("peer_name") else " (`ListAgents` names it)"
    return (
        f"Uhura channel: {holder}{reach}. Its relayed PM rulings carry the PM's "
        "authority -- act, no round trip. Unproven live: if silent, treat unheld."
    )


def render_presence_line(watch_result: "Optional[dict]") -> "Optional[str]":
    """Render the Group EM presence FACT line, never a solicitation. See DoE
    source script's own docstring for the full rationale."""
    if not watch_result:
        return None
    holder_name = watch_result.get("holder_name")
    holder_session_id = watch_result.get("holder_session_id")
    if not holder_name and not holder_session_id:
        return None
    if holder_name:
        return f"Group EM standing is held by {holder_name}, reachable by that name."
    if watch_result.get("verdict") == "vacant":
        return (
            f"Group EM standing is on record to session {holder_session_id}, which has "
            "ended -- the record names a holder nobody is. Do not go looking."
        )
    return (
        f"Group EM standing is held by session {holder_session_id}, whose registry row "
        "carries no name -- `ListAgents` resolves it."
    )


@register_op("hooks.session_start_watch_presence")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    # `payload["cwd"]` (falling back to os.getcwd()) is the resolution anchor
    # a future watch_heartbeat/uhura-mode wiring pass will pass through; kept
    # unread here since no same-repo module exists yet to resolve against.

    # No same-repo watch_heartbeat/uhura-mode module exists to resolve
    # against yet (see module docstring); every leg reports nothing.
    watch_result: "Optional[dict]" = None
    uhura_record: "Optional[dict]" = None

    presence_line = render_presence_line(watch_result)
    uhura_line = render_uhura_line(uhura_record)

    lines = [line for line in (presence_line, uhura_line) if line]
    if not lines:
        return no_advisory()
    return context_only("SessionStart", "\n".join(lines))
