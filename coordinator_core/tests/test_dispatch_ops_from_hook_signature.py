"""Signature pin for `coordinator_core.ipc.dispatch_ops_from_hook` (AC7 of
`docs/plans/2026-08-23-no-hook-fire-pays-an-interpreter-start.md`).

AC7's claim: no new parameter reaches `dispatch_ops_from_hook` from any
production call site -- the warm decision is read from INSIDE the function,
never passed in. A parameter pinned here fails loudly the moment a future
change tries to widen this signature to take a warm-decision argument.

Negative-spec, stated plainly rather than left implicit: this test pins only
OUR side of the function -- its own definition in this repo. AC7 also names
"any production call site" and "the stub signatures its tests pin" as things
to verify against DoE-claude's hook scripts, which are that repo's live
callers of this function and are not reachable from here. This test cannot
and does not close that half of the criterion.
"""

from __future__ import annotations

import inspect

from coordinator_core import ipc


def test_dispatch_ops_from_hook_signature_has_no_warm_decision_parameter():
    signature = inspect.signature(ipc.dispatch_ops_from_hook)

    assert list(signature.parameters) == ["ops", "origin_worktree"]
    assert signature.parameters["origin_worktree"].kind == inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["origin_worktree"].default is None
