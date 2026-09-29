"""coordinator_core.hooks.session_start_watch_presence — SessionStart op:
state who holds this repo's Group EM standing, the watch heartbeat's own
verdict, and who holds the Uhura comms channel, as one `additionalContext`
line-set.

Port of: coordinator-content-repo `coordinator/hooks/scripts/session-start-watch-presence.py`
(PM order: warm-hook migration, claude-klabauter slice). Same three facts, same
never-a-solicitation posture, same silent-on-nothing-to-say contract.

Shape change from the DoE source: the DoE script shells a subprocess-free
but FILE-PATH import of `coordinator/skills/group-em/watch_heartbeat.py`
(`_watch_module.resolve_watch_module`) and of `coordinator/bin/uhura-mode.py`.
Both legs are read in-process here instead:

- The watch-liveness leg reads `coordinator_core.group_em.watch_heartbeat
  .read_liveness`, the SAME reader `ops/group_em_enter.py`'s own
  `watch_liveness` leg already uses — one watch-record reader in this
  engine, not two independently drifting ones.
- The Uhura leg has no existing port in this engine (`grep -ri uhura
  coordinator_core/` finds nothing). `_read_uhura_record` below is a
  MINIMAL port of `uhura-mode.py::read_record` — same record path
  (`<settings_home>/state/uhura/<repo_key>.json`, via
  `coordinator_core.group_em.atomic_record.settings_home`/`repo_key`, the
  identical helpers `read_record`'s own docstring says its DoE sibling
  shares with the Group EM record), same schema-version fencing, same
  never-raises contract. Not a new module: `uhura-mode.py` is a CLI with a
  write path (`enter`/`stand_down`) this hook never needs: only the read
  leg is ported, inline, because there is nothing else here to hang a
  package on yet.

PARITY GAP, NAMED RATHER THAN SILENTLY DROPPED: `read_liveness` here never
returns a `vacant` verdict (holder-session-no-longer-live) — the DoE reader
still models one, but `coordinator_core.group_em.watch_heartbeat.read_liveness`
already OMITTED it on this side by an accepted `overengineering-reviewer`
finding (see that function's own docstring): no in-repo caller ever passed
the `agents` join that verdict requires, and a watcher that exited stops
stamping and reads `stale` on the next tick anyway — same finding, better
evidence. This hook therefore renders `stale`/`armed`/`absent` only; a
`vacant`-worded presence line never fires and holder-named `stale` output
covers the same case.

Contract, unchanged: never blocks, never raises past this handler, silent
(`no_advisory()`) when there is nothing to report.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.group_em import atomic_record
from coordinator_core.group_em import watch_heartbeat as group_em_watch_heartbeat
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

_UHURA_SCHEMA_VERSION = 1


def _read_uhura_record(repo_root: str) -> Optional[dict]:
    """Minimal port of DoE `uhura-mode.py::read_record`. Never raises."""
    try:
        path = atomic_record.settings_home() / "state" / "uhura" / f"{atomic_record.repo_key(repo_root)}.json"
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return None
        if int(record.get("schema_version") or 0) > _UHURA_SCHEMA_VERSION:
            return None
        return record
    except Exception:  # noqa: BLE001 -- read leg must degrade to silence, never raise
        return None


def render_watch_line(liveness: dict) -> Optional[str]:
    verdict = (liveness or {}).get("verdict")
    if not verdict:
        return None
    return f"GROUP EM WATCH: {verdict}"


def render_presence_line(liveness: dict) -> Optional[str]:
    if not liveness:
        return None
    holder_name = liveness.get("holder_name")
    holder_session_id = liveness.get("holder_session_id")
    if not holder_name and not holder_session_id:
        return None
    if holder_name:
        return (
            f"Group EM standing is held by {holder_name}, reachable by that name. Its "
            "direction on this repo carries PM-delegated authority -- act, no round trip."
        )
    return (
        f"Group EM standing is held by session {holder_session_id}, whose registry row "
        "carries no name -- `ListAgents` resolves it."
    )


def render_uhura_line(record: Optional[dict]) -> Optional[str]:
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


def compute_context(payload: dict) -> Optional[str]:
    cwd = payload.get("cwd")
    repo_root = cwd if isinstance(cwd, str) and cwd else os.getcwd()

    try:
        liveness = group_em_watch_heartbeat.read_liveness(repo_root, time.time())
    except Exception:  # noqa: BLE001
        liveness = None

    presence_line = render_presence_line(liveness) if liveness else None
    watch_line = render_watch_line(liveness) if liveness else None

    try:
        uhura_record = _read_uhura_record(repo_root)
    except Exception:  # noqa: BLE001
        uhura_record = None
    uhura_line = render_uhura_line(uhura_record)

    lines = [line for line in (presence_line, watch_line, uhura_line) if line]
    if not lines:
        return None
    return "\n".join(lines)


@register_op("hooks.session_start_watch_presence")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        additional_context = compute_context(params)
    except Exception:  # noqa: BLE001 -- SessionStart never blocks
        additional_context = None

    if not additional_context:
        return no_advisory()
    return context_only("SessionStart", additional_context)
