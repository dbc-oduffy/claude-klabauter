"""
coordinator_core.execute_plan_assemble.tests.test_close_out_and_stamp_publishes
-- pins `close_out_and_stamp.py`'s REAL `run_commit_pipeline` call boundary
(the module's own `elif wrote_anything:` branch, `close_out_and_stamp.py`
~line 2307) against a genuine remote-tracking ref, never a mocked
`PipelineResult`.

Why this file exists (C5, docs/plans/2026-08-25-push-re-homes-onto-the-
cadence-surfaces.md, AC6): `test_close_out_and_stamp.py`'s own commit-leg
tests already run the REAL `run_commit_pipeline` (no mocked `PipelineResult`
anywhere in that file), but none of them ever configure an actual remote,
so none can observe whether the op's own push leg (or deliberate absence of
one) ever reaches a remote-tracking ref. Before this file, a caller that
silently stopped publishing at all -- a real regression, not merely a
`pushed=None`/`push_status` field mutation -- would never be observed by any
existing test, mocked or otherwise.

Disposition pinned here, DR-329 § 7 (`docs/decisions/DR-329-push-runs-on-a-
cadence-not-on-every-commit.md`): `close_out_and_stamp.py`'s call site is not
one of the six named cadence surfaces, so it no longer owns a synchronous
push at all -- it now passes `push_mode=PUSH_MODE_NEVER` explicitly (never by
omission), and publication is deferred to whichever cadence checkpoint runs
next. AC6 itself is carried by `TestPublishBoundaryCurrentContract` above
alone -- it calls `coas.close_out_and_stamp` directly and is the class that
would fail if the fix at that call site were reverted.
`TestPreFixCallShapeContrastCase` below does NOT call `close_out_and_stamp`
at all (see its own docstring): it hand-constructs the PRE-C5
`run_commit_pipeline` call shape -- a synchronous in-pipeline push, spelled
`push_mode=PUSH_MODE_SYNC` explicitly since 2026-08-26, when the parameter's
default became `PUSH_MODE_NONE` and omission stopped meaning sync (pinned by
`ops/ceremony/tests/test_push_mode_default_contract.py`) -- and shows the
same remote-ref assertion flips under that shape. That is a characterisation
of `run_commit_pipeline`'s own synchronous-push behaviour, not a second,
independent regression pin on `close_out_and_stamp`'s real call site -- a
future edit that reverts `close_out_and_stamp.py`'s own call to omit
`push_mode` again would NOT be caught by this class, only by
`TestPublishBoundaryCurrentContract`.

Negative-spec: this file does not exercise `push_outstanding()` or any of
the six cadence surfaces' own checkpoint calls -- those are C4b's own
call-site wiring and are pinned by that chunk's own tests. This file's only
subject is what `close_out_and_stamp.py`'s own `run_commit_pipeline` call
does or does not do to a remote, nothing past that boundary.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.ops.ceremony import commit_pipeline as cp

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = (
    _REPO_ROOT / "coordinator" / "bin" / "tests" / "fixtures" / "plan-tasks-spine"
)
_FIXTURE_VALID_SPINE = _FIXTURES_DIR / "valid-spine-with-deferrals.md"
_DLV_VALID_SPINE = "dlv-fixture-valid-spine-000001"

_WORK_BRANCH = "work/publish-boundary-test/2026-08-25"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo_with_real_remote(root: Path, remote: Path) -> None:
    """Builds a genuine bare `remote` and a `root` worktree tracking it on
    `_WORK_BRANCH` -- `auto_push.branch_gate()` (and `push_with_retry`'s own
    same gate) proceeds ONLY on a `work/*` branch, so this is load-bearing,
    not cosmetic: a `main`-checkout fixture would make every push in this
    file decline for a reason unrelated to the one under test."""
    remote.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q", "--bare"], remote)

    root.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@t"], root)
    _run_git(["config", "user.name", "test"], root)
    _run_git(["checkout", "-q", "-b", _WORK_BRANCH], root)
    _run_git(["remote", "add", "origin", str(remote)], root)


def _seed_plan(root: Path, fixture_path: Path, dest_name: str = "plan.md") -> Path:
    dest = root / dest_name
    dest.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


def _commit_chunk(
    root: Path, plan_rel: str, chunk_id: str, *, deliverable_id: Optional[str] = None
) -> None:
    plan_file = root / plan_rel
    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {chunk_id} landed -->\n")
    _run_git(["add", plan_rel], root)
    message_args = ["-m", f"{chunk_id}: land chunk"]
    if deliverable_id:
        message_args += ["-m", f"Deliverable-Id: {deliverable_id}"]
    _run_git(["commit", "-q", *message_args], root)


def _push_initial_state(root: Path) -> None:
    """Publishes the seed history to `origin` and wires the upstream
    tracking ref BEFORE the op under test runs -- so any push this op's own
    call leg issues is observable as an ADVANCE of `refs/remotes/origin/
    <branch>`, not merely the ref's first-ever appearance."""
    _run_git(["push", "-q", "-u", "origin", _WORK_BRANCH], root)


def _origin_head_sha(root: Path) -> str:
    """Resolves `origin/<_WORK_BRANCH>` by asking the bare `remote` DIRECTLY
    (`git ls-remote`), never `root`'s own cached `refs/remotes/origin/*` --
    that cached ref only advances when THIS worktree itself fetches, so
    reading it would silently pass regardless of whether a push actually
    reached the remote. `run_commit_pipeline`'s push leg (when it runs) is
    the only thing in this test that can move the remote's own tip."""
    result = _run_git(["ls-remote", "origin", _WORK_BRANCH], root)
    line = result.stdout.strip()
    return line.split("\t", 1)[0] if line else ""


