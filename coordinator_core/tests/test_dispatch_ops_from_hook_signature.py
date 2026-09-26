
from __future__ import annotations

import inspect

from coordinator_core import ipc


def test_dispatch_ops_from_hook_signature_has_no_warm_decision_parameter():
    signature = inspect.signature(ipc.dispatch_ops_from_hook)

    assert list(signature.parameters) == ["ops", "origin_worktree"]
    assert signature.parameters["origin_worktree"].kind == inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["origin_worktree"].default is None
