"""Tests for `j-fan-in-cardinality` (sedge-04, narration-only fan-in
cardinality legibility).

Per Resolution 1 (`state/roadmap/sedge-2026-08-06/COORDINATOR-RESOLUTIONS.md`),
N->1 fan-in is the correct engine shape and stays -- this point adds no new
cardinality, it only narrates a fan-in that `resolve_lineage` already
resolved via `lineage["additional_predecessors"]`.

Spec backlink: coordinator_core/baton_assemble/__init__.py
`_build_fan_in_cardinality_judgment_point` and `brief()`'s append site
behind `if kind == "handoff" and lineage.get("additional_predecessors"):`.
"""

from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _init_repo, _write_artifact


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- `brief()` calls `resolve_operator_config()` unconditionally. Mirrors
    `test_j_continuation_vs_fork_excise.py`'s own fixture of the same name."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _seed_handoff_claim(repo_root, session_id: str, basename: str, claimed_at: str) -> None:
    claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


class TestFanInCardinalityJudgmentPointFires:
    def test_two_claimed_batons_surface_the_new_point_with_deliverable_id_evidence(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        first_claimed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-first-alpha.md",
            ["deliverable_id: DEL-PRIMARY-1", "handoff_id: hnd-first-claimed-1a2b46"],
        )
        second_claimed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-21-second-alpha.md",
            ["deliverable_id: DEL-EXTRA-1", "handoff_id: hnd-second-claimed-1a2b50"],
        )
        _seed_handoff_claim(
            tmp_path, "sid-fan-in", first_claimed.name, claimed_at="2026-07-20T09:00:00Z"
        )
        _seed_handoff_claim(
            tmp_path, "sid-fan-in", second_claimed.name, claimed_at="2026-07-20T10:00:00Z"
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-fan-in")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["additional_predecessors"] == [
            second_claimed.relative_to(tmp_path).as_posix()
        ]

        jp = next(
            j for j in decision["judgment_points"] if j["id"] == "j-fan-in-cardinality"
        )
        assert set(jp.keys()) == {
            "id",
            "question",
            "dispositions",
            "evidence",
            "reason",
            "recommendation",
            "revalidate_at_dispatch",
            "round_trip",
        }
        assert jp["recommendation"] is None
        assert jp["dispositions"] == [{"value": "acknowledge", "resolves": []}]
        assert "DEL-PRIMARY-1" in jp["evidence"]
        assert "SURVIVES" in jp["evidence"]
        assert "DEL-EXTRA-1" in jp["evidence"]
        assert "dropped" in jp["evidence"]

    def test_new_point_id_never_appears_in_any_depends_on(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        first_claimed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-first-alpha.md",
            ["deliverable_id: DEL-PRIMARY-2", "handoff_id: hnd-first-claimed-2a2b46"],
        )
        second_claimed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-21-second-alpha.md",
            ["deliverable_id: DEL-EXTRA-2", "handoff_id: hnd-second-claimed-2a2b50"],
        )
        _seed_handoff_claim(
            tmp_path, "sid-fan-in-2", first_claimed.name, claimed_at="2026-07-20T09:00:00Z"
        )
        _seed_handoff_claim(
            tmp_path, "sid-fan-in-2", second_claimed.name, claimed_at="2026-07-20T10:00:00Z"
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-fan-in-2")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        for directive in decision["directives"]:
            depends_on = directive.get("depends_on") or []
            assert "j-fan-in-cardinality" not in depends_on, directive


class TestFanInCardinalityJudgmentPointDoesNotFireOnSinglePredecessor:
    def test_single_predecessor_brief_has_no_fan_in_point_and_is_unaffected(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        single = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-single.md",
            ["deliverable_id: DEL-SOLO-1", "handoff_id: hnd-solo-1a2b48"],
        )
        _seed_handoff_claim(tmp_path, "sid-single", single.name, claimed_at="2026-07-20T09:00:00Z")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-single")

        without_fan_in = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        with_fan_in_disabled_again = ba.brief("handoff", "", repo_root=tmp_path).decision_object

        ids = {j["id"] for j in without_fan_in["judgment_points"]}
        assert "j-fan-in-cardinality" not in ids

        # AC-4: byte-identical repeated computation for a single-predecessor
        # session -- this stub's change must not perturb this path at all.
        assert without_fan_in == with_fan_in_disabled_again
