"""coordinator_core.ops.docgen — document-generation strangle (strang-12 beachhead).

Package marker for the claude-klabauter-side extraction of DoE's ``coordinator-doc-new``
(~3200 lines) scaffolders into a declarative, consume-as-data template format.

This package registers ZERO IPC ops (see DR-221 / this plan's C1-C7): every
module here is an internal library — ``types``/``render`` are plain functions,
not ``@register_op`` verbs. The terminal filesystem write (``doc.scaffold``,
the only op this family ships) is out of scope until plan 2 (D1).

Spec backlink: pln-strang-12-document-generation--75a7eb § C1-C7
Spec backlink: docs/decisions/DR-221-doc-generation-strangler-target.md
Oracle (byte-identity target): /coordinator/bin/coordinator-doc-new.py in the DoE
clone (resolved live via coordinator_core.ops.emit.doe_drift.resolve_doe_clone()
— no vendoring, so both sides cannot drift together unnoticed).
"""
