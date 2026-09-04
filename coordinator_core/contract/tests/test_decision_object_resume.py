"""Tests for `coordinator_core.contract.decision_object.resume` (chunk C3,
docs/plans/2026-09-02-the-loader-fires-the-assembly-not-the-em.md).

Fixtures are built from the REAL persisted decision-object shape found under
`.git/coordinator-sessions/decisions/` in this repo (envelope.py's 8-key
`ENVELOPE_KEYS` + a single-judgment-point `j-kind` proposal shape, verified
against `.git/coordinator-sessions/decisions/f884605e-d165-416a-bdfa-
da0e59975b2b__cross-repo__inbox__2026-09-02-doe-claude-em-skill-assembly-
should-fire-itself.md.json`) rather than an invented shape, per this chunk's
own instruction to build against the actual substrate.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.contract.decision_object.resume import (
    ResumeRefused,
    check_not_stale,
    load_decision_object,
    resume_decisions,
)

_DISPOSITIONS = [
    {"value": "adopt", "resolves": ["d-action-memo"], "guidance": "Adopt the proposal."},
    {"value": "decline", "resolves": ["d-action-memo"], "guidance": "Decline the proposal."},
    {"value": "negotiate", "resolves": ["d-action-memo"], "guidance": "Counter-propose."},
    {"value": "fold-into-plan", "resolves": ["d-action-memo"], "guidance": "Fold into a live plan."},
]


def _make_decision_object(*, artifact_path: str, persisted_status: str) -> dict:
    """Builds a decision object matching the real 8-key envelope shape plus
    the single open `j-kind` judgment point actually found on disk for this
    plan's own source memo."""
    return {
        "artifact": {
            "chain": None,
            "classification": "memo",
            "frontmatter": {
                "created": "2026-09-02",
                "status": persisted_status,
                "title": "A skill should fire its own assembly on entry",
            },
            "kind_resolved": "proposal",
            "path": artifact_path,
            "resolution": None,
        },
        "preflight": {},
        "gates": {},
        "directives": [
            {
                "already_satisfied": False,
                "args": ["action-memo", artifact_path],
                "cli": "archive-stamp-cli",
                "depends_on": "j-kind",
                "id": "d-action-memo",
            }
        ],
        "judgment_points": [
            {
                "id": "j-kind",
                "question": "proposal: Adopt / Decline / Negotiate?",
                "dispositions": [dict(d) for d in _DISPOSITIONS],
                "evidence": "artifact.kind_resolved",
                "reason": "insufficient-evidence",
                "recommendation": None,
                "revalidate_at_dispatch": False,
                "round_trip": "terminal",
            }
        ],
        "decisions": {},
        "narration": "1 judgment point(s) open.",
        "next_move": "Resolve the open judgment point(s).",
    }


@pytest.fixture
def fresh_artifact(tmp_path):
    """An artifact file whose current on-disk frontmatter `status` matches
    the decision object's persisted snapshot -- the not-stale case."""
    rel = "cross-repo/inbox/sample-memo.md"
    full = tmp_path / rel
    full.parent.mkdir(parents=True)
    full.write_text(
        "---\nstatus: open\ntitle: sample\n---\n\nbody\n", encoding="utf-8"
    )
    decision_object = _make_decision_object(artifact_path=rel, persisted_status="open")
    return tmp_path, decision_object


