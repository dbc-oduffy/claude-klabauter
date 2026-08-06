"""
coordinator_core.ops.memo.tests.test_memo_transition_classification —
classification and HTTP-403 dispatch-gate tests for the memo.transition op.

Coverage:
  (1) classify("memo.transition") returns OpClass.MUTATING.
  (2) memo.transition dispatched over the HTTP invoke path with a RW token is
      refused at gate 6 (requires_single_writer_queue) → HTTP 403.

Test convention: mirrors test_authz_contract.py (classify) and
test_http_invoke.test_mutating_op_rw_token_returns_403 (403 gate).

Spec backlink: docs/plans/2026-07-05-strang-09-memo-transition-op-strangle.md § C2
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Registry non-vacuity (mirrors test_http_invoke.py guard — prevents silent
# "method not found" false-negatives from an empty registry).
# ---------------------------------------------------------------------------

import coordinator_core.ops  # noqa: F401 — side-effect: populates _REGISTRY
import coordinator_core.ipc as _ipc

assert len(_ipc._REGISTRY) >= 2, (
    f"_REGISTRY must have >=2 ops after importing coordinator_core.ops; "
    f"got {len(_ipc._REGISTRY)}: {sorted(_ipc._REGISTRY)}"
)

from coordinator_core.authz.classification import OpClass, classify  # noqa: E402


# ---------------------------------------------------------------------------
# (1) Classification — classify("memo.transition") == OpClass.MUTATING
# ---------------------------------------------------------------------------

class TestMemoTransitionClassification:
    def test_classify_memo_transition_is_mutating(self) -> None:
        """classify("memo.transition") must return OpClass.MUTATING."""
        assert classify("memo.transition") is OpClass.MUTATING

    def test_memo_transition_in_op_classification(self) -> None:
        """memo.transition must be present in OP_CLASSIFICATION (drift-guard complement)."""
        from coordinator_core.authz.classification import OP_CLASSIFICATION
        assert "memo.transition" in OP_CLASSIFICATION, (
            "memo.transition missing from OP_CLASSIFICATION — "
            "add an entry in coordinator_core/authz/classification.py"
        )

    def test_memo_transition_in_worktree_scoped_ops(self) -> None:
        """memo.transition must appear in WORKTREE_SCOPED_OPS (show_top scope enrolled)."""
        from coordinator_core.ipc import WORKTREE_SCOPED_OPS
        assert "memo.transition" in WORKTREE_SCOPED_OPS, (
            "memo.transition missing from WORKTREE_SCOPED_OPS — "
            "add a 'show_top' entry in _OP_KEY_SCOPE in coordinator_core/ipc.py"
        )


# (2) HTTP 403 dispatch-gate test (test_memo_transition_rw_token_returns_403) removed by C5
# (DR-215): the HTTP invoke transport was retired. Gate 6 (single-writer-queue) enforcement
# via _dispatch_line is no longer present in the command-type engine.
# Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
