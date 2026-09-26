"""Tests for `j-continuation-vs-fork`'s `excise` disposition (2026-08-05,
break-glass predecessor excise).

The PM's ask: "we should be able to excise a predecessor in a break-glass
time like this." Before this disposition, the ONLY way to get a
predecessor-less brief was an EMPTY `artifact_path` (the
`standalone_no_predecessor_reason` shape) -- which also threw away the
`deliverable_id`/`initiative` carry, since those are resolved FROM
`artifact_path`. `excise` is the missing third option: keep `artifact_path`
(and its carry), but deliberately discard the resolved predecessor edge.

Reuses the EXISTING machinery both d1 (`if _pred:` guard omitting
`--predecessor`/`--predecessor-id`) and d6 (`if lineage.get("predecessor")
is not None:` arming guard) already have -- `brief()` nulls
`lineage["predecessor"]`/`lineage["predecessor_id"]` when `excise` is
selected, before `_build_directives` ever reads either.

Spec backlink: coordinator_core/baton_assemble/__init__.py `brief()` and
`_build_judgment_points`'s `j-continuation-vs-fork` entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.ops.deliverable_carry import DivergentDeliverableIdError
from coordinator_core.session import claims as session_claims
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _init_repo,
    _write_artifact,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _write_predecessor_and_artifact(tmp_path):
    predecessor = _write_artifact(
        tmp_path / "state" / "handoffs" / "predecessor.md",
        ["handoff_id: hnd-predecessor-1a2b3c"],
    )
    artifact = _write_artifact(
        tmp_path / "state" / "handoffs" / "h1.md",
        [
            "deliverable_id: DEL-EXCISE-1",
            "initiative: init-excise-1",
            f"predecessor: {predecessor.relative_to(tmp_path)}",
        ],
    )
    return predecessor, artifact


def _excise_decisions(decision_note: str | None = "operator-authorized break-glass excise") -> dict:
    payload: dict = {"disposition": "excise"}
    if decision_note is not None:
        payload["decision_note"] = decision_note
    return {"j-continuation-vs-fork": payload}


class TestExciseDropsPredecessorArgsAndD6:
    def test_d1_emitted_without_predecessor_args(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief(
            "handoff", str(artifact), decisions=_excise_decisions(), repo_root=tmp_path
        ).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert not any(a.startswith("--predecessor=") for a in d1["args"]), d1["args"]
        assert not any(a.startswith("--predecessor-id=") for a in d1["args"]), d1["args"]

    def test_d6_absent_entirely(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief(
            "handoff", str(artifact), decisions=_excise_decisions(), repo_root=tmp_path
        ).decision_object
        ids = {d["id"] for d in decision["directives"]}
        assert "d6" not in ids, ids
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis, clis

    def test_deliverable_id_still_carries_from_the_plan(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief(
            "handoff", str(artifact), decisions=_excise_decisions(), repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] == "DEL-EXCISE-1"
        assert lineage["initiative"] == "init-excise-1"
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert "--deliverable-id=DEL-EXCISE-1" in d1["args"], d1["args"]

    def test_lineage_predecessor_and_predecessor_id_nulled(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief(
            "handoff", str(artifact), decisions=_excise_decisions(), repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] is None
        assert lineage["predecessor_id"] is None

    def test_standalone_no_predecessor_reason_records_the_decision_note(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        note = "operator-authorized break-glass excise: predecessor unreachable"
        decision = ba.brief(
            "handoff", str(artifact), decisions=_excise_decisions(note), repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["standalone_no_predecessor_reason"] == note


class TestExciseRequiresDecisionNote:
    def test_missing_decision_note_is_refused(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        with pytest.raises(ValueError, match="decision_note"):
            ba.brief(
                "handoff",
                str(artifact),
                decisions=_excise_decisions(decision_note=None),
                repo_root=tmp_path,
            )

    def test_empty_decision_note_is_refused(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        with pytest.raises(ValueError, match="decision_note"):
            ba.brief(
                "handoff",
                str(artifact),
                decisions=_excise_decisions(decision_note="   "),
                repo_root=tmp_path,
            )


class TestExciseDispositionAdvertisedOnJudgmentPoint:
    def test_j_continuation_vs_fork_advertises_excise_alongside_continue(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        jp = next(j for j in decision["judgment_points"] if j["id"] == "j-continuation-vs-fork")
        values = {d["value"] for d in jp["dispositions"]}
        assert values == {"continue", "excise"}
        for d in jp["dispositions"]:
            assert d["resolves"] == ["d1"]


class TestExciseIsAdditiveOnly:
    def test_continue_disposition_path_unaffected(self, tmp_path):
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert any(a.startswith("--predecessor=") for a in d1["args"]), d1["args"]
        assert any(a.startswith("--predecessor-id=") for a in d1["args"]), d1["args"]
        ids = {d["id"] for d in decision["directives"]}
        assert "d6" in ids, ids

    def test_legacy_nonconforming_predecessor_id_omitted(self, tmp_path):
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-windows-deferred-legs-2026-07-28"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-EXCISE-LEGACY-1",
                "initiative: init-excise-legacy-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert any(a.startswith("--predecessor=") for a in d1["args"]), d1["args"]
        assert not any(a.startswith("--predecessor-id=") for a in d1["args"]), d1["args"]


class TestExciseReachesTheDivergenceCheck:

    @staticmethod
    def _seed_divergent_rungs(tmp_path: Path, session_id: str) -> tuple[str, Path]:
        """A claimed plan naming one `deliverable_id` and an OPERATOR-NAMED
        predecessor handoff naming a different one -- divergent by
        construction, same fixture shape `repro_claim_a.py` used."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-14-excise-divergence-alpha"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: dlv-alpha-aaa111"],
        )
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-14-predecessor-beta.md",
            ["deliverable_id: dlv-beta-bbb222", "initiative: init-beta"],
        )
        session_claims.claim_plan(plan_slug, cwd=str(tmp_path))
        return session_id, predecessor

    def test_ac1_excise_rescues_a_divergent_rung_handoff(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-excise-rescue")
        _, predecessor = self._seed_divergent_rungs(tmp_path, "sid-excise-rescue")

        decision = ba.brief(
            "handoff",
            str(predecessor),
            decisions=_excise_decisions("operator: two legitimate deliverables, excising"),
            repo_root=tmp_path,
        ).decision_object

        assert decision["artifact"]["lineage"]["deliverable_id"] == "dlv-beta-bbb222"

    def test_ac2_divergence_without_a_disposition_never_auto_picks(
        self, tmp_path, monkeypatch
    ):
        """Superseded shape (2026-08-14, multi-plan sessions cannot hand
        off): this used to assert `DivergentDeliverableIdError` propagates
        out of `brief` -- the dead end that left a multi-plan session with
        no sanctioned route. `brief` now converts the raise into
        `j-divergent-deliverable-id`. What this test pins is UNCHANGED and
        is the load-bearing half: absent an operator disposition, no rung is
        selected and no directive is emitted.
        """
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-excise-still-fatal")
        _, predecessor = self._seed_divergent_rungs(tmp_path, "sid-excise-still-fatal")

        decision = ba.brief(
            "handoff", str(predecessor), repo_root=tmp_path
        ).decision_object

        assert decision["directives"] == []
        assert not decision["artifact"]["lineage"].get("deliverable_id")

    def test_ac4_surviving_id_is_the_operator_named_rung_and_note_carried(
        self, tmp_path, monkeypatch
    ):
        """The operator supplied `artifact_path` (the predecessor) explicitly
        -- per the plan's rule, an explicit `artifact_path` means the
        OPERATOR named the predecessor, so excise cuts the auto-discovered
        `_plan_file` rung and keeps `_predecessor_file`'s carry."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-excise-note")
        _, predecessor = self._seed_divergent_rungs(tmp_path, "sid-excise-note")
        note = "operator: two legitimate deliverables, excising the claimed-plan rung"

        decision = ba.brief(
            "handoff",
            str(predecessor),
            decisions=_excise_decisions(note),
            repo_root=tmp_path,
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "dlv-beta-bbb222"
        assert lineage["standalone_no_predecessor_reason"] == note

    def test_predecessor_file_arm_self_resolved_from_ledger_is_cut(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        session_id = "sid-excise-predecessor-arm"
        plan_slug = "2026-08-14-excise-divergence-predecessor-arm"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: dlv-plan-arm-ccc333", "initiative: init-plan-arm"],
        )
        held_predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-14-held-predecessor.md",
            ["deliverable_id: dlv-predecessor-arm-ddd444", "initiative: init-predecessor-arm"],
        )
        self._seed_handoff_claim(tmp_path, session_id, held_predecessor.name)
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
        session_claims.claim_plan(plan_slug, cwd=str(tmp_path))

        decision = ba.brief(
            "handoff",
            "",
            decisions=_excise_decisions("operator: cutting the self-resolved predecessor arm"),
            repo_root=tmp_path,
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "dlv-plan-arm-ccc333"
        assert lineage["initiative"] == "init-plan-arm"
        assert lineage["predecessor"] is None
        assert lineage["predecessor_id"] is None

    def _seed_handoff_claim(self, repo_root, session_id, basename, claimed_at=None, stage=None):
        claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
        if claimed_at is not None:
            (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
        if stage is not None:
            (claims_dir / "stage").write_text(stage, encoding="utf-8")


class TestExciseInertWhenRungsAgree:

    def test_initiative_source_unswapped_when_deliverable_ids_agree(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        session_id = "sid-excise-rungs-agree"
        plan_slug = "2026-08-14-excise-rungs-agree-plan"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: dlv-shared-eee555", "initiative: init-from-plan"],
        )
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-14-predecessor-agree.md",
            ["deliverable_id: dlv-shared-eee555", "initiative: init-from-predecessor"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
        session_claims.claim_plan(plan_slug, cwd=str(tmp_path))

        decision = ba.brief(
            "handoff",
            str(predecessor),
            decisions=_excise_decisions("operator: excise supplied, but rungs agree"),
            repo_root=tmp_path,
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "dlv-shared-eee555"
        assert lineage["initiative"] == "init-from-plan", (
            "excise must not swap the initiative-attribution source when "
            "the rungs agree on deliverable_id -- got "
            f"{lineage['initiative']!r}"
        )
        assert lineage["predecessor"] is None
        assert lineage["predecessor_id"] is None
