"""
coordinator_core.plugin_health — read-only health/currency probes for DoE-claude's
plugin ecosystem (live-install drift, doctor-sentinel probes, addon-health scan).

Sibling modules (drift.py, sentinel.py, scan.py, probe_select.py) each
self-register their own op(s) via a module-level register_op(...) call — this
__init__ stays minimal and carries no shared state sibling modules depend on.

C6 (docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md)
adds this module's own `brief()` — this host's scaffold-emission compute
for the one row `coordinator_core/ops/doctype_hosts.py` marks `emitted`
against `module="coordinator_core.plugin_health"`: `health-status` (keyed
`ceremony="plugin-health"`). `health-status` carries no required flag of
its own on `coordinator-doc-new`'s real parser beyond `--type` — its
`--out` is the one computed value, mirroring the CLI's own default path
(`state/health/YYYY-MM-DD-health-summary.md`, one-per-day). `brief()` is
additive and gated on an explicit `emit_health_status` boolean — the
ceremony's own resolved "a daily health-status record is due" signal,
never inferred here — so a caller not passing it is unaffected.

Negative-spec: entirely unrelated to coordinator_core.doctor_envelope, which is
Claude-klabauter's OWN two-tier doctor for claude-klabauter's own health (see that module's negative-
spec for the disambiguation both directions).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T3a-g2/T3b
Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C6.

Negative spec (C6's slice): does NOT decide whether `health-status` is
emitted at all — that is `coordinator_core.ops.doctype_hosts`'s (C0) table,
read by C8's coverage pin, never re-derived here. Does NOT call
`coordinator-doc-new` or any other CLI — a compute-half constructor only
(§ Which discriminator this plan uses). Does NOT run any probe itself
(drift/sentinel/scan/probe_select are untouched by C6) — this module's own
writes are scoped to `__init__.py` alone.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)

# C6: the shared constructor's (C1) per-type required-flag computation for
# this host's one emitted row (coordinator_core/ops/doctype_hosts.py --
# keyed (type="health-status", ceremony="plugin-health"),
# module=this package). No flag beyond `--type`/`--out` is required or
# even accepted by the real parser for this type.
_HEALTH_STATUS_FLAG_SPEC: tuple[Flag, ...] = ()


def _health_status_directive() -> dict[str, Any]:
    """Computes the `health-status` scaffold directive through the shared
    constructor (C1). `--out` mirrors `coordinator-doc-new`'s own
    `health-status` default path (`state/health/YYYY-MM-DD-health-
    summary.md`, one-per-day) — computed here rather than left to the
    CLI's own default, so `already_satisfied` (this constructor's replay
    guard) can see an already-emitted today's record."""
    root = Path.cwd()
    today = date.today().isoformat()
    out_path = f"state/health/{today}-health-summary.md"
    resolved: dict[str, Any] = {"out": out_path}
    return build_scaffold_directive(
        "d-scaffold-health-status",
        "health-status",
        resolved,
        _HEALTH_STATUS_FLAG_SPEC,
        root=root,
    )


def brief(*, emit_health_status: bool = False) -> dict[str, Any]:
    """C6: this module's own scaffold-emission compute. Returns
    `{"directives": [...]}`: a `health-status` directive when the caller
    has resolved `emit_health_status` true (a daily health-status record
    is due), else an empty list — additive and gated, same shape as
    `roadmap_planning_assemble.brief`'s C3 precedent, so a caller not
    passing it is unaffected.
    """
    directives: list[dict[str, Any]] = []
    if emit_health_status:
        directives.append(_health_status_directive())
    return {"directives": directives}
