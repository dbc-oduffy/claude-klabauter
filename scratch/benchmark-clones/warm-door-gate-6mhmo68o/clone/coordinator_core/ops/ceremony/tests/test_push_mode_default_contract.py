"""What an OMITTING caller gets from `run_commit_pipeline`'s `push_mode`.

The default is a live contract, not a formality: `ceremony.commit` forwards
`push_mode` only when its caller supplies one, so whatever this default says is
what every cross-repo caller that omits the param actually gets. Two ways for
that to be wrong, and this module pins both, because a fix for either one alone
lands you in the other:

  - Too eager (`sync`, the default until 2026-08-26) put a SYNCHRONOUS push
    inside a `ceremony.*` op, bounded only by `ipc.CEREMONY_BUDGET_SECS` (2.0s).
    That clamp is `asyncio.wait_for` -- wall clock -- so it can fire only
    mid-leg, inside a `git push` whose outcome is then never observed. A
    dispatch timeout does not abort server-side execution, so the push may
    still land afterwards: the caller is left with `unconfirmed`, which is the
    worst of the three states. Nobody chose that for those callers; they got it
    by omission.
  - Too shy (`never`) ALSO stands the `post-commit` hook's detached push down,
    so the commit would reach no remote at all -- silently unpublished, which
    is worse than an honest failure.

`none` is the one value that is neither: it skips the in-pipeline push and
leaves the hook as publisher, off the ceremony's critical path and under no 2s
clamp. A caller that genuinely must not publish still says `never` explicitly,
and every in-repo caller that means it already does.

Kept out of `test_commit_pipeline.py` deliberately -- that module is ~5,900
lines about staging, gates and the commit ladder, and this is a two-assertion
contract about one parameter's default that reads better where a reader can
find it by name.

Spec backlink: `commit_pipeline.py`'s `PUSH_MODE_*` comment block.
Related: `PUSH_RETRY_BUDGET_SECS` / `CEREMONY_PUSH_BUDGET_SECS` in the same file.
"""

from __future__ import annotations

import inspect

from coordinator_core.ops.ceremony import commit_pipeline as commit_pipeline_mod


def _default_push_mode() -> str:
    return inspect.signature(
        commit_pipeline_mod.run_commit_pipeline
    ).parameters["push_mode"].default


def test_run_commit_pipeline_does_not_push_synchronously_by_default():
    """No in-pipeline push for an omitting caller.

    Asserted as "not sync" plus membership rather than `== "none"` on purpose:
    the property that matters is that no synchronous push runs by default, and
    a future, still correct, move between `none` and `never` should not fail
    here. The half that pins WHICH of the two is the next test, which has its
    own reason.
    """
    default = _default_push_mode()
    assert default != commit_pipeline_mod.PUSH_MODE_SYNC, (
        "run_commit_pipeline defaults to a synchronous push again -- an omitting "
        "ceremony.commit caller is back inside the 2.0s ceremony clamp, where a "
        "timeout can only fire mid-push and yields an unconfirmed push"
    )
    assert default in {
        commit_pipeline_mod.PUSH_MODE_NONE,
        commit_pipeline_mod.PUSH_MODE_NEVER,
    }


def test_default_push_mode_leaves_a_publisher_standing():
    """...and the default must still leave SOMEONE publishing the commit.

    Read live from the pipeline's own hook-suppressing set rather than restated
    here, so that set growing a new member is caught instead of silently making
    this assertion weaker than it looks.
    """
    default = _default_push_mode()
    assert (
        default not in commit_pipeline_mod._PUSH_MODES_SUPPRESSING_POST_COMMIT_HOOK
    ), (
        f"default push_mode {default!r} stands the post-commit hook down, so an "
        f"omitting caller's commit would be published by nobody at all"
    )
