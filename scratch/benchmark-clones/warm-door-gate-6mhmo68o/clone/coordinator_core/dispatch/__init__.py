"""
coordinator_core.dispatch -- agent-side dispatch-tooling package (W2-B3,
canonical-resolution-engine Wave 2).

Public seam: ``provision_subagent_sidecar`` is the spawn-time provisioner
that writes the agent-side decision-object container (the ``subagent-sidecar``
document type scaffolded by ``coordinator-doc-new --type subagent-sidecar``)
under ``state/subagent-share/<session_id>/<key>.md``.

See ``coordinator_core.dispatch.provision`` for the implementation and
``docs/plans/2026-07-24-canonical-resolution-engine.md`` § W2-B3 for the spec.

Negative-spec: this package does NOT re-implement the ``coordinator_core
.subagent_sandbox`` resolver/policy-load layer -- it imports
``resolve_effective_types``, ``load_policy``, and ``resolve_git_root``
verbatim from there, the same way ``provision_report.py`` already does.
"""

from __future__ import annotations

from coordinator_core.dispatch.provision import provision_subagent_sidecar

__all__ = ["provision_subagent_sidecar"]
