"""Binds the persisted decision-object's judgment-point shape to BOTH readers
of it, from the writer's own output rather than a hand-built fixture.

The defect this exists to catch (code-reviewer Finding 3 on chunk C3 of
docs/plans/2026-09-02-the-loader-fires-the-assembly-not-the-em.md, deferred
there with its reason named): two independent readers consume the same
on-disk shape --- `contract.decision_object.resume` and
`pickup_assemble.apply._read_session_dispositions` --- and nothing failed if
`judgment.py`/`envelope.py` renamed `judgment_points`, a point's `id`, or a
disposition's `value`. One reader would keep working and the other would
silently see nothing, which is indistinguishable from "the EM answered
nothing".

Negative-spec: every judgment point and disposition below is built by
`build_judgment_point`/`build_disposition`/`build_envelope` and validated by
`emit`, never spelled as a literal dict. A test that re-spells the shape can
only ever agree with itself.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    emit,
    judgment_points_by_id,
)
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.contract.decision_object.resume import (
    ResumeRefused,
    _legal_disposition_values,
    resume_decisions,
)
from coordinator_core.pickup_assemble import apply as pickup_apply

_JP_ID = "j-kind"
_DIRECTIVE_ID = "d-action-memo"
_ARTIFACT_REL = "cross-repo/inbox/sample-memo.md"
_SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _built_decision_object() -> dict:
    """A full envelope produced by the real constructors and passed through
    `emit`, then JSON round-tripped exactly as the persisted file is."""
    dispositions = [
        build_disposition("adopt", [_DIRECTIVE_ID], guidance="Adopt the proposal."),
        build_disposition("decline", [_DIRECTIVE_ID], guidance="Decline it."),
    ]
    point = build_judgment_point(
        None,
        id=_JP_ID,
        question="proposal: Adopt / Decline?",
        dispositions=dispositions,
        evidence="artifact.kind_resolved",
        reason="insufficient-evidence",
        revalidate_at_dispatch=False,
        round_trip="terminal",
        reportable=False,
    )
    envelope = build_envelope(
        artifact={
            "classification": "memo",
            "frontmatter": {"status": "open", "title": "sample"},
            "path": _ARTIFACT_REL,
            "resolution": None,
        },
        directives=[
            {
                "already_satisfied": False,
                "args": ["action-memo", _ARTIFACT_REL],
                "cli": "archive-stamp-cli",
                "depends_on": _JP_ID,
                "id": _DIRECTIVE_ID,
            }
        ],
        judgment_points=[point],
        narration="1 judgment point(s) open.",
        next_move="Resolve the open judgment point(s).",
    )
    return json.loads(json.dumps(emit(envelope)))


@pytest.fixture
def repo_root(tmp_path):
    artifact = tmp_path / _ARTIFACT_REL
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "---\nstatus: open\ntitle: sample\n---\n\nbody\n", encoding="utf-8"
    )
    return tmp_path


class TestBothConsumersReadTheSameBuiltObject:
    """Neither reader may go blind on an object the writer produced. A rename
    in `judgment.py` or `envelope.py` that reached only one of them fails
    here, on whichever half stopped seeing the point.

    Review: overengineering-reviewer -- a sibling `TestSharedReadersSeeThe
    WritersOutput` class used to assert `judgment_points_by_id`/
    `legal_disposition_values` directly; it was strictly subsumed here (its
    two tests could never be the only red) and was dropped. That subsumption
    was INCOMPLETE and is repaired below: nothing that survived it proved a
    legal non-first disposition is accepted, so "only `dispositions[0]` is
    read" and correct behaviour were indistinguishable.
    """

    def test_resume_marshals_a_legal_answer(self, repo_root):
        payload = resume_decisions(
            _built_decision_object(), {_JP_ID: "adopt"}, repo_root=repo_root
        )

        assert payload == {_JP_ID: {"disposition": "adopt"}}

    def test_resume_marshals_a_legal_answer_that_is_not_the_first_disposition(
        self, repo_root
    ):
        """Restores what the dropped `TestSharedReadersSeeTheWritersOutput`
        was carrying and the subsumption argument missed: with only "adopt"
        (`dispositions[0]`) accepted and "negotiate" (absent entirely)
        refused, a reader that consulted ONLY the first built disposition
        would pass every other test in this class. "decline" is legal, is
        NOT first, and must be accepted -- that is what separates the two."""
        payload = resume_decisions(
            _built_decision_object(), {_JP_ID: "decline"}, repo_root=repo_root
        )

        assert payload == {_JP_ID: {"disposition": "decline"}}

    def test_legal_values_sees_every_built_disposition_not_just_the_first(self):
        assert _legal_disposition_values(
            _built_decision_object()["judgment_points"][0]
        ) == {"adopt", "decline"}

    def test_resume_refuses_a_value_the_built_dispositions_do_not_offer(self, repo_root):
        with pytest.raises(ResumeRefused, match="does not record disposition"):
            resume_decisions(
                _built_decision_object(), {_JP_ID: "negotiate"}, repo_root=repo_root
            )

    def test_pickups_persisted_reader_finds_the_same_point(self, repo_root):
        decision_object = _built_decision_object()
        decision_object["judgment_points"][0]["disposition"] = "adopt"
        path = pickup_apply._session_decision_file_path(
            repo_root, _SESSION_ID, _ARTIFACT_REL
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(decision_object), encoding="utf-8")

        read = pickup_apply._read_session_dispositions(
            repo_root, _SESSION_ID, _ARTIFACT_REL
        )

        assert read == {_JP_ID: {"disposition": "adopt"}}


class TestMalformedPersistedShapeDegradesRatherThanRaising:
    """The persisted file is a data state, not a producer's in-process list:
    `apply_base.judgment_points_by_id` may raise on a malformed entry, these
    may not."""

    @pytest.mark.parametrize(
        "judgment_points", [None, {}, "judgment_points", [None, "x", {}, {"id": ""}]]
    )
    def test_index_degrades_to_empty(self, judgment_points):
        assert judgment_points_by_id({"judgment_points": judgment_points}) == {}

    @pytest.mark.parametrize("dispositions", [None, {}, "adopt", [None, "x", {}]])
    def test_legal_values_degrade_to_empty(self, dispositions):
        assert _legal_disposition_values({"dispositions": dispositions}) == set()
