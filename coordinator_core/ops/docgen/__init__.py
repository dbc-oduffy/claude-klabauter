"""coordinator_core.ops.docgen — document-generation strangle (strang-12 beachhead).

Package marker for the claude-klabauter-side extraction of example-doctrine-repo's ``coordinator-doc-new``
(~3200 lines) scaffolders into a declarative, consume-as-data template format.

This package registers ZERO IPC ops (see DR-221 / this plan's C1-C7): every
module here is an internal library — ``types``/``render`` are plain functions,
not ``@register_op`` verbs. The terminal filesystem write (``doc.scaffold``,
the only op this family ships) is out of scope until plan 2 (D1).

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C1-C7
Spec backlink: docs/decisions/DR-221-doc-generation-strangler-target.md
Oracle (byte-identity target): /coordinator/bin/coordinator-doc-new in the example-doctrine-repo
clone (resolved live via coordinator_core.ops.emit.doe_drift.resolve_doe_clone()
— no vendoring, so both sides cannot drift together unnoticed).
"""
