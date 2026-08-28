"""Tier-T tests for `handoff.housekeeping` — the FUSION contract, not the legs.

Governing plan:
`docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md`,
chunk C4.

What this module is for. The op composes three things that already have their
own test suites — `handoff_reconcile`'s close pass, `archive_terminal_handoffs`'s
`plan_sweep`, and `fleet._common`'s `archive_and_commit`. Re-testing what those
do is duplication; what has never been tested is the seam between them, which is
where this op's entire value and its entire risk sit. So every test below asserts
a property of the COMPOSITION: what runs, in what order, and what survives when
one leg fails.

Why the legs are patched rather than driven with a real repo. The one property
that most needs asserting — that the close pass runs BEFORE `plan_sweep`, because
step 1 mutates the states step 2 selects on — is invisible in an end-to-end
result and trivially observable in a call recorder. Patching also keeps these in
the FAST tier: the sibling suites that drive real git are `pytest.mark.cadence`
and excluded from the fast run, so a fusion regression there would not be seen
until a full run. End-to-end behaviour over a real corpus is C6's verification
job, and is not duplicated here.

Handler function imported directly, never resolved by op key — same rationale as
`test_archive_terminal_handoffs.py`: resolving by key races any concurrent
registration work in a peer chunk, and this module is not testing registration.
(That `handoff.housekeeping` IS registered on all five surfaces is
`check_registration_quad`'s job.)

Negative-spec:
  - Does NOT test terminality, scan rails, cap deferral, or dest-conflict
    detection. Those are `plan_sweep`'s and are covered in
    `coordinator_core/ops/fleet/tests/test_archive_terminal_handoffs.py`.
  - Does NOT test the close pass's own gate-cascade logic — `handoff_reconcile`'s
    suite owns it.
  - Does NOT spawn git, and must not start: a fusion test that needs a real
    repository has drifted into being a leg test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops.handoff_housekeeping as hk

_MODULE = "coordinator_core.ops.handoff_housekeeping"

_WORKTREE = Path("/nonexistent-fake-worktree")
_COMMON_DIR = Path("/nonexistent-fake-worktree/.git")


@pytest.fixture(autouse=True)
def _no_worktree_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main_worktree_root` reads real git config. Every test here supplies its
    own common dir, so pin the resolution rather than let one leg reach disk."""
    monkeypatch.setattr(f"{_MODULE}.main_worktree_root", lambda _cd: _WORKTREE)


def _patch_legs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: list,
    close_result: object = None,
    plan_result: object = ([], []),
    archive_result: object = ([], []),
):
    """Replace all three legs with recorders. Returns nothing; `calls` collects
    the leg names in invocation order, which is what the ordering test reads."""

    def _close(common_dir):
        calls.append("close")
        if isinstance(close_result, Exception):
            raise close_result
        return close_result if close_result is not None else {
            "gates_cleared": [],
            "surfaced": [],
        }

    def _plan(worktree, common_dir, cap):
        calls.append(("plan_sweep", worktree, common_dir, cap))
        if isinstance(plan_result, Exception):
            raise plan_result
        return plan_result

    async def _archive(worktree, moves, subject):
        calls.append(("archive_and_commit", worktree, tuple(moves), subject))
        return archive_result

    monkeypatch.setattr(f"{_MODULE}._close_finished", _close)
    monkeypatch.setattr(f"{_MODULE}.plan_sweep", _plan)
    monkeypatch.setattr(f"{_MODULE}.archive_and_commit", _archive)


