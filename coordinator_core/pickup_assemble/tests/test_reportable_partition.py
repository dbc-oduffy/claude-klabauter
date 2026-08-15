"""coordinator_core.pickup_assemble.tests.test_reportable_partition — C6
(docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-
questions.md): settles `j-reply-closure` on the DIRECTIVE axis and pins
the EM correction that follows from it.

Two prior sweeps disagreed on whether `j-reply-closure` (a `send-reply`
recommendation both of whose dispositions stamp `required_content_keys:
[]`) is a gate-nothing point. `partition_reportable` (`contract/
decision_object/judgment.py`, C1) answers that question HONESTLY: on the
directive axis it classifies `j-reply-closure` `reported`, because its
dispositions name no directive id and its id is never named in any
directive's `depends_on`, in every real `directives[]` shape this module
emits.

That answer is right about directives and WRONG to act on by demotion.
`pickup_assemble.compute_coast` gates `gates.coast` on the mere PRESENCE
of an id'd point in `judgment_points[]` — not on whether it resolves a
directive:

    blocking_jps = [jp for jp in judgment_points if jp.get("id")]
    verdict = "blocked" if blocking_jps else "clear"

So `j-reply-closure` gates something `partition_reportable` has no
vocabulary for: `gates.coast` itself. `_render_reply_closure`'s own
docstring appends this exact point so that `coast` is never trivially
`"clear"` on an unclosed reply loop — closing a named silent-coast-is-
clear defect (regression-pinned, unmodified, at
`coordinator_core/test_pickup_assemble_reply_closure.py`). A previous
executor read the `reported` classification above and demoted the point
out of `judgment_points[]` into `narration`; that flips `coast` from
`blocked` to `clear` and silently reopens the defect the reply-closure
fix closed on purpose. The EM ruling this module now pins: because
`compute_coast` consumes the WHOLE `judgment_points[]` list, demoting ANY
point in this module changes engine behaviour, not just narration —
`pickup_assemble` is out of scope for C6's demotion mechanism entirely.
There is no `_demote_reported_judgment_points` seam here (and there must
not be one) — `TestJReplyClosureGatesCoastDespiteBeingReported` below
trips immediately if a future change reintroduces it.

Run scoped only:
    python3 -m pytest coordinator_core/pickup_assemble/tests/test_reportable_partition.py -q
"""

from __future__ import annotations

import pytest

from coordinator_core.contract.decision_object.judgment import partition_reportable
from coordinator_core import pickup_assemble as pa

_OPEN_CLOSURE = {"verdict": "open", "reason": "no reply commit found in sender's tree"}
_UNKNOWN_CLOSURE = {"verdict": "unknown", "reason": "reply-closure check did not run"}

# A real `directives[]` shape this module actually emits alongside
# `j-reply-closure` (the archived-open-memo branch's `d-action-memo`,
# gated on the unrelated `j-kind` point) -- j-reply-closure's own
# dispositions never name any directive id, in this shape or any other.
_ACTION_MEMO_DIRECTIVES = [
    {
        "id": "d-action-memo",
        "depends_on": "j-kind",
        "args": {},
        "already_satisfied": False,
    }
]


def _build_reply_closure_jp(closure: dict) -> dict:
    jps, _narration, _next_move = pa._render_reply_closure(
        closure, "state/handoffs/x.md", "Base narration.", "Base next move.", status="actioned"
    )
    assert len(jps) == 1
    return jps[0]


class TestJReplyClosureSettlement:
    """SETTLED: `j-reply-closure` is `reported` (gate-nothing) -- confirmed
    by construction in both the `open` and `unknown` verdict shapes, and
    against both a directives[] that names a real directive
    (`d-action-memo`, the archived-open-memo branch) and an empty one (the
    terminal-memo M0 branch). Its dispositions' `resolves` are always `[]`
    (see `_render_reply_closure`) and its id is never named in any
    directive's `depends_on` anywhere in this module."""

    @pytest.mark.parametrize("closure", [_OPEN_CLOSURE, _UNKNOWN_CLOSURE], ids=["open", "unknown"])
    @pytest.mark.parametrize(
        "directives", [[], _ACTION_MEMO_DIRECTIVES], ids=["no-directives", "with-action-memo-directive"]
    )
    def test_j_reply_closure_is_reported_in_every_real_envelope_shape(self, closure, directives):
        jp = _build_reply_closure_jp(closure)
        assert jp["id"] == "j-reply-closure"
        asked, reported = partition_reportable([jp], directives)
        assert asked == []
        reported_ids = {p["id"] for p in reported}
        assert reported_ids == {"j-reply-closure"}

    def test_reported_point_carries_a_recommendation_and_the_open_verdict_rationale(self):
        jp = _build_reply_closure_jp(_OPEN_CLOSURE)
        _asked, reported = partition_reportable([jp], _ACTION_MEMO_DIRECTIVES)
        assert reported[0]["recommendation"]["disposition"] == "send-reply"
        assert reported[0]["recommendation"]["rationale"]