def _head_sha(root: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], root).stdout.strip()


def _run_close_out(monkeypatch: pytest.MonkeyPatch, root: Path, plan_rel: str):
    monkeypatch.chdir(root)
    return coas.close_out_and_stamp(plan_rel, repo_root=root)


class TestPublishBoundaryCurrentContract:
    """Pins the REAL call boundary's post-DR-329 contract: `close_out_and_
    stamp`'s own commit leg lands a local commit but issues no synchronous
    push -- publication is deferred to a cadence checkpoint, never to this
    call site."""

    def test_commit_lands_locally_but_does_not_reach_the_remote_tracking_ref(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "work"
        remote = tmp_path / "remote.git"
        _init_repo_with_real_remote(root, remote)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        # C2a/C2b deliberately left uncommitted -- halted path, AC8
        # auto-resolve is what makes `wrote_anything` True and reaches this
        # op's own `run_commit_pipeline` call (never the plan-status-
        # transition-already-committed short-circuit -- see
        # `test_close_out_and_stamp.py`'s sibling test this scenario is
        # copied from).
        _push_initial_state(root)
        pre_call_head = _head_sha(root)
        pre_call_origin_head = _origin_head_sha(root)
        assert pre_call_origin_head == pre_call_head

        exit_code, result = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["commit"]["commit_failed"] is False
        assert result["commit"]["committed_sha"] is not None
        # A real local commit landed...
        assert _head_sha(root) != pre_call_head
        assert result["commit"]["committed_sha"] == _head_sha(root)
        # ...but it did not reach the remote-tracking ref synchronously --
        # the DR-329 disposition this test pins: `close_out_and_stamp` is
        # not a cadence surface, so it no longer pushes at all, and
        # publication is left to the next cadence checkpoint's own
        # `push_outstanding()` call.
        assert _origin_head_sha(root) == pre_call_origin_head
        assert _origin_head_sha(root) != _head_sha(root)
        assert result["commit"]["push_status"] == cp.PUSH_STATUS_NOT_ATTEMPTED
        assert result["commit"]["pushed"] is None


class TestPreFixCallShapeContrastCase:
    """Review: coordinator:code-reviewer -- does NOT call `coas.close_out_
    and_stamp` and is not itself a regression pin on that call site; AC6 is
    carried entirely by `TestPublishBoundaryCurrentContract` above. This
    class instead characterises `cp.run_commit_pipeline`'s OWN behaviour
    under a synchronous in-pipeline push (the pre-C5 `close_out_and_stamp.py`
    call shape, reproduced here by hand): it shows the SAME remote-ref
    assertion `TestPublishBoundaryCurrentContract` makes about the post-fix
    call shape is false under the sync shape, as a side-by-side contrast case
    for a reader comparing the two. A future edit that drops
    `close_out_and_stamp.py`'s own explicit `push_mode=PUSH_MODE_NEVER`
    argument would NOT be caught here -- only `TestPublishBoundaryCurrent
    Contract`, which calls the real module, would fail."""

    def test_pre_fix_call_shape_synchronously_reaches_the_remote(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "work"
        remote = tmp_path / "remote.git"
        _init_repo_with_real_remote(root, remote)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        _push_initial_state(root)
        pre_call_origin_head = _origin_head_sha(root)

        # Dirty `plan.md` uncommitted -- the real call boundary always has
        # SOMETHING dirty on `stage_paths` at the point it reaches
        # `run_commit_pipeline` (AC8's auto-resolve write, in the module's
        # own case); a clean `stage_paths` here would hit git's own
        # "nothing to commit" refusal for a reason unrelated to the one
        # under test.
        with plan_file.open("a", encoding="utf-8") as fh:
            fh.write("\n<!-- red-proof dirty -->\n")

        # Locally reproduce the PRE-C5 call shape: `run_commit_pipeline`
        # taking a synchronous in-pipeline push -- exactly
        # `close_out_and_stamp.py`'s call shape before this chunk's fix. It
        # got there by omission then; omission now yields `PUSH_MODE_NONE`,
        # so the shape under test is named rather than inherited. Reached the
        # same way the module reaches it
        # (`_stage_paths_committed_already` False, AC8 auto-resolve dirties
        # `stage_paths`), so this is the real boundary, not a synthetic one.
        session_id = "publish-boundary-red-proof"
        pipeline_result = cp.run_commit_pipeline(
            root,
            session_id=session_id,
            subject="red-proof: pre-C5 sync-by-omission call shape",
            stage_paths=["plan.md"],
            caller_paths={"plan.md"},
            push_mode=cp.PUSH_MODE_SYNC,
        )

        assert pipeline_result.commit_failed is False
        assert pipeline_result.committed_sha is not None
        # The pre-fix call shape DOES reach the remote synchronously -- a
        # contrast case only, not a call through `close_out_and_stamp`: the
        # same assertion `TestPublishBoundaryCurrentContract` makes about
        # the post-fix call shape is FALSE here, by construction.
        assert _origin_head_sha(root) != pre_call_origin_head
        assert _origin_head_sha(root) == _head_sha(root)
        assert pipeline_result.push_status == cp.PUSH_STATUS_PUSHED