class TestCapIsRequiredAndPositive:
    """`cap` has no default anywhere in this path, by design: an unbounded
    archival move over a 253-record corpus is not a thing a caller should be
    able to ask for by omission. Both legs this op replaces refuse an absent
    cap as a setup error and so does this one."""

    @pytest.mark.parametrize(
        "cap",
        [None, 0, -1, "150", 1.0, True],
        ids=["absent", "zero", "negative", "string", "float", "bool-true"],
    )
    def test_a_non_positive_int_cap_is_a_setup_error_and_moves_nothing(
        self, monkeypatch: pytest.MonkeyPatch, cap: object
    ) -> None:
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)
        params = {} if cap is None else {"cap": cap}

        result = hk._handler(params, _COMMON_DIR)

        assert result["exit_code"] == 1
        assert "cap" in result["error"]
        assert calls == [], (
            "a setup error ran a leg — the cap check must gate BEFORE the close "
            "pass, which is a writer"
        )

    def test_bool_true_is_refused_even_though_it_is_an_int_that_is_positive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`isinstance(True, int)` is True and `True > 0`, so a naive check
        accepts `cap=True` and silently caps the sweep at one move. Named
        separately from the parametrize above because it is the case that
        passes an obvious implementation."""
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)

        result = hk._handler({"cap": True}, _COMMON_DIR)

        assert result["exit_code"] == 1
        assert calls == []

    def test_a_missing_repo_root_is_a_setup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)

        result = hk._handler({"cap": 150}, None)

        assert result["exit_code"] == 1
        assert calls == []


class TestStepOrder:
    """The load-bearing claim in this op's docstring: close runs first, and the
    second read is not redundant. Step 1 mutates the deployment states step 2
    selects on, so a fused single read would file against a pre-close view and
    miss every handoff the same call just closed — the exact gap that made the
    sweep a separate op originally."""

    def test_the_close_pass_runs_before_the_sweep_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)

        hk._handler({"cap": 150}, _COMMON_DIR)

        assert [c if isinstance(c, str) else c[0] for c in calls] == [
            "close",
            "plan_sweep",
        ]

    def test_the_commit_runs_after_the_scan_and_only_with_moves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list = []
        _patch_legs(monkeypatch, calls=calls, plan_result=(["m1", "m2"], []))

        hk._handler({"cap": 150}, _COMMON_DIR)

        assert [c if isinstance(c, str) else c[0] for c in calls] == [
            "close",
            "plan_sweep",
            "archive_and_commit",
        ]

    def test_the_sweep_is_scoped_to_the_resolved_worktree_and_the_given_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)

        hk._handler({"cap": 7}, _COMMON_DIR)

        assert calls[1] == ("plan_sweep", _WORKTREE, _COMMON_DIR, 7)


class TestNothingToDo:
    def test_an_empty_plan_commits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty move set must not reach the committer. A commit leg invoked
        with nothing to move is how an empty commit — or worse, a commit that
        sweeps up a peer's staged files — reaches a shared tree."""
        calls: list = []
        _patch_legs(monkeypatch, calls=calls, plan_result=([], ["skipped-1"]))

        result = hk._handler({"cap": 150}, _COMMON_DIR)

        assert result["exit_code"] == 0
        assert result["archived"] == []
        assert result["skipped"] == ["skipped-1"]
        assert "archive_and_commit" not in [
            c if isinstance(c, str) else c[0] for c in calls
        ]