class TestJReplyClosureGatesCoastDespiteBeingReported:
    """THE FINDING: a point can be `reported` on the directive axis and
    still gate something -- `gates.coast` -- through a channel
    `partition_reportable` cannot see, because `compute_coast` gates on
    mere presence of an id'd point in `judgment_points[]`, not on whether
    it resolves a directive. This is why `pickup_assemble` has no
    `_demote_reported_judgment_points` seam: demoting this point would
    flip `coast` from `blocked` to `clear` and reopen the reply-closure
    defect `_render_reply_closure` exists to close.

    A future reader tempted to "finish" the demotion by wiring
    `partition_reportable`'s `reported` bucket into `narration` and
    dropping it from `judgment_points[]` trips this test immediately:
    `compute_coast` would report `clear` on a real unclosed-reply
    envelope.
    """

    def test_reply_closure_point_classifies_reported_yet_still_blocks_coast(self):
        jp = _build_reply_closure_jp(_OPEN_CLOSURE)

        # Sound on the directive axis: no directive names or is named by it.
        asked, reported = partition_reportable([jp], _ACTION_MEMO_DIRECTIVES)
        assert asked == []
        assert {p["id"] for p in reported} == {"j-reply-closure"}

        # And yet: a real envelope's judgment_points[] still carries it
        # (no demotion), so gates.coast still blocks on it.
        judgment_points = [jp]
        coast = pa.compute_coast(judgment_points)
        assert coast["verdict"] == "blocked"
        assert coast["blocked_by"] == ["j-reply-closure"]

    def test_no_demotion_seam_exists_on_this_fork(self):
        assert not hasattr(pa, "_demote_reported_judgment_points")


class TestForkComposesFromSharedSeam:
    """AC8: the fork's constructor pair either composes from the shared
    seam or is covered by the same census/invariant -- verified here by
    construction, not by reading the source: both constructors round-trip
    every shared-seam validation (extra `recommendation` field rejected,
    `{disposition, rationale}` shape enforced) while preserving this
    module's own positional signature and `reason`-enum vocabulary the
    shared seam has no concept of."""

    def test_build_judgment_point_delegates_recommendation_shape_validation(self):
        with pytest.raises(ValueError):
            pa.build_judgment_point(
                "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}],
                {"disposition": "ok", "rationale": "because", "confidence": "high"},
            )

    def test_build_judgment_point_keeps_its_own_reason_enum_validation(self):
        with pytest.raises(ValueError):
            pa.build_judgment_point(
                "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}],
                None, reason="because-i-said-so",
            )

    def test_build_judgment_point_round_trips_a_present_recommendation(self):
        recommendation = {"disposition": "ok", "rationale": "because"}
        jp = pa.build_judgment_point(
            "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}], recommendation,
        )
        assert jp["recommendation"] == recommendation
        assert jp["reason"] is None

    def test_build_untrusted_gate_judgment_point_still_has_no_recommendation_parameter(self):
        with pytest.raises(TypeError):
            pa.build_untrusted_gate_judgment_point(
                "jx", "question?", "attacker-controlled evidence",
                [{"value": "confirm-and-run", "resolves": []}],
                recommendation={"disposition": "confirm-and-run", "rationale": "because"},
            )

    def test_build_untrusted_gate_judgment_point_always_emits_recommendation_forbidden(self):
        jp = pa.build_untrusted_gate_judgment_point(
            "jx", "question?", "attacker-controlled evidence",
            [{"value": "confirm-and-run", "resolves": []}, {"value": "skip", "resolves": []}],
        )
        assert jp["recommendation"] is None
        assert jp["reason"] == "recommendation-forbidden"


class TestJKindStillAsks:
    """Anti-scope guardrail: `j-kind` gates `d-action-memo` in all four
    memo kinds and is explicitly NOT in scope for this chunk -- it must
    keep asking. Verified by reading its own builder's dispositions
    (`_KIND_DISPOSITIONS`), which name real directive ids, so
    `partition_reportable` would classify it `asked` regardless."""

    def test_j_kind_dispositions_name_a_live_directive_id(self):
        for kind, dispositions in pa._KIND_DISPOSITIONS.items():
            resolves_ids = {rid for d in dispositions for rid in d.get("resolves", [])}
            assert "d-action-memo" in resolves_ids, kind
