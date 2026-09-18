"""coordinator_core.hooks.sessionstart_dispatch — SessionStart sync fan-in op:
composes the SYNC (never registered async) legs this row's own `writes:`
footprint carries.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/sessionstart-dispatch.py` — a
subprocess-based fan-in over five hyphenated sibling scripts, folded into one
`python3` process to save four interpreter starts per boot. That whole
mechanism (dynamic `importlib.util.spec_from_file_location` per-guard import,
an incremental byte-level stdout/stderr flush to survive a guard's
`os._exit(0)`, a `Ctx`/`StartGuard` dataclass registry) existed to compose
FILES; here, every composable leg is an already-registered same-repo op,
composed via a direct handler import and CONCATENATE-ALL aggregation — the
identical shape `stop_dispatch.py` (a prior arrival) already established for
this engine's own SessionStart/Stop fan-ins. There is no subprocess
boundary left to fold across, so none of the byte-capture machinery applies.

COMPOSED HERE, all three within THIS row's own `writes:` footprint:
  - `sessionstart_bin_drift_refresh` (source `sources`: `{"startup"}`)
  - `session_start_announce_job_mode` (source `sources`: `{"startup"}`)
  - `guard_hook_generation_self_probe` (source `sources`:
    `{"startup", "clear", "compact"}`)

DEFERRED — NOT COMPOSED, each for a stated reason, not an oversight:
  - `project_orientation` — DoE's own `REGISTRY` folds it here too, but its
    arrival is W4-C11, a SIBLING chunk in this same wave with no ordering
    dependency on this one; composing an op this row does not own would be
    reaching outside this chunk's `writes:`. A future edit adds it once
    landed.
  - `guard_settings_integrity` / `guard_foreign_platform_paths` — DoE
    classifies both "retire": DoE shims over an engine body that ALREADY
    EXISTS (`session.guard_settings_integrity` /
    `session.guard_hooks_kill_switch_detail`, already-registered IPC ops,
    not `hooks.*`-namespaced SessionStart ops). Composing them here would
    require a `hooks.*` wrapper this row's `writes:` does not authorize.
  - `day_branch_assert` — `coordinator_core/hooks/day_branch_assert.py`
    exists on disk (a prior, unrelated arrival) but carries no
    `@register_op` — it is not yet a callable op this fan-in can compose
    without reaching outside this chunk's own `writes:` to add one.

SOURCE-GATING: this op's own per-leg `sources` set is NOT re-derived here —
gating stays the caller's job (whichever registration eventually points
`hooks.sessionstart_dispatch` at this op), matching every other composed
`hooks.*` op in this engine, none of which self-gates on
`params["payload"]["source"]`. The comment above records each composed leg's
DoE-side `sources` value for when that registration is written.

AGGREGATION CONTRACT: CONCATENATE-ALL (never first-fires-wins), mirroring
`stop_dispatch.py`'s own contract and the source dispatcher's own
incremental-emit intent — every leg runs regardless of an earlier leg's
result; one leg raising is isolated to that leg alone.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.guard_hook_generation_self_probe import (
    _handler as _guard_hook_generation_self_probe_handler,
)
from coordinator_core.hooks.session_start_announce_job_mode import (
    _handler as _session_start_announce_job_mode_handler,
)
from coordinator_core.hooks.sessionstart_bin_drift_refresh import (
    _handler as _sessionstart_bin_drift_refresh_handler,
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


@register_op("hooks.sessionstart_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    payload = dict(payload)
    leg_params = {"payload": payload}

    texts: "list[str]" = []
    for leg_call in (
        lambda: _sessionstart_bin_drift_refresh_handler(leg_params),
        lambda: _session_start_announce_job_mode_handler(leg_params),
        lambda: _guard_hook_generation_self_probe_handler(leg_params),
    ):
        try:
            result = await leg_call()
        except Exception:
            continue
        text = _extract_context(result)
        if text:
            texts.append(text)

    if not texts:
        return no_advisory()
    return context_only("SessionStart", "\n".join(texts))
