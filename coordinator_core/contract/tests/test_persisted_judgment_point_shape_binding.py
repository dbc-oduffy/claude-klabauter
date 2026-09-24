"""Binds the persisted decision-object's judgment-point shape to its
production reader, from the writer's own output rather than a hand-built
fixture.

The defect this exists to catch (code-reviewer Finding 3 on chunk C3 of
docs/plans/2026-09-02-the-loader-fires-the-assembly-not-the-em.md, deferred
there with its reason named): a persisted-shape reader consumes the same
on-disk shape the writer emits, and nothing failed if `judgment.py`/
`envelope.py` renamed `judgment_points`, a point's `id`, or a disposition's
`value`, leaving the reader silently seeing nothing -- indistinguishable
from "the EM answered nothing".

This originally bound TWO readers -- `contract.decision_object.resume` and
`pickup_assemble.apply._read_session_dispositions` -- but `resume.py` is
GRAVESTONED (Item 67, docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-
2026-09-11.md: zero non-test production callers). The `pickup_assemble.apply`
binding below is what remains.

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


class TestPickupReaderReadsTheWritersOutput:
    """`pickup_assemble.apply._read_session_dispositions` may not go blind on
    an object the writer produced. A rename in `judgment.py` or `envelope.py`
    that this reader stopped tracking fails here.

    Was `TestBothConsumersReadTheSameBuiltObject`, binding a second reader
    (`contract.decision_object.resume`) alongside this one; that reader is
    GRAVESTONED (Item 67) and its tests removed with it -- see this module's
    docstring.
    """

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
