"""
coordinator_core.ops.ceremony.tests.test_post_commit_tail

Op-level tests for `coordinator_core/ops/ceremony/post_commit_tail.py` — the
C3a (docs/plans/2026-07-23-wsc-tail-slim-down.md § C3a) extraction of
`wsc_tail.py`'s steps 5c (post-commit consumed-handoff stamp+ship) and 5d
(origin-stub close) into ONE standalone REGISTERED op,
`ceremony.post_commit_tail`.

This module is a pure refactor — `wsc_tail.py`'s own existing test suites
(`test_wsc_tail_parity.py`, `test_consumed_handoff_stamp.py`) already cover
the composed steps' full end-to-end behavior against real git repos; those
suites are the acceptance bar (must stay green UNCHANGED). This file instead
covers the NEW op-level surface directly: registration, sequencing/ordering,
the origin-stub-close handler-injection contract, timing-span recording, the
standalone JSON-RPC `_handler` entry, and the no-`ceremony_lock` invariant —
using fakes/monkeypatch rather than a real git fixture, since the underlying
git-touching behavior is already covered elsewhere.

Coverage:
  (a) op_is_registered                        — "ceremony.post_commit_tail"
      resolves via get_op_handler and IS this module's own `_handler`.
  (b) run_sequences_stamp_then_origin_close    — both composed calls happen,
      in order (stamp-ship BEFORE origin-stub-close).
  (c) run_stamp_exception_propagates_before_origin_close_runs — a stamp+ship
      exception propagates out of `run()` and origin-stub-close never runs
      (mirrors AC18 crash-before-stamp-completes semantics).
  (d) run_origin_close_failure_does_not_propagate — an injected handler
      exception is soft-failed into the returned `origin_stub_result`, never
      raised past `run()`.
  (e) run_records_two_separate_timing_spans_when_timing_supplied — the C1
      "stamp_and_ship"/"origin_stub_close" step-name contract.
  (f) run_is_noop_safe_without_timing           — `timing=None` (the
      standalone-dispatch shape) records nothing and does not raise.
  (g) handler_requires_sid_and_committed_sha    — setup-error paths.
  (h) handler_requires_repo_root                — setup-error path.
  (i) handler_errors_when_close_origin_stub_unregistered.
  (j) handler_happy_path_dispatches_through_run  — the JSON-RPC entry composes
      `run()` correctly and reports exit_code 0/2 per outcome.
  (k) module_does_not_import_ceremony_lock       — repo-wide AC9
      reintroduction guard (docs/plans/2026-08-07-excise-the-ceremony-lock.md
      § C7), re-pointed here by C7 from the retired DEC-3 per-module scope:
      no module under `coordinator_core/` or `coordinator/bin/` may import,
      dynamically import, or define/call anything named exactly
      `ceremony_lock` (see `_ceremony_lock_guard.py` for exactly what is and
      is not covered). Not scoped to `post_commit_tail` specifically.

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C3a.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core import ipc
from coordinator_core.ops.ceremony import consumed_handoff_stamp
from coordinator_core.ops.ceremony import post_commit_tail as m
from coordinator_core.ops.ceremony.commit_pipeline import PUSH_MODE_NONE, PushOutcome
from ._ceremony_lock_guard import assert_no_ceremony_lock_reintroduction
from .fixtures.real_git import make_diverged_path, real_git_repo


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _committed_content_at_head(repo: Path, rel: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return result.stdout


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_op_is_registered():
    handler = ipc.get_op_handler("ceremony.post_commit_tail")
    assert handler is not None
    assert handler is m._handler


# ---------------------------------------------------------------------------
# (b)-(f) run() composition/sequencing/timing
# ---------------------------------------------------------------------------


class _FakeTiming:
    """Minimal stand-in for `wsc_tail._TailTiming` — records (name, order)
    pairs without any real wall-clock measurement."""

    def __init__(self) -> None:
        self.entered: list[str] = []
        self.exited: list[str] = []

    def measure(self, name: str):
        entered, exited = self.entered, self.exited

        class _Ctx:
            def __enter__(self_inner):
                entered.append(name)
                return None

            def __exit__(self_inner, exc_type, exc, tb):
                exited.append(name)
                return False  # never swallow

        return _Ctx()


def _make_stamp_outcome(**kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
    return consumed_handoff_stamp.StampOutcome(**kwargs)


def test_run_sequences_stamp_then_origin_close(monkeypatch, tmp_path):
    call_order: list[str] = []

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        call_order.append("stamp_and_ship")
        return _make_stamp_outcome(stamped=["state/handoffs/x.md"])

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        call_order.append("origin_stub_close")
        return {"exit_code": 0, "closed": [], "skipped": []}

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="my-plan",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            push_mode="deferred",
        )
    )

    assert call_order == ["stamp_and_ship", "origin_stub_close"]
    assert outcome.stamp_outcome.stamped == ["state/handoffs/x.md"]
    assert outcome.origin_stub_result == {
        "acted": [],
        "skipped": [f"{m.OP_CLOSE_ORIGIN_STUB}:no-op"],
        "failed": [],
    }


def test_run_stamp_exception_propagates_before_origin_close_runs(monkeypatch, tmp_path):
    call_order: list[str] = []

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        call_order.append("stamp_and_ship")
        raise RuntimeError("simulated crash")

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        call_order.append("origin_stub_close")
        return {"exit_code": 0, "closed": [], "skipped": []}

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(
            m.run(
                tmp_path,
                tmp_path,
                "sid-1",
                "deadbeef",
                chain_terminal=True,
                governing_plan_slug="",
                initial_consumed=[],
                close_origin_stub_handler=_fake_close_origin_stub,
            )
        )

    assert call_order == ["stamp_and_ship"], (
        "origin-stub close must never run once stamp+ship has raised"
    )


def test_run_origin_close_failure_does_not_propagate(monkeypatch, tmp_path):
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated handoff.close_origin_stub crash")

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="my-plan",
            initial_consumed=[],
            close_origin_stub_handler=_boom,
        )
    )

    assert outcome.origin_stub_result["acted"] == []
    assert any(
        "simulated handoff.close_origin_stub crash" in f
        for f in outcome.origin_stub_result["failed"]
    )


def test_run_origin_close_surfaces_message_when_no_error_key(monkeypatch, tmp_path):
    """`handoff.close_origin_stub`'s own docstring documents TWO non-zero
    reply shapes: a usage error carries `error`, the loud zero-join no-op
    carries `message` only. `_run_origin_stub_close` must prefer `error`,
    fall back to `message`, and only then to a literal "unknown error" —
    reading only `error` silently discarded the op's explanation and
    surfaced a bare "unknown error" for every message-only reply."""

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        return {
            "exit_code": 1,
            "closed": [],
            "skipped": [],
            "pairs_resolved": 0,
            "message": "no (roadmap_id,stub_id) resolvable from state/handoffs/x.md",
        }

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="my-plan",
            initial_consumed=[("state/handoffs/x.md", {})],
            close_origin_stub_handler=_fake_close_origin_stub,
        )
    )

    assert outcome.origin_stub_result["acted"] == []
    failed = outcome.origin_stub_result["failed"]
    assert any(
        "no (roadmap_id,stub_id) resolvable from state/handoffs/x.md" in f
        for f in failed
    ), failed
    assert not any("unknown error" in f for f in failed), failed


def test_run_skip_rendering_distinguishes_live_children_from_indeterminate(
    monkeypatch, tmp_path
):
    """`_render_skip_entry` must surface `blocking_children`/`guard_error`
    into the rendered skip string, capped at `_MAX_RENDERED_BLOCKING_CHILDREN`
    with a `(+N more)` suffix — and a skip entry carrying neither new field
    (`no-match`) must render exactly as the bare `roadmap:stub:reason` prefix,
    unchanged."""

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        return {
            "exit_code": 0,
            "closed": [],
            "skipped": [
                {
                    "roadmap_id": "r1",
                    "stub_id": "s1",
                    "reason": "guard-declined-live-children",
                    "blocking_children": [
                        "state/handoffs/a.md",
                        "state/handoffs/b.md",
                        "state/handoffs/c.md",
                        "state/handoffs/d.md",
                    ],
                    "guard_error": None,
                },
                {
                    "roadmap_id": "r2",
                    "stub_id": "s2",
                    "reason": "guard-declined-indeterminate",
                    "blocking_children": [],
                    "guard_error": "unscannable subtree: boom",
                },
                {
                    "roadmap_id": "r3",
                    "stub_id": "s3",
                    "reason": "no-match",
                },
            ],
            "pairs_resolved": 3,
            "message": "closed 0 origin stub(s); skipped 3 of 3 resolved pair(s)",
        }

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="my-plan",
            initial_consumed=[("state/handoffs/x.md", {})],
            close_origin_stub_handler=_fake_close_origin_stub,
        )
    )

    skipped = outcome.origin_stub_result["skipped"]
    assert len(skipped) == 3

    live_children_line = skipped[0]
    assert live_children_line.startswith("r1:s1:guard-declined-live-children")
    assert "blocking: state/handoffs/a.md, state/handoffs/b.md, state/handoffs/c.md" in live_children_line
    assert "(+1 more)" in live_children_line
    assert "guard_error" not in live_children_line

    indeterminate_line = skipped[1]
    assert indeterminate_line == "r2:s2:guard-declined-indeterminate guard_error: unscannable subtree: boom"

    no_match_line = skipped[2]
    assert no_match_line == "r3:s3:no-match"


def test_render_skip_entry_exact_boundary_suppresses_suffix_at_cap():
    """Review: code-reviewer — exact-boundary case (exactly
    `_MAX_RENDERED_BLOCKING_CHILDREN` == 3 children, cap not exceeded): the
    `if remaining > 0` guard in `_render_skip_entry` must suppress the
    `(+N more)` suffix entirely, not just at `remaining == 1`."""
    entry = {
        "roadmap_id": "r1",
        "stub_id": "s1",
        "reason": "guard-declined-live-children",
        "blocking_children": [
            "state/handoffs/a.md",
            "state/handoffs/b.md",
            "state/handoffs/c.md",
        ],
        "guard_error": None,
    }
    line = m._render_skip_entry(entry)
    assert line == (
        "r1:s1:guard-declined-live-children "
        "blocking: state/handoffs/a.md, state/handoffs/b.md, state/handoffs/c.md"
    )
    assert "more)" not in line


def test_render_skip_entry_large_fan_out_remaining_is_len_minus_cap():
    """Review: code-reviewer — large fan-out (10 children) confirms
    `remaining` is `len(children) - _MAX_RENDERED_BLOCKING_CHILDREN`, not
    off-by-one."""
    children = [f"state/handoffs/{i}.md" for i in range(10)]
    entry = {
        "roadmap_id": "r1",
        "stub_id": "s1",
        "reason": "guard-declined-live-children",
        "blocking_children": children,
        "guard_error": None,
    }
    line = m._render_skip_entry(entry)
    assert "(+7 more)" in line
    shown = ", ".join(children[: m._MAX_RENDERED_BLOCKING_CHILDREN])
    assert (
        line
        == f"r1:s1:guard-declined-live-children blocking: {shown} (+7 more)"
    )


def test_run_multi_baton_two_distinct_origin_stubs_close_in_one_follow_up_commit(
    monkeypatch, tmp_path
):
    """DEC-5 (docs/plans/2026-07-24-multibaton-pickup-and-args-prose.md § C3):
    a session owning two consumed handoffs deriving from two distinct origin
    stubs must close BOTH stubs, but issue exactly ONE follow-up
    `_to_thread_commit_and_push` call over the unioned `closed_paths` — never
    one follow-up commit per handoff, and never silently truncating to the
    first consumed handoff (the pre-DEC-5 `initial_consumed[0]` defect)."""

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    handoff_paths_seen: list[str] = []

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        handoff_path = params["handoff_path"]
        handoff_paths_seen.append(handoff_path)
        if handoff_path == "state/handoffs/baton-a.md":
            return {
                "exit_code": 0,
                "closed": [{"stub_path": "state/roadmap-stubs/stub-a.md", "roadmap_id": "r-a", "stub_id": "s-a"}],
                "skipped": [],
            }
        if handoff_path == "state/handoffs/baton-b.md":
            return {
                "exit_code": 0,
                "closed": [{"stub_path": "state/roadmap-stubs/stub-b.md", "roadmap_id": "r-b", "stub_id": "s-b"}],
                "skipped": [],
            }
        raise AssertionError(f"unexpected handoff_path {handoff_path!r}")

    follow_up_calls: list[tuple[Path, list[str], str, str]] = []

    async def _fake_to_thread_commit_and_push(
        worktree_root: Path, closed_paths: list[str], committed_sha: str, push_mode: str
    ) -> tuple[Optional[str], Optional[bool], str, Optional[str]]:
        follow_up_calls.append((worktree_root, list(closed_paths), committed_sha, push_mode))
        return ("followupsha", True, m.PUSH_STATUS_PUSHED, None)

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)
    monkeypatch.setattr(m, "_to_thread_commit_and_push", _fake_to_thread_commit_and_push)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[
                ("state/handoffs/baton-a.md", {}),
                ("state/handoffs/baton-b.md", {}),
            ],
            close_origin_stub_handler=_fake_close_origin_stub,
            push_mode="deferred",
        )
    )

    assert handoff_paths_seen == ["state/handoffs/baton-a.md", "state/handoffs/baton-b.md"]
    assert len(follow_up_calls) == 1, "must be exactly one unioned follow-up commit, not one per handoff"
    _worktree_root, closed_paths, committed_sha, push_mode = follow_up_calls[0]
    assert set(closed_paths) == {
        "state/roadmap-stubs/stub-a.md",
        "state/roadmap-stubs/stub-b.md",
    }
    assert committed_sha == "deadbeef"
    assert push_mode == "deferred"
    assert set(outcome.origin_stub_result["acted"]) == {
        "state/roadmap-stubs/stub-a.md",
        "state/roadmap-stubs/stub-b.md",
    }
    assert outcome.origin_stub_result["failed"] == []


def test_run_records_two_separate_timing_spans_when_timing_supplied(monkeypatch, tmp_path):
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        return {"exit_code": 0, "closed": [], "skipped": []}

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    timing = _FakeTiming()
    _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            timing=timing,
        )
    )

    # C6b's deliverable-cascade step deliberately runs untimed -- it must
    # not widen `wsc_tail.py`'s own pinned `_TailTiming` step-name contract
    # (see post_commit_tail.py module docstring "Timing-span preservation").
    assert timing.entered == ["stamp_and_ship", "origin_stub_close"]
    assert timing.exited == ["stamp_and_ship", "origin_stub_close"]


def test_run_is_noop_safe_without_timing(monkeypatch, tmp_path):
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        return {"exit_code": 0, "closed": [], "skipped": []}

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    # Must not raise with timing=None (the standalone/registry-dispatch shape).
    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            timing=None,
        )
    )
    assert outcome.stamp_outcome is not None


# ---------------------------------------------------------------------------
# (g)-(j) standalone JSON-RPC _handler
# ---------------------------------------------------------------------------


def test_handler_requires_sid_and_committed_sha(tmp_path):
    result = _run(m._handler({}, repo_root=tmp_path))
    assert result["exit_code"] == 1
    assert "sid" in result["error"]

    result = _run(m._handler({"sid": "sid-1"}, repo_root=tmp_path))
    assert result["exit_code"] == 1
    assert "committed_sha" in result["error"]


def test_handler_requires_repo_root():
    result = _run(m._handler({"sid": "sid-1", "committed_sha": "deadbeef"}, repo_root=None))
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]


def test_handler_errors_when_close_origin_stub_unregistered(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "get_op_handler", lambda name: None)
    result = _run(
        m._handler({"sid": "sid-1", "committed_sha": "deadbeef"}, repo_root=tmp_path)
    )
    assert result["exit_code"] == 1
    assert "not registered" in result["error"]


def test_handler_happy_path_dispatches_through_run(monkeypatch, tmp_path):
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/x.md"], follow_up_committed_sha="ffff")

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    result = _run(
        m._handler(
            {"sid": "sid-1", "committed_sha": "deadbeef", "chain_terminal": True},
            repo_root=tmp_path,
        )
    )

    assert result["exit_code"] == 0
    assert result["stamped"] == ["state/handoffs/x.md"]
    assert result["follow_up_committed_sha"] == "ffff"
    assert result["origin_stub_close"]["skipped"] == [
        f"{m.OP_CLOSE_ORIGIN_STUB}:no-governing-plan-or-consumed-handoff"
    ]


def test_handler_reports_exit_code_2_on_stamp_error(monkeypatch, tmp_path):
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(errors=[{"path": "x.md", "error": "boom"}])

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    result = _run(
        m._handler(
            {"sid": "sid-1", "committed_sha": "deadbeef", "chain_terminal": True},
            repo_root=tmp_path,
        )
    )

    assert result["exit_code"] == 2


# ---------------------------------------------------------------------------
# (k) DEC-3 HARD CONSTRAINT — never acquires ceremony_lock
# ---------------------------------------------------------------------------


def test_module_does_not_import_ceremony_lock():
    """Repo-wide AC9 reintroduction guard (re-pointed by C7,
    docs/plans/2026-08-07-excise-the-ceremony-lock.md).

    `ceremony_lock.py` was deleted outright by C7 -- the mutex it implemented
    was killed by PM ruling (repeated shared-worktree wedges) and its
    restoration is separately sized, explicitly out of scope for this plan.
    This is the only executable enforcement of the plan's Anti-scope "do NOT
    reimplement a mutex" -- but it enforces exactly one identifier
    (`ceremony_lock`), not the Anti-scope's full "any file, any name" text; a
    mutex reintroduced under a different name is NOT caught here and needs
    plan review to catch. See `_ceremony_lock_guard.py`'s module docstring
    for exactly what is and is not covered, and why."""
    assert not hasattr(m, "CeremonyLockTimeout")
    assert not hasattr(m, "DEFAULT_LOCK_TIMEOUT_SECS")

    repo_root = Path(__file__).resolve().parents[4]
    assert_no_ceremony_lock_reintroduction(repo_root)


# ---------------------------------------------------------------------------
# (l) origin-stub-close follow-up commit routes through commit_scoped — a
# peer's deliberately-staged partial-hunk content on a path in the closed
# set survives verbatim (the claude-klabauter 506748a0 incident shape,
# closed). Real git required (fixtures.real_git) -- divergence cannot be
# exhibited by a mocked git.
# ---------------------------------------------------------------------------


def test_origin_stub_close_follow_up_commit_preserves_peer_staged_divergence(tmp_path):
    repo = real_git_repo(tmp_path)
    make_diverged_path(
        repo, "docs/plans/some-stub.md", staged_content="STAGED\n", worktree_content="WORKTREE\n"
    )

    follow_up_sha, pushed, push_status, error = m._commit_and_push_origin_stub_close(
        repo, ["docs/plans/some-stub.md"], "deadbeef", push_mode=PUSH_MODE_NONE
    )

    assert error is None, error
    assert follow_up_sha is not None
    assert pushed is None  # push_mode="none" -- no attempt, not a failure
    assert push_status == m.PUSH_STATUS_NOT_ATTEMPTED
    assert _committed_content_at_head(repo, "docs/plans/some-stub.md") == "STAGED\n"
    # Worktree content is untouched -- commit_scoped never re-derives the
    # diverged path's content from the worktree.
    assert (repo / "docs/plans/some-stub.md").read_text(encoding="utf-8") == "WORKTREE\n"


# ---------------------------------------------------------------------------
# (n) C6d — `_commit_and_push_origin_stub_close`'s push-status vocabulary: a
# policy decline is reported distinctly from a genuine push failure, and
# distinctly from `push_mode="none"`'s own no-attempt shape.
# docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md § C6d.
# ---------------------------------------------------------------------------


def test_origin_stub_close_push_decline_is_not_routed_through_error_channel(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "docs/plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs/plans/some-stub.md").write_text("content\n", encoding="utf-8")

    declined_outcome = PushOutcome(
        exit_code=0, skipped=["push:branch-policy"], message="declined: not a work/* branch"
    )
    monkeypatch.setattr(m, "push_with_retry", lambda worktree_root: declined_outcome)

    follow_up_sha, pushed, push_status, error = m._commit_and_push_origin_stub_close(
        repo, ["docs/plans/some-stub.md"], "deadbeef", push_mode=m.PUSH_MODE_SYNC
    )

    assert follow_up_sha is not None
    assert error is None, "a policy decline must not surface through the error channel"
    assert pushed is None
    assert push_status == m.PUSH_STATUS_DECLINED


def test_origin_stub_close_genuine_push_failure_still_routes_through_error_channel(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "docs/plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs/plans/some-stub.md").write_text("content\n", encoding="utf-8")

    failed_outcome = PushOutcome(
        exit_code=1, failed=["push:rejected"]
    )
    monkeypatch.setattr(m, "push_with_retry", lambda worktree_root: failed_outcome)

    follow_up_sha, pushed, push_status, error = m._commit_and_push_origin_stub_close(
        repo, ["docs/plans/some-stub.md"], "deadbeef", push_mode=m.PUSH_MODE_SYNC
    )

    assert follow_up_sha is not None
    assert pushed is False
    assert push_status == m.PUSH_STATUS_FAILED
    assert error is not None
    assert "git push failed" in error


def test_origin_stub_close_push_mode_none_keeps_distinct_not_attempted_shape(tmp_path, monkeypatch):
    """`push_mode="none"` (no attempt at all) and a `"sync"` attempt that
    `push_with_retry` itself declined are two different reasons for "no push
    happened" -- see module docstring. Both carry `push_status` values, but
    `push_mode="none"` never calls `push_with_retry` at all."""
    repo = real_git_repo(tmp_path)
    (repo / "docs/plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs/plans/some-stub.md").write_text("content\n", encoding="utf-8")

    called = False

    def _boom(worktree_root):
        nonlocal called
        called = True
        raise AssertionError("push_with_retry must not be called under push_mode='none'")

    monkeypatch.setattr(m, "push_with_retry", _boom)

    follow_up_sha, pushed, push_status, error = m._commit_and_push_origin_stub_close(
        repo, ["docs/plans/some-stub.md"], "deadbeef", push_mode=PUSH_MODE_NONE
    )

    assert called is False
    assert follow_up_sha is not None
    assert pushed is None
    assert push_status == m.PUSH_STATUS_NOT_ATTEMPTED
    assert error is None


# ---------------------------------------------------------------------------
# (m) C6b — second trigger: a newly-stamped consumed handoff fires
# `deliverable.cascade_terminal` (the SAME shared entrypoint C6 registers),
# never a second cascade implementation.
# docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6b.
# ---------------------------------------------------------------------------


def _write_handoff(worktree_root: Path, relpath: str, deliverable_id: str | None) -> None:
    fm_lines = ["---", "id: h-1", "status: shipped"]
    if deliverable_id is not None:
        fm_lines.append(f"deliverable_id: {deliverable_id}")
    fm_lines += ["---", "", "body"]
    target = worktree_root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")


async def _fake_close_origin_stub_noop(params: dict, repo_root: Path) -> dict:
    return {"exit_code": 0, "closed": [], "skipped": []}


def test_run_fires_cascade_for_each_newly_stamped_handoff_with_deliverable_id(
    monkeypatch, tmp_path
):
    _write_handoff(tmp_path, "state/handoffs/a.md", "dlv-alpha")
    _write_handoff(tmp_path, "state/handoffs/b.md", "dlv-beta")

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/a.md", "state/handoffs/b.md"])

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    cascade_calls: list[dict] = []

    async def _fake_cascade(params: dict, repo_root: Path) -> dict:
        cascade_calls.append(params)
        return {
            "exit_code": 0,
            "advanced": [{"handoff_path": f"advanced-for-{params['deliverable_id']}", "message": "advanced"}],
            "refused": [],
        }

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub_noop,
            cascade_handler=_fake_cascade,
        )
    )

    assert {c["deliverable_id"] for c in cascade_calls} == {"dlv-alpha", "dlv-beta"}
    assert all(c["source_kind"] == "handoff" for c in cascade_calls)
    assert {c["source_path"] for c in cascade_calls} == {
        str(tmp_path / "state/handoffs/a.md"),
        str(tmp_path / "state/handoffs/b.md"),
    }
    assert set(outcome.deliverable_cascade_result["acted"]) == {
        "advanced-for-dlv-alpha",
        "advanced-for-dlv-beta",
    }
    assert outcome.deliverable_cascade_result["failed"] == []


def test_run_skips_cascade_for_stamped_handoff_with_no_deliverable_id(monkeypatch, tmp_path):
    _write_handoff(tmp_path, "state/handoffs/no-did.md", deliverable_id=None)

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/no-did.md"])

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    called = False

    async def _fake_cascade(params: dict, repo_root: Path) -> dict:
        nonlocal called
        called = True
        return {"exit_code": 0, "advanced": [], "refused": []}

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub_noop,
            cascade_handler=_fake_cascade,
        )
    )

    assert called is False
    assert outcome.deliverable_cascade_result["acted"] == []
    assert outcome.deliverable_cascade_result["failed"] == []
    assert any("no-deliverable-id" in s for s in outcome.deliverable_cascade_result["skipped"])


def test_run_cascade_failure_does_not_propagate(monkeypatch, tmp_path):
    _write_handoff(tmp_path, "state/handoffs/a.md", "dlv-alpha")

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/a.md"])

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    async def _boom(params: dict, repo_root: Path) -> dict:
        raise RuntimeError("simulated deliverable.cascade_terminal crash")

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub_noop,
            cascade_handler=_boom,
        )
    )

    assert outcome.deliverable_cascade_result["acted"] == []
    assert any(
        "simulated deliverable.cascade_terminal crash" in f
        for f in outcome.deliverable_cascade_result["failed"]
    )


def test_run_cascade_handler_defaults_to_get_op_handler_when_not_injected(monkeypatch, tmp_path):
    """No `cascade_handler` supplied (the `wsc_tail.py` call-site shape,
    unchanged by C6b) resolves it internally via `get_op_handler` -- never a
    required param existing callers must be updated to pass."""
    _write_handoff(tmp_path, "state/handoffs/a.md", "dlv-alpha")

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/a.md"])

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    resolved_calls: list[str] = []

    async def _fake_cascade(params: dict, repo_root: Path) -> dict:
        resolved_calls.append(params["deliverable_id"])
        return {"exit_code": 0, "advanced": [], "refused": []}

    monkeypatch.setattr(m, "get_op_handler", lambda name: _fake_cascade if name == m.OP_DELIVERABLE_CASCADE else None)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub_noop,
        )
    )

    assert resolved_calls == ["dlv-alpha"]
    assert outcome.deliverable_cascade_result is not None


def test_run_cascade_no_op_when_op_not_registered(monkeypatch, tmp_path):
    _write_handoff(tmp_path, "state/handoffs/a.md", "dlv-alpha")

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/a.md"])

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)
    monkeypatch.setattr(m, "get_op_handler", lambda name: None)

    outcome = _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=True,
            governing_plan_slug="",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub_noop,
        )
    )

    assert outcome.deliverable_cascade_result["acted"] == []
    assert outcome.deliverable_cascade_result["failed"] == []
    assert any("not-registered" in s for s in outcome.deliverable_cascade_result["skipped"])


def test_handler_happy_path_includes_deliverable_cascade_field(monkeypatch, tmp_path):
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    result = _run(
        m._handler(
            {"sid": "sid-1", "committed_sha": "deadbeef", "chain_terminal": True},
            repo_root=tmp_path,
        )
    )

    assert result["exit_code"] == 0
    assert result["deliverable_cascade"] == {"acted": [], "skipped": [], "failed": []}


def test_handler_reports_exit_code_2_on_cascade_failure(monkeypatch, tmp_path):
    # `_handler` derives worktree_root as `main_worktree_root(repo_root)` ==
    # repo_root.parent -- monkeypatch `_read_meta` directly rather than
    # depending on that derivation to locate a fixture file on disk.
    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome(stamped=["state/handoffs/a.md"])

    async def _boom(params: dict, repo_root: Path) -> dict:
        raise RuntimeError("simulated cascade crash")

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)
    monkeypatch.setattr(m, "_read_meta", lambda path: {"deliverable_id": "dlv-alpha"})
    monkeypatch.setattr(m, "get_op_handler", lambda name: _boom)

    result = _run(
        m._handler(
            {"sid": "sid-1", "committed_sha": "deadbeef", "chain_terminal": True},
            repo_root=tmp_path,
        )
    )

    assert result["exit_code"] == 2
    assert any(
        "simulated cascade crash" in f for f in result["deliverable_cascade"]["failed"]
    )


# ---------------------------------------------------------------------------
# (l) delivery_proof threading -- close_out_and_stamp's own delivery proof
# forwarded verbatim through run()/_run_origin_stub_close into every
# close_origin_stub_handler call. See `_run_origin_stub_close`'s own
# "delivery_proof" docstring section.
# ---------------------------------------------------------------------------


def test_run_forwards_delivery_proof_into_close_origin_stub_handler_calls(
    monkeypatch, tmp_path
):
    seen_params: list[dict] = []

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        seen_params.append(params)
        return {"exit_code": 0, "closed": [], "skipped": []}

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    proof = {
        "deliverable_id": "dlv-alpha",
        "join_provenance": "joined",
        "missing_chunk_ids": [],
        "status": "implemented",
    }

    _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=False,
            governing_plan_slug="my-plan",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            push_mode="deferred",
            delivery_proof=proof,
        )
    )

    assert len(seen_params) == 1
    assert seen_params[0]["delivery_proof"] == proof


def test_run_omits_delivery_proof_key_when_none(monkeypatch, tmp_path):
    """`delivery_proof=None` (every `wsc_tail`-invoked call site) must not
    add a `"delivery_proof"` key to the handler's params at all -- absence,
    not an explicit `None` value, preserves `handoff.close_origin_stub`'s
    own `params.get("delivery_proof")` default-None read exactly as it was
    before this threading existed."""
    seen_params: list[dict] = []

    async def _fake_stamp(*args: Any, **kwargs: Any) -> consumed_handoff_stamp.StampOutcome:
        return _make_stamp_outcome()

    async def _fake_close_origin_stub(params: dict, repo_root: Path) -> dict:
        seen_params.append(params)
        return {"exit_code": 0, "closed": [], "skipped": []}

    monkeypatch.setattr(consumed_handoff_stamp, "post_commit_stamp_and_ship", _fake_stamp)

    _run(
        m.run(
            tmp_path,
            tmp_path,
            "sid-1",
            "deadbeef",
            chain_terminal=False,
            governing_plan_slug="my-plan",
            initial_consumed=[],
            close_origin_stub_handler=_fake_close_origin_stub,
            push_mode="deferred",
        )
    )

    assert len(seen_params) == 1
    assert "delivery_proof" not in seen_params[0]
