"""
coordinator_core.ops.tests.test_session_hierarchy_derive_repo_root — proves
``session_hierarchy_derive._run`` threads the resolved engine worktree root
into ``derive(..., repo_root=...)`` at the production call site.

Purpose: chunk C6b2 landed ledger-first resolution inside ``derive()``'s
``_claimed_by`` helper behind an optional ``repo_root`` kwarg, but its only
caller (``session_hierarchy_derive._run``) did not pass it — so the fix was
inert in production despite passing tests. This test asserts on the CALL
(``mock.assert_called_once_with(..., repo_root=<worktree_root>)``), not
merely that the op still runs, per the C6b2wire dispatch brief.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3 § C6b2wire

Negative-spec:
- Does NOT re-test ``derive()``'s own ledger-first resolution logic — that is
  ``coordinator_core/session_hierarchy/tests/test_derive_claim_state.py``'s
  job.
- Does NOT exercise the JSON-RPC handler path — patches at the ``_run``/
  ``derive`` call boundary directly, since ``_run`` is the shared code path
  for both the handler and the CLI ``main()``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from coordinator_core.ops import session_hierarchy_derive as sut


def test_run_threads_worktree_root_into_derive_repo_root(tmp_path: Path) -> None:
    worktree_root = tmp_path / "engine-checkout"
    (worktree_root / "state").mkdir(parents=True)

    with mock.patch.object(
        sut, "query_records", return_value=[]
    ) as mock_query, mock.patch.object(
        sut, "derive", return_value=[]
    ) as mock_derive, mock.patch.object(
        sut, "_atomic_write_json"
    ):
        sut._run(worktree_root)

    assert mock_query.call_count == 2

    mock_derive.assert_called_once()
    args, kwargs = mock_derive.call_args
    assert kwargs.get("repo_root") == worktree_root, (
        "derive() must be called with repo_root=worktree_root — the whole "
        "point of C6b2wire is that the production call site threads the "
        "already-resolved engine worktree root through, rather than "
        "leaving derive() to fall back to cwd-relative resolution."
    )
