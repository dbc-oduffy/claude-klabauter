"""coordinator_core.contract.decision_object -- the Tier-B decision-object envelope.

Distinct family from `coordinator_core.contract.cockpit_schema` (the pydantic
producer-contract tree). This subpackage is deliberately stdlib-light: no
pydantic, asyncio, ipc, or `cockpit_schema` imports anywhere below this point,
so importing it stays cheap on the brief-path (see the W0-1 import-cost win
this subpackage was carved out to protect).

The envelope shape (8 top-level keys) conforms to the coordinator-claude-side schema-of-record
`schemas/decision-object.schema.json` (DR-047) -- that file is the contract of
truth; this package encodes its key set as `ENVELOPE_KEYS` in `envelope.py`
rather than coupling to the cross-repo path.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md (Wave 1,
chunk W1-A2). [DEAD-CITATION: plan file never committed to this repo]
"""

from coordinator_core.contract.decision_object.envelope import (
    ENVELOPE_KEYS,
    DecisionObjectError,
    ExitCodeBase,
    build_envelope,
    emit,
)
from coordinator_core.contract.decision_object.judgment import (
    build_judgment_point,
    build_untrusted_gate_judgment_point,
)

__all__ = [
    "ENVELOPE_KEYS",
    "DecisionObjectError",
    "ExitCodeBase",
    "build_envelope",
    "emit",
    "build_judgment_point",
    "build_untrusted_gate_judgment_point",
]
