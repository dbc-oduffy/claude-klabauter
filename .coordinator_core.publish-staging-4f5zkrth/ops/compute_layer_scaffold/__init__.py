"""coordinator_core.ops.compute_layer_scaffold — the Sub-shape B producer emitter.

Purpose: composes the TEXT of a `<skill>_assemble` decision-object producer
module from a declared verb set, the same emission shape as
`coordinator_core.ops.workflow_scaffold::_compose_script` (f-string
concatenation, not a reusable template engine — copying the shape is the
intended reuse; importing that module is explicit anti-scope, see the
emitter module's own docstring).

Spec backlink: pln-the-compute-layer-scaffolder-e-90d036, chunk C1.
Spike evidence: docs/research/spike-verdicts/2026-08-13-compute-layer-scaffolder-emits-conformant-producer.md
"""

from __future__ import annotations
