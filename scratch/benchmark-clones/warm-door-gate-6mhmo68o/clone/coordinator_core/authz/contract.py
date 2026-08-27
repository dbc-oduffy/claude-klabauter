"""
coordinator_core.authz.contract — vacated two-tier token authorization surface.

Negative-spec (DR-215/C8 — vacated):
    The two-tier token matrix (Scope.READ_ONLY / Scope.READ_WRITE), the authorization
    predicate (is_authorized), and the single-writer-queue predicate
    (requires_single_writer_queue) are vacated by DR-215 / C8. The resident-daemon
    UDS+HTTP transport surfaces they guarded are retired; command-type execution is
    serial-by-construction so neither token auth nor a single-writer queue is needed
    on the surviving hot path.

    Callers that imported Scope / is_authorized / requires_single_writer_queue from
    this module (invoke/dispatch.py, ipc.py) are cleaned up by sibling chunks C2/C5.

    The MUTATING/COMPUTE_ONLY op-classification framework (coordinator_core.authz.classification)
    SURVIVES as retained metadata — it is transport-independent. Note: dispatch_message does NOT
    call classify(); classification is metadata, not an enforcement gate on the in-process path.

    Re-introduction path (DR-215 § 6): a future cross-language live caller re-adds a
    ~150 LOC transport shim; Scope + is_authorized can be revived alongside it.

Spec backlink: pln-pcore-05-invoke-op-write-seman-80eecd § C1
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md (vacated in part by DR-215/C8)
"""