class TestRoundTrip:
    def test_round_trip_produces_apply_shaped_payload(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact
        answers = {"j-kind": "adopt"}

        payload = resume_decisions(decision_object, answers, repo_root=repo_root)

        assert payload == {"j-kind": {"disposition": "adopt"}}

    def test_round_trip_preserves_extra_content_keys(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact
        answers = {"j-kind": {"disposition": "adopt", "realized_by": "abc123"}}

        payload = resume_decisions(decision_object, answers, repo_root=repo_root)

        assert payload == {"j-kind": {"disposition": "adopt", "realized_by": "abc123"}}

    def test_payload_is_accepted_by_apply_bases_own_normalizer(self, fresh_artifact):
        """The produced payload must be exactly what
        `apply_base.normalize_decisions` widens/accepts -- proves the
        round-trip against the real consumer, not an assumption about its
        shape."""
        from coordinator_core.contract.apply_base import (
            disposition_resolves_directive,
            normalize_decisions,
        )

        repo_root, decision_object = fresh_artifact
        payload = resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=repo_root)

        normalized, malformed = normalize_decisions(payload)
        assert malformed == []
        assert normalized == {"j-kind": {"disposition": "adopt"}}

        jp = decision_object["judgment_points"][0]
        assert disposition_resolves_directive(jp, normalized, "d-action-memo") is True


class TestRefusesUnrecordedDisposition:
    def test_refuses_disposition_not_named_by_the_judgment_point(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact

        with pytest.raises(ResumeRefused, match="does not record disposition"):
            resume_decisions(decision_object, {"j-kind": "yolo-ship-it"}, repo_root=repo_root)

    def test_refuses_answer_for_unknown_judgment_point_id(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact

        with pytest.raises(ResumeRefused, match="does not carry"):
            resume_decisions(decision_object, {"j-does-not-exist": "adopt"}, repo_root=repo_root)

    def test_never_coerces_dict_answer_missing_disposition(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact

        with pytest.raises(ResumeRefused, match="does not record disposition"):
            resume_decisions(
                decision_object, {"j-kind": {"decision_note": "no disposition key"}},
                repo_root=repo_root,
            )


class TestRefusesStaleObject:
    def test_refuses_when_artifact_status_has_moved_on(self, tmp_path):
        rel = "cross-repo/inbox/sample-memo.md"
        full = tmp_path / rel
        full.parent.mkdir(parents=True)
        # Artifact was actioned by a later turn after this object was persisted.
        full.write_text(
            "---\nstatus: actioned\ntitle: sample\n---\n\nbody\n", encoding="utf-8"
        )
        decision_object = _make_decision_object(artifact_path=rel, persisted_status="open")

        with pytest.raises(ResumeRefused, match="frontmatter status changed"):
            resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=tmp_path)

    def test_refuses_when_artifact_no_longer_exists(self, tmp_path):
        decision_object = _make_decision_object(
            artifact_path="cross-repo/inbox/gone.md", persisted_status="open"
        )

        with pytest.raises(ResumeRefused, match="no longer exists"):
            resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=tmp_path)

    def test_refuses_when_persisted_object_carries_no_status_snapshot(self, tmp_path):
        """Some artifact classes (archived/ambiguous) are persisted with an
        empty frontmatter snapshot -- no usable staleness signal exists, and
        this must refuse rather than treat "no signal" as "safe"."""
        rel = "cross-repo/archive/gone-quiet.md"
        full = tmp_path / rel
        full.parent.mkdir(parents=True)
        full.write_text("---\nstatus: archived\n---\n\nbody\n", encoding="utf-8")
        decision_object = _make_decision_object(artifact_path=rel, persisted_status="open")
        decision_object["artifact"]["frontmatter"] = {}

        with pytest.raises(ResumeRefused, match="no `status` snapshot"):
            resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=tmp_path)

    def test_real_persisted_decision_object_on_disk_is_now_stale(self):
        """Not a synthetic fixture: the actual `.git/coordinator-sessions/
        decisions/` file this plan's own source memo was persisted under,
        checked against this repo's actual current tree. The memo has since
        been actioned (its live frontmatter `status: actioned`), while the
        persisted snapshot recorded `status: open` -- a real, naturally-
        occurring stale case, not a contrived one."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        decision_path = (
            repo_root
            / ".git"
            / "coordinator-sessions"
            / "decisions"
            / (
                "f884605e-d165-416a-bdfa-da0e59975b2b__cross-repo__inbox__"
                "2026-09-02-doe-claude-em-skill-assembly-should-fire-itself.md.json"
            )
        )
        if not decision_path.is_file():
            pytest.skip("real fixture decision object not present on this checkout")

        decision_object = load_decision_object(decision_path)
        assert decision_object["artifact"]["frontmatter"]["status"] == "open"

        with pytest.raises(ResumeRefused, match="frontmatter status changed"):
            check_not_stale(decision_object, repo_root=repo_root)


class TestNoMutation:
    def test_resume_decisions_does_not_write_the_artifact(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact
        artifact_full_path = repo_root / decision_object["artifact"]["path"]
        before = artifact_full_path.read_text(encoding="utf-8")
        before_mtime = artifact_full_path.stat().st_mtime_ns

        resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=repo_root)

        after = artifact_full_path.read_text(encoding="utf-8")
        after_mtime = artifact_full_path.stat().st_mtime_ns
        assert after == before
        assert after_mtime == before_mtime

    def test_resume_decisions_does_not_mutate_the_decision_object_argument(self, fresh_artifact):
        repo_root, decision_object = fresh_artifact
        before = json.dumps(decision_object, sort_keys=True)

        resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=repo_root)

        after = json.dumps(decision_object, sort_keys=True)
        assert after == before

    def test_resume_decisions_never_calls_apply_or_archive_stamp(self, fresh_artifact, monkeypatch):
        """Proves the no-mutation claim rather than assuming it: patches the
        two real mutating surfaces this module could plausibly reach for --
        `apply_base.execute_directives` (the directive-dispatch chokepoint
        every `apply` runs through) and pickup's own `apply()` entry point --
        to raise if invoked at all."""
        import coordinator_core.contract.apply_base as apply_base
        import coordinator_core.pickup_assemble.apply as pickup_apply

        def _boom(*args, **kwargs):
            raise AssertionError("resume_decisions must never call an apply/mutation surface")

        monkeypatch.setattr(apply_base, "execute_directives", _boom)
        monkeypatch.setattr(pickup_apply, "apply", _boom)

        repo_root, decision_object = fresh_artifact
        resume_decisions(decision_object, {"j-kind": "adopt"}, repo_root=repo_root)
        # No AssertionError raised => no mutating surface was reached.


class TestLoadDecisionObject:
    def test_refuses_missing_file(self, tmp_path):
        with pytest.raises(ResumeRefused, match="cannot read"):
            load_decision_object(tmp_path / "does-not-exist.json")

    def test_refuses_malformed_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ResumeRefused, match="not valid JSON"):
            load_decision_object(p)

    def test_refuses_non_object_json(self, tmp_path):
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ResumeRefused, match="must be a JSON object"):
            load_decision_object(p)

    def test_loads_a_real_persisted_object(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        decision_path = (
            repo_root
            / ".git"
            / "coordinator-sessions"
            / "decisions"
            / (
                "f884605e-d165-416a-bdfa-da0e59975b2b__cross-repo__inbox__"
                "2026-09-02-doe-claude-em-skill-assembly-should-fire-itself.md.json"
            )
        )
        if not decision_path.is_file():
            pytest.skip("real fixture decision object not present on this checkout")
        obj = load_decision_object(decision_path)
        assert set(obj.keys()) == {
            "artifact",
            "preflight",
            "gates",
            "directives",
            "judgment_points",
            "decisions",
            "narration",
            "next_move",
        }