class TestCloseCanBeSkipped:
    def test_close_false_sweeps_without_running_the_close_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For a caller that has just closed records itself. The sweep still
        runs — `close: False` is not `dry_run`."""
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)

        result = hk._handler({"cap": 150, "close": False}, _COMMON_DIR)

        assert result["exit_code"] == 0
        assert [c if isinstance(c, str) else c[0] for c in calls] == ["plan_sweep"]
        assert result["closed"] == []

    def test_close_defaults_to_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list = []
        _patch_legs(monkeypatch, calls=calls)

        hk._handler({"cap": 150}, _COMMON_DIR)

        assert "close" in [c if isinstance(c, str) else c[0] for c in calls]


class TestAFailingLegDoesNotEatTheRest:
    def test_a_raising_close_pass_still_lets_the_sweep_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The close pass is the leg most likely to fail — it is the one that
        writes. Its failure must not take the archival with it, because
        archival being dead is the outage this op exists to end."""
        calls: list = []
        _patch_legs(
            monkeypatch,
            calls=calls,
            close_result=RuntimeError("gate cascade blew up"),
            plan_result=(["m1"], []),
        )

        result = hk._handler({"cap": 150}, _COMMON_DIR)

        assert result["exit_code"] == 0
        assert "archive_and_commit" in [
            c if isinstance(c, str) else c[0] for c in calls
        ]
        assert "gate cascade blew up" in result["close_error"]

    def test_a_close_pass_reporting_exit_code_1_is_not_silently_a_clean_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`handoff_reconcile._handler` reports its own failures as
        `exit_code: 1` with empty lists — byte-identical to a clean run over a
        corpus with nothing to close. Without `close_error` a caller cannot tell
        the two apart, and a dead close pass looks like a quiet success
        forever."""
        calls: list = []
        _patch_legs(
            monkeypatch,
            calls=calls,
            close_result={"gates_cleared": [], "surfaced": [], "exit_code": 1},
        )

        result = hk._handler({"cap": 150}, _COMMON_DIR)

        assert result["close_error"], (
            "an exit_code:1 close pass reported as a clean run — indistinguishable "
            "from having nothing to close"
        )

    def test_a_clean_close_pass_reports_no_close_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list = []
        _patch_legs(
            monkeypatch,
            calls=calls,
            close_result={"gates_cleared": ["h1"], "surfaced": [], "exit_code": 0},
        )

        result = hk._handler({"cap": 150}, _COMMON_DIR)

        assert result["close_error"] is None
        assert result["closed"] == ["h1"]

    def test_a_failing_plan_sweep_preserves_what_the_close_pass_already_did(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The close pass has already MUTATED disk by this point. Returning a
        bare setup error would report those closures as never having happened,
        and the next caller would have no way to know a write had landed."""
        calls: list = []
        _patch_legs(
            monkeypatch,
            calls=calls,
            close_result={"gates_cleared": ["h1"], "surfaced": ["h2"]},
            plan_result=OSError("corpus unreadable"),
        )

        result = hk._handler({"cap": 150}, _COMMON_DIR)

        assert result["exit_code"] == 1
        assert "corpus unreadable" in result["error"]
        assert result["closed"] == ["h1"]
        assert result["surfaced"] == ["h2"]


class TestResultShape:
    @pytest.mark.parametrize(
        "params,repo_root",
        [
            ({"cap": 150}, _COMMON_DIR),
            ({}, _COMMON_DIR),
            ({"cap": 150}, None),
        ],
        ids=["success", "bad-cap", "no-repo-root"],
    )
    def test_every_return_path_carries_the_same_keys(
        self, monkeypatch: pytest.MonkeyPatch, params: dict, repo_root: object
    ) -> None:
        """A caller branching on `result["archived"]` must not have to guard
        against the key being absent on the error paths — a KeyError inside a
        ceremony is a ceremony that stops."""
        calls: list = []
        _patch_legs(monkeypatch, calls=calls, plan_result=(["m1"], []))

        result = hk._handler(params, repo_root)

        for key in (
            "exit_code",
            "closed",
            "surfaced",
            "archived",
            "skipped",
            "failed",
            "close_error",
        ):
            assert key in result, f"missing {key!r} on this return path"

    def test_the_commit_subject_names_the_op_that_wrote_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A commit landing on a shared tree names its author. `handoff.
        housekeeping` is a NEW key over the surviving computes of three killed
        ops, so a commit attributed to one of the dead names would be a false
        provenance trail, not just an untidy subject."""
        calls: list = []
        _patch_legs(monkeypatch, calls=calls, plan_result=(["m1", "m2"], []))

        hk._handler({"cap": 150}, _COMMON_DIR)

        subject = calls[-1][3]
        assert hk.OP_KEY in subject
        assert "2" in subject


def test_the_killed_op_keys_are_not_resurrected() -> None:
    """Kill means kill forever (PM, 2026-08-23). This op is a new name over the
    surviving computes, never a restoration — a future edit that re-decorates
    one of the three dead keys to save a deferred import fails here."""
    import coordinator_core.ops.handoff_reconcile as reconcile

    source = Path(str(hk.__file__)).read_text(encoding="utf-8")
    assert "register_op" in source, "sanity: this op does register its OWN key"
    for dead in (
        "handoff.reconcile_open",
        "handoff.archive_transition",
        "session.sweep_consumed_handoffs",
    ):
        assert f'register_op("{dead}"' not in source
        assert f"register_op('{dead}')" not in source
    assert hk.OP_KEY == "handoff.housekeeping"
    # The close pass is reached as a LIBRARY, which is the sanctioned shape for
    # a suspended op's surviving compute — not a re-registration of its key.
    assert callable(reconcile._handler)
