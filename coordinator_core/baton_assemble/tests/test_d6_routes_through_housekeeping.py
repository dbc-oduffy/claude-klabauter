"""d6 unwraps `housekeeping.cycle`, and keys on the transition's own verdict.

Governing plan:
`docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md`,
chunk C5.

What broke, and what this module holds shut. `_dispatch_handoff_supersede_predecessor`
called `handoff.archive_transition`, which is in `SUSPENDED_OPS` — so `get_op_handler`
refused before the op was composed and the directive degraded on every `/handoff` in
the fleet, leaving every continuation baton's predecessor non-terminal. That is the
PM-quoted d6 outage. The rewire points d6 at `housekeeping.cycle`, which reaches the
same surviving compute as a library while the killed key stays dead. It named
`handoff.housekeeping` until that job was itself killed under the brightline and this
call site was repointed onto its replacement (plan
`2026-08-29-the-housekeeping-cycle-stops-committing.md`, chunk C8).

The rewire adds ONE layer — housekeeping returns the transition op's result under a
`transition` key — and that layer is where a silent regression would live. d6's fail
posture reads `superseded` off the transition result, and `superseded is False` is the
half-applied succession the directive exists to eliminate. If the unwrap ever returns
the housekeeping envelope instead of the inner result, `superseded` is absent, the
falsy check fires, and d6 raises on every SUCCESSFUL supersession — or, unwrapped the
other way, never fires at all. So both directions are asserted here.

`_invoke_op_in_process` is patched rather than driven for real: this module tests the
UNWRAP and the fail posture, not the op behind it (that is
`coordinator_core/ops/tests/test_handoff_housekeeping.py`). The sibling suite
`test_apply_degrade_no_compensation.py` covers the same handler's degrade paths through
the real scaffold generator.

Negative-spec: does NOT re-test the DR-242 gate, `_cleanup_successor`'s pristine-scaffold
predicate, or the suspension degrade — all three are in that sibling suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble.apply as ba_apply

_PRED_REL = "state/handoffs/predecessor.md"
_SUCCESSOR_REL = "state/handoffs/2026-08-28-successor.md"


def _seed(tmp_path: Path) -> None:
    """A predecessor that honestly satisfies DR-242's claimed-or-shipped gate, and
    a successor that is NOT a pristine generator scaffold — so `_cleanup_successor`
    declines to unlink it and the assertions below are about d6's own posture rather
    than about the cleanup predicate."""
    pred = tmp_path / _PRED_REL
    pred.parent.mkdir(parents=True, exist_ok=True)
    pred.write_text(
        "---\n"
        "status: claimed\n"
        "claimed_by: some-session\n"
        "claimed_at: 2026-08-27T10:00:00Z\n"
        "---\n\n# Predecessor\n\nBody.\n",
        encoding="utf-8",
    )
    succ = tmp_path / _SUCCESSOR_REL
    succ.write_text(
        "---\nstatus: open\n---\n\n# Successor\n\nReal operator prose.\n",
        encoding="utf-8",
    )


def _housekeeping_returning(transition: dict | None, **envelope):
    """A stand-in for `_invoke_op_in_process` that records what d6 asked for and
    answers in `housekeeping.cycle`'s real envelope shape."""
    seen: dict = {}

    def _fake(op_name, params, repo_root):
        seen["op_name"] = op_name
        seen["params"] = params
        return {
            "exit_code": 0,
            "closed": [],
            "surfaced": [],
            "archived": [],
            "skipped": [],
            "failed": [],
            "close_error": None,
            "transition": transition,
            **envelope,
        }

    return _fake, seen


