"""coordinator_core.hooks.session_start_announce_job_mode — SessionStart
(startup-only) op: announces the resolved `job_mode` (blitz/cron/interactive).

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/session-start-announce-job-mode.py`
— DR-047 transport-seam PLUMBING: DoE's own version resolved a sibling claude-klabauter
checkout, placed it on `sys.path`, and imported
`coordinator_core.session.mode_resolution.resolve_mode` across that boundary.
None of that applies here: this module IS inside the engine boundary, so
`resolve_mode` is a same-repo sibling import — the whole cross-repo resolution
step (`_engine_root.resolve_claude_klabauter_root`/`arm_lazy_ops`/
`place_engine_root_on_path`) is dropped, matching `preuse_write_dispatch.py`'s
own W4-C7 precedent for the identical class of simplification.

Op contract: `params["payload"]` is the flat SessionStart payload dict
(`session_id`, `source`, ...); only `session_id` is read (best-effort, for the
durable log line). Returns `context_only("SessionStart", <one-line banner>)`
naming the resolved mode and its provenance, or `no_advisory()` on any
resolution failure. The durable log append is a side effect independent of
the returned envelope — a write failure there never retracts the banner
already composed (mirrors the source script's own per-leg independence).

Negative-spec:
    Does NOT resolve a sibling engine checkout or place anything on
    `sys.path` — nothing on the other side of a boundary to resolve.
    Does NOT gate on `source == "startup"` itself — that gating is the
    fan-in's job (`sessionstart_dispatch.py`'s own per-leg `sources` set),
    matching every other composed leg in this arrival, which is unconditional
    on its own and relies on its caller for event-source scoping.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from coordinator_core._hook_envelope import payload_of
from coordinator_core._settings_home import settings_home
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

_LOG_FILENAME = "job-mode-announce.log"


def _append_durable_line(line: str) -> None:
    log_path = settings_home() / "state" / _LOG_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


@register_op("hooks.session_start_announce_job_mode")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    session_id = payload.get("session_id")
    session_id = session_id if isinstance(session_id, str) and session_id else "unknown"

    try:
        from coordinator_core.session.mode_resolution import (
            COORDINATOR_JOB_MODE,
            JOB_MODE_VALUES,
            resolve_mode,
        )
    except Exception:
        return no_advisory()

    env = dict(os.environ)
    try:
        mode = resolve_mode(
            "job_mode", session_id if session_id != "unknown" else "", env=env
        )
    except Exception:
        return no_advisory()

    raw_env_value = env.get(COORDINATOR_JOB_MODE)
    if isinstance(raw_env_value, str) and raw_env_value in JOB_MODE_VALUES:
        provenance = f"asserted via {COORDINATOR_JOB_MODE}"
    else:
        provenance = f"{COORDINATOR_JOB_MODE} absent/unrecognised -- conservative anchor"

    banner = f"[job-mode] {mode} ({provenance})"

    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        _append_durable_line(
            f"{timestamp} session={session_id} job_mode={mode} ({provenance})"
        )
    except Exception:
        pass

    return context_only("SessionStart", banner)
