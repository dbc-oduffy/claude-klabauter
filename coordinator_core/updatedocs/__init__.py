"""
coordinator_core.updatedocs — deterministic drift detectors for the /update-docs ceremony.

Purpose: read-only compute modules backing the doc-index and prune-candidate gates
registered in `coordinator_core.ops.updatedocs_gates`. Each module exposes one pure
function over repo substrate and returns a structured result; none writes repo
substrate, and none constructs a `GateResult` — the gate layer owns verdict mapping,
so that "the target is missing" can become UNAVAILABLE rather than CLEAN at exactly
one place.

Negative spec: nothing here regenerates `docs/README.md` or a `DIRECTORY.md`. Those
files interleave deterministic index rows with hand-written prose; the mechanical half
is detecting the drift, and the prose half stays with a model. A writer added here
would destroy the prose or have to reproduce the judgment that wrote it.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa
"""

from __future__ import annotations
