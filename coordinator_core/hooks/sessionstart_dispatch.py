"""coordinator_core.hooks.sessionstart_dispatch — SessionStart sync fan-in op:
composes the SYNC (never registered async) legs this row's own `writes:`
footprint carries.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from coordinator-content-repo `coordinator/hooks/scripts/sessionstart-dispatch.py` — a
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

COMPOSED HERE, in DoE's REGISTRY order, each with its `sources` set:
  - `project_orientation` (`startup`, `clear`, `compact`)
  - `guard_settings_integrity` (`startup`, `clear`, `compact`)
  - `guard_hooks_kill_switch_detail` (`startup`, `clear`, `compact`) — no DoE
    row; it takes the set of its sibling `guard_settings_integrity`
  - `guard_foreign_platform_paths` (`startup`, `clear`, `compact`)
  - `session_start_write_bump_anchor` (all five sources)
  - `bin_drift_refresh` (`startup`)
  - `job_mode_announce` (`startup`)
  - `governed_surface_drift` (all five sources)
  - `guard_hook_generation_self_probe` (`startup`, `clear`, `compact`)
  "All five" is `startup`, `resume`, `clear`, `compact`, `fork`. Every leg is a
  direct import of an already-registered engine function; no `hooks.*` wrapper
  op exists for the three adapters defined here.

DEFERRED — NOT COMPOSED, for a stated reason, not an oversight:
  - `day_branch_assert` — its DoE shim gates on the DoE-owned author profile
    and a compliance pre-gate before it calls the engine, and the leg mutates
    git. Putting it on the http path is a port with an ownership question, not
    a closure step.

SOURCE-GATING: this op is reached through an http entry on the union matcher,
so gating lives here. The handler reads `payload["source"]`. A missing source
runs no leg. A non-empty source in no leg's set runs no leg and returns the
`[sessionstart-dispatch] source=... matches no guard in REGISTRY` breadcrumb as
context. Otherwise a leg runs only when the source is in its `sources` set.

AGGREGATION CONTRACT: CONCATENATE-ALL (never first-fires-wins), mirroring
`stop_dispatch.py`'s own contract and the source dispatcher's own
incremental-emit intent — every leg runs regardless of an earlier leg's
result; one leg raising is isolated to that leg alone.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.bash_guards._write_bump_session_start import (
    write_session_start_record as _write_session_start_record,
)
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.guard_hook_generation_self_probe import (
    _handler as _guard_hook_generation_self_probe_handler,
)
from coordinator_core.hooks.project_orientation import (
    _handler as _project_orientation_handler,
)
from coordinator_core.hooks.session_start_announce_job_mode import (
    _handler as _session_start_announce_job_mode_handler,
)
from coordinator_core.hooks.sessionstart_bin_drift_refresh import (
    _handler as _sessionstart_bin_drift_refresh_handler,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_admission import (
    uncommitted_surface_refusals as _uncommitted_surface_refusals,
)
from coordinator_core.ops.session.guard_foreign_platform_paths import (
    evaluate_foreign_platform_paths as _evaluate_foreign_platform_paths,
)
from coordinator_core.ops.session.guard_settings_integrity import (
    _handler as _guard_settings_integrity_handler,
    _handler_kill_switch_detail as _guard_hooks_kill_switch_detail_handler,
)


def _extract_context(result) -> "Optional[str]":
    if not isinstance(result, dict):
        return None
    text = result.get("text")
    if isinstance(text, str) and text:
        return text
    hso = result.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return None
    text = hso.get("additionalContext")
    return text if isinstance(text, str) and text else None


_STARTUP = frozenset({"startup"})
_START_CLEAR_COMPACT = frozenset({"startup", "clear", "compact"})
_ALL_SOURCES = frozenset({"startup", "resume", "clear", "compact", "fork"})

_DRIFT_HEADER = (
    "UNADMITTED BOOT PAYLOAD: uncommitted text in a governed doctrine surface fails "
    "admission, and this session loaded it. Treat that text as not doctrine. Its "
    "author commits it through the ledger (classify the section or bump the "
    "watermark with a reason) or removes it; a commit of it as it stands is refused."
)


def _leg_foreign_platform_paths(payload: dict) -> "Optional[str]":
    config_dir_param = payload.get("config_dir")
    if config_dir_param:
        config_dir = Path(config_dir_param)
    else:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return _evaluate_foreign_platform_paths(config_dir / "settings.json", config_dir=config_dir)


def _leg_write_bump_anchor(payload: dict) -> None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    cwd = payload.get("cwd")
    _write_session_start_record(
        session_id, launch_cwd=cwd if isinstance(cwd, str) and cwd else None
    )
    return None


def _leg_governed_surface_drift(payload: dict) -> "Optional[str]":
    cwd = payload.get("cwd")
    refusals = _uncommitted_surface_refusals(
        Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
    )
    if not refusals:
        return None
    return "\n".join([_DRIFT_HEADER, *(f"  - {r}" for r in refusals)])


# (module_key, sources, leg callable name) in DoE REGISTRY order. Each leg takes
# the payload dict and returns a result `_extract_context` understands, or text.
_LEGS = (
    ("project_orientation", _START_CLEAR_COMPACT, "_leg_project_orientation"),
    ("guard_settings_integrity", _START_CLEAR_COMPACT, "_leg_guard_settings_integrity"),
    ("guard_hooks_kill_switch_detail", _START_CLEAR_COMPACT, "_leg_kill_switch_detail"),
    ("guard_foreign_platform_paths", _START_CLEAR_COMPACT, "_leg_foreign_platform_paths"),
    ("session_start_write_bump_anchor", _ALL_SOURCES, "_leg_write_bump_anchor"),
    ("bin_drift_refresh", _STARTUP, "_leg_bin_drift_refresh"),
    ("job_mode_announce", _STARTUP, "_leg_job_mode_announce"),
    ("governed_surface_drift", _ALL_SOURCES, "_leg_governed_surface_drift"),
    ("guard_hook_generation_self_probe", _START_CLEAR_COMPACT, "_leg_self_probe"),
)


def _leg_project_orientation(payload):
    return _project_orientation_handler({"payload": payload})


def _leg_guard_settings_integrity(payload):
    return _guard_settings_integrity_handler(payload)


def _leg_kill_switch_detail(payload):
    return _guard_hooks_kill_switch_detail_handler(payload)


def _leg_bin_drift_refresh(payload):
    return _sessionstart_bin_drift_refresh_handler({"payload": payload})


def _leg_job_mode_announce(payload):
    return _session_start_announce_job_mode_handler({"payload": payload})


def _leg_self_probe(payload):
    return _guard_hook_generation_self_probe_handler({"payload": payload})


def _unmatched_breadcrumb(source: str) -> str:
    return (
        f"[sessionstart-dispatch] source={source!r} matches no guard in REGISTRY -- "
        "every guard skipped for this boot. If the harness added a source value, add it "
        "to the per-guard `sources` sets, not just the hooks.json matcher."
    )


@register_op("hooks.sessionstart_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        return no_advisory()

    matching = [(k, fn) for k, srcs, fn in _LEGS if source in srcs]
    if not matching:
        return context_only("SessionStart", _unmatched_breadcrumb(source))

    texts: "list[str]" = []
    for _key, fn_name in matching:
        try:
            result = globals()[fn_name](payload)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            continue
        text = result if isinstance(result, str) and result else _extract_context(result)
        if text:
            texts.append(text)

    if not texts:
        return no_advisory()
    return context_only("SessionStart", "\n".join(texts))
