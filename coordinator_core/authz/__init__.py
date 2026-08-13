"""
coordinator_core.authz — invoke-op write-semantics classification.

Purpose: Provides the op-class taxonomy (OpClass), caller-scope taxonomy (Scope),
the classification registry (OP_CLASSIFICATION), and the classification predicate
(classify) that downstream dispatch layers consume to gate op dispatch.

Negative-spec (DR-215/C8 — vacated surface):
    Scope, is_authorized, requires_single_writer_queue, and the token provisioning
    surface (token_path, token_ro_path, write_tokens, read_token, read_token_ro,
    resolve_scope) are vacated by DR-215 / C8. The resident-daemon UDS+HTTP transport
    surfaces they guarded are retired. Callers in invoke/dispatch.py and ipc.py are
    cleaned up by sibling chunks C2/C5.

    The surviving surface is the op-class classification framework only — it is
    transport-independent retained metadata. Note: dispatch_message does NOT call
    classify(); classification is not an enforcement gate on the in-process path.

Spec backlink: pln-pcore-05-invoke-op-write-seman-80eecd
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md
               docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
"""

from __future__ import annotations

from coordinator_core.authz.classification import (
    OpClass,
    OP_CLASSIFICATION,
    classify,
)

__all__ = [
    "OpClass",
    "OP_CLASSIFICATION",
    "classify",
]