class TestTheOpItAsksFor:
    def test_d6_calls_the_housekeeping_cycle_and_never_the_suspended_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the rewire. `handoff.archive_transition` is suspended;
        naming it here is what raised `OpSuspendedError` and stranded every
        predecessor."""
        _seed(tmp_path)
        fake, seen = _housekeeping_returning({"exit_code": 0, "superseded": True})
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
        )

        assert seen["op_name"] == "housekeeping.cycle"

    def test_the_transition_is_a_supersede_naming_this_runs_successor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(tmp_path)
        fake, seen = _housekeeping_returning({"exit_code": 0, "superseded": True})
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
        )

        transition = seen["params"]["transition"]
        assert transition["mode"] == "supersede"
        assert transition["handoff_path"] == _PRED_REL
        assert transition["continued_into"] == _SUCCESSOR_REL, (
            "`continued_into` is FRONTMATTER and contractually repo-relative — an "
            "absolute value here would author a machine-specific edge"
        )

    def test_the_corpus_legs_are_off_and_the_cap_is_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """d6 runs mid-`/handoff`, inside `apply()`'s transaction. Its remit is one
        succession — a fleet-wide close pass, or a 150-move archival commit landing
        on the operator's tree while they are minting a baton, is not something this
        directive may take on. The ceremonies own the full sweep."""
        _seed(tmp_path)
        fake, seen = _housekeeping_returning({"exit_code": 0, "superseded": True})
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
        )

        assert seen["params"]["close"] is False
        assert seen["params"]["cap"] == 1

    def test_no_exclude_reaches_the_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`exclude` fed the live-children guard, deleted from all four of its sites
        on 2026-08-28 per the PM ruling that having a child says nothing about
        whether a handoff should be archived. Passing it now would be cargo — and
        `handoff_archive_transition` no longer reads it."""
        _seed(tmp_path)
        fake, seen = _housekeeping_returning({"exit_code": 0, "superseded": True})
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
        )

        assert "exclude" not in seen["params"]
        assert "exclude" not in seen["params"]["transition"]


class TestTheUnwrap:
    def test_a_superseded_transition_returns_the_inner_result_not_the_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Downstream readers (`_report`'s committed_by attribution, the operator's
        own report) key on the transition op's own fields. Handing them the
        housekeeping envelope instead would lose `moved`, `retained` and the rest
        without any of them erroring."""
        _seed(tmp_path)
        inner = {"exit_code": 0, "superseded": True, "moved": True, "retained": False}
        fake, _ = _housekeeping_returning(inner)
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        out = ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
        )

        assert out["result"] == inner
        assert out["cli"] == "handoff.supersede_predecessor"

    def test_a_half_applied_succession_still_raises_through_the_new_layer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`superseded is False` is reached only AFTER the op ran, where the
        predecessor may be half-stamped. It must keep raising — a successor minted
        against a predecessor left un-superseded is the exact stranding defect this
        directive exists to eliminate, and the extra unwrap layer must not soften
        it into a green return."""
        _seed(tmp_path)
        fake, _ = _housekeeping_returning(
            {"exit_code": 0, "superseded": False, "retained": True,
             "retain_reason": "live claim holder"}
        )
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        with pytest.raises(RuntimeError, match="did not supersede"):
            ba_apply._dispatch_handoff_supersede_predecessor(
                [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
            )

    def test_a_housekeeping_refusal_before_the_transition_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A setup error inside housekeeping — a bad cap, an unresolvable worktree —
        returns exit_code:1 with `transition: None`. Falling through to the
        `superseded` check would report it as a half-applied succession, which
        names the wrong cause; it is a refusal before anything was composed."""
        _seed(tmp_path)
        fake, _ = _housekeeping_returning(
            None, exit_code=1, error="cap is required and must be a positive int"
        )
        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", fake)

        with pytest.raises(RuntimeError, match="refused before the transition ran"):
            ba_apply._dispatch_handoff_supersede_predecessor(
                [_PRED_REL, _SUCCESSOR_REL, _SUCCESSOR_REL], tmp_path
            )


def test_the_in_process_seam_dispatches_a_sync_handler() -> None:
    """`_invoke_op_in_process` used to `asyncio.run(handler(...))` unconditionally,
    which raises `ValueError: a coroutine was expected` on a sync op. That was
    latent until d6 pointed at one: `housekeeping.cycle` is sync at its op
    boundary, as are `fleet.archive_terminal_handoffs` and
    `session.sweep_consumed_handoffs`.

    Driven through the REAL seam with a deliberately-invalid cap, so the op refuses
    at its own first check and touches no disk — the assertion is that the call
    returns a dict at all rather than raising on the await."""
    result = ba_apply._invoke_op_in_process(
        "housekeeping.cycle", {"cap": 0}, Path.cwd()
    )

    assert isinstance(result, dict)
    assert result["exit_code"] == 1
    assert "cap" in result["error"]
