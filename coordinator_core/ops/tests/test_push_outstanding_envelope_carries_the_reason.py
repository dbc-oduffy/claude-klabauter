"""
coordinator_core.ops.tests.test_push_outstanding_envelope_carries_the_reason —
pins that `push.outstanding`'s JSON-RPC envelope never reports a FAILED push
as an unexplained exit code.

The handler used to flatten `exit_code`/`acted`/`skipped` only. A failed push
therefore serialized as
`{"exit_code": 1, "acted": [], "skipped": ["push:lfs-range-clean"]}` — whose
only legible content is the AC7b LFS verdict, which is informational and
unrelated to the failure, while `condense_git_diagnostic`'s output sat in the
discarded `failed`. Read twice in one session as an ordinary no-op.

Negative-spec:
    - Does NOT assert `failed` and `unconfirmed` are ever both populated —
      `PushOutcome` documents them as mutually exclusive.
    - Does NOT assert diagnostic TEXT — only that the reason survives the
      flattening at all.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.ops.push_outstanding as po
from coordinator_core.ops.ceremony.commit_pipeline import PushOutcome


def _envelope(monkeypatch, outcome: PushOutcome) -> dict:
    monkeypatch.setattr(po, "push_outstanding", lambda *a, **k: outcome)
    monkeypatch.setattr(po, "main_worktree_root", lambda p: Path(p))
    return po._handler({}, repo_root=Path("X:/nonexistent"))


def test_a_failed_push_carries_its_reason(monkeypatch):
    """The regression: exit 1 must not arrive with the LFS verdict as its
    only content."""
    env = _envelope(
        monkeypatch,
        PushOutcome(
            exit_code=1,
            failed=["git push: rejected (non-fast-forward)"],
            skipped=["push:lfs-range-clean"],
        ),
    )

    assert env["exit_code"] == 1
    assert env["failed"] == ["git push: rejected (non-fast-forward)"]


def test_an_indeterminate_push_is_not_reported_as_a_confirmed_failure(monkeypatch):
    """`unconfirmed` decides whether re-pushing is safe — it must reach the
    caller, and must not be merged into `failed`."""
    env = _envelope(
        monkeypatch,
        PushOutcome(exit_code=1, unconfirmed=["git push: timed out, outcome unobserved"]),
    )

    assert env["unconfirmed"] == ["git push: timed out, outcome unobserved"]
    assert env["failed"] == []


def test_a_policy_decline_carries_the_branch_gate_message(monkeypatch):
    """`message` is `branch_gate()`'s verbatim text; the two surfaces must
    not drift, so it cannot be dropped and reworded downstream."""
    env = _envelope(
        monkeypatch,
        PushOutcome(
            exit_code=0,
            skipped=["push:branch-policy"],
            message="refusing to push protected branch 'main'",
        ),
    )

    assert env["message"] == "refusing to push protected branch 'main'"


def test_a_landed_push_carries_its_range(monkeypatch):
    """`pushed_range`/`pushed_count` are read together — AC7's evidence that
    a push landed and what it landed."""
    env = _envelope(
        monkeypatch,
        PushOutcome(exit_code=0, acted=["push"], pushed_range="aaa..bbb", pushed_count=3),
    )

    assert env["acted"] == ["push"]
    assert env["pushed_range"] == "aaa..bbb"
    assert env["pushed_count"] == 3
