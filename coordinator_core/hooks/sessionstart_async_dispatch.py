"""coordinator_core.hooks.sessionstart_async_dispatch — SessionStart async
fan-in op: composes every ASYNC (registered `async: true`) leg this row's
own `writes:` footprint carries.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/sessionstart-async-dispatch.py` —
folded originally for the identical reason its sync sibling
(`sessionstart_dispatch.py`) was: multiple `hooks.json` registrations sharing
one `python3` interpreter start. As with the sync dispatcher, the subprocess-
level fold (per-guard `importlib.util` import, byte-capture stdout/stderr
re-emission) has no analogue here — every composed leg is an already-
registered same-repo op, composed via direct handler import and CONCATENATE-
ALL aggregation. Async transport discards this op's returned context
regardless, but the aggregation is kept uniform with the sync dispatcher's
own shape (never a bare fire-and-forget loop) so a future transport change
that DOES surface async output needs no rewrite here.

COMPOSED HERE, all five within THIS row's own `writes:` footprint (DoE's own
`sources`, all `{"startup", "resume", "clear", "compact", "fork"}` except the
repair leg's `{"startup"}`):
  - `session_start_register_doe_claude_root`
  - `session_start_repair_prepare_commit_msg_hook` (startup only, per source)
  - `session_start_register_published_engine`
  - `sessionstart_ensure_http_forwarder`
  - `session_start_write_plugin_root_breadcrumb`

SOURCE-GATING is the caller's job, not self-applied here — matching the sync
dispatcher's own note. This op runs every leg unconditionally; whichever
registration eventually points `hooks.sessionstart_async_dispatch` at it
narrows by `source` at that seam, per DoE's own per-leg `sources` sets
recorded above for when that registration is written.

AGGREGATION CONTRACT: CONCATENATE-ALL, identical shape to the sync
dispatcher. Every leg is fully side-effect-driven (registry writes, a
forwarder ensure, a breadcrumb write) — none is expected to return
meaningful `additionalContext` under normal operation (async transport
discards it anyway), but a leg that DOES return context (e.g. the http-
forwarder-ensure disclosure path) is still folded in, never dropped.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import asyncio
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.session_start_register_doe_claude_root import (
    _handler as _session_start_register_doe_claude_root_handler,
)
from coordinator_core.hooks.session_start_register_published_engine import (
    _handler as _session_start_register_published_engine_handler,
)
from coordinator_core.hooks.session_start_repair_prepare_commit_msg_hook import (
    _handler as _session_start_repair_prepare_commit_msg_hook_handler,
)
from coordinator_core.hooks.session_start_write_plugin_root_breadcrumb import (
    _handler as _session_start_write_plugin_root_breadcrumb_handler,
)
from coordinator_core.hooks.sessionstart_ensure_http_forwarder import (
    _handler as _sessionstart_ensure_http_forwarder_handler,
)
from coordinator_core.ipc import register_op


def _extract_context(result) -> "Optional[str]":
    if not isinstance(result, dict):
        return None
    hso = result.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return None
    text = hso.get("additionalContext")
    return text if isinstance(text, str) and text else None


@register_op("hooks.sessionstart_async_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    payload = dict(payload)
    leg_params = {"payload": payload}

    texts: "list[str]" = []
    for leg_call in (
        lambda: _session_start_register_doe_claude_root_handler(leg_params),
        lambda: _session_start_repair_prepare_commit_msg_hook_handler(leg_params),
        lambda: _session_start_register_published_engine_handler(leg_params),
        lambda: _sessionstart_ensure_http_forwarder_handler(leg_params),
        lambda: _session_start_write_plugin_root_breadcrumb_handler(leg_params),
    ):
        try:
            result = await asyncio.to_thread(leg_call)
        except Exception:
            continue
        text = _extract_context(result)
        if text:
            texts.append(text)

    if not texts:
        return no_advisory()
    return context_only("SessionStart", "\n".join(texts))
