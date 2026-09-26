"""
coordinator_core.authz.completion_evidence — what a terminal ack may claim
about a MUTATING op's own mutation, beyond "the handler returned".

Ratified by docs/decisions/DR-442-completion-evidence-is-the-engine-s-record-not-the-side-effect.md,
extending the 2026-09-09 ruling (DoE-claude state/rulings/2026-09-09-staff-eng-rulings-run-c0d7111e.md
§ Row M11). Sparse by design: only ops for which a terminal stamp proves LESS than complete
evidence are declared. An op absent from the registry is `ack` -- the terminal stamp is
complete evidence its mutation happened -- and restating that default is refused (see the guard
test). An unregistered, unrecognized, or lookup-failure case renders `UNDECLARED`, the
fail-closed answer: the stamp asserts nothing beyond the box, and the caller must not re-run.

No imports beyond `enum` and `types`. Never imported on the warm client's happy path -- only
from `_indeterminate_envelope`'s failure branch, matching `_op_may_mutate`'s own
lazy-on-failure-path-only pattern.
"""

from __future__ import annotations

import enum
import types


class EvidenceClass(enum.Enum):
    """What a terminal ack stamp may claim about a MUTATING op's mutation.

    ACK           -- (not a registry value; the implicit default for an
                      undeclared-but-classified op) every write completes
                      synchronously, on-box, before the handler returns. The
                      terminal stamp is complete evidence.
    FIRE_AND_FORGET -- the effect lands off-box or after the handler returns.
                      The result is telemetry: it may report an unobserved
                      outcome, never an instruction to reconcile or re-run.
    UNDECLARED    -- fail-closed. No declaration exists for this op (or the
                      lookup itself failed). The stamp asserts nothing beyond
                      the box; do not re-run.
    """

    FIRE_AND_FORGET = "fire_and_forget"
    UNDECLARED = "undeclared"


# MUTATING op whose handler either cannot observe a remote outcome, or
OP_COMPLETION_EVIDENCE: "types.MappingProxyType[str, EvidenceClass]" = types.MappingProxyType(
    {
        "push.outstanding": EvidenceClass.FIRE_AND_FORGET,
        "app_session.launch": EvidenceClass.FIRE_AND_FORGET,
        "app_session.teardown": EvidenceClass.FIRE_AND_FORGET,
    }
)


def evidence_class(op) -> EvidenceClass:
    """The declared evidence class for `op`, or `UNDECLARED` (fail-closed).

    A dict lookup and nothing more -- no filesystem read, no git spawn, no
    side-effect probe. Any non-hashable or unexpected `op` value degrades to
    `UNDECLARED` rather than raising, matching the fail-closed discipline
    every caller of this module relies on.
    """
    try:
        return OP_COMPLETION_EVIDENCE.get(op, EvidenceClass.UNDECLARED)
    except TypeError:
        return EvidenceClass.UNDECLARED
