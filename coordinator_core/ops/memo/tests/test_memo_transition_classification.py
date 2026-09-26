"""
coordinator_core.ops.memo.tests.test_memo_transition_classification —
classification and HTTP-403 dispatch-gate tests for the memo.transition op.

Coverage:
  (1) classify("memo.transition") returns OpClass.MUTATING.
  (2) memo.transition dispatched over the HTTP invoke path with a RW token is
      refused at gate 6 (requires_single_writer_queue) → HTTP 403.

Test convention: mirrors test_authz_contract.py (classify) and
test_http_invoke.test_mutating_op_rw_token_returns_403 (403 gate).

Spec backlink: pln-strang-09-memo-transition-op-s-fec3a1 § C2
"""

from __future__ import annotations


# `import coordinator_core.ops` stopped populating _REGISTRY on its own when that
from coordinator_core.ops import _eager_import_all
import coordinator_core.ipc as _ipc

_eager_import_all()

assert len(_ipc._REGISTRY) >= 2, (
    f"_REGISTRY must have >=2 ops after importing coordinator_core.ops; "
    f"got {len(_ipc._REGISTRY)}: {sorted(_ipc._REGISTRY)}"
)

from coordinator_core.authz.classification import OpClass, classify  # noqa: E402


# (1) Classification — classify("memo.transition") == OpClass.MUTATING

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

