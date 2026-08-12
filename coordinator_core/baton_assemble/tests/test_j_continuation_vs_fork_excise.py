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

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _write_artifact


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- `brief()` calls `resolve_operator_config()` unconditionally (B0 seam
    assertion), which resolves real per-machine settings_home/claude_klabauter_root
    values absent this stub. Mirrors
    `test_deliverable_collision_warn.py`'s own fixture of the same name."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _write_predecessor_and_artifact(tmp_path):
    """A predecessor handoff (carrying its own `handoff_id`) plus an
    artifact naming it via `predecessor:` and a `deliverable_id` -- the
    same two-file shape `TestKindParametrizedCascade`'s own predecessor-
    resolution test in `coordinator_core/test_baton_assemble.py` uses, so
    the ordinary (non-excise) predecessor-resolution path this module
    exercises against is proven to actually resolve a predecessor before
    excise is asked to discard it."""
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
        """The whole point of `excise` over the pre-existing empty-
        `artifact_path` standalone shape: lineage carry survives the
        predecessor excision."""
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
        """Strictly additive: an ordinary `continue`/no-decision brief still
        threads --predecessor/--predecessor-id into d1 and still arms d6."""
        _predecessor, artifact = _write_predecessor_and_artifact(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert any(a.startswith("--predecessor=") for a in d1["args"]), d1["args"]
        assert any(a.startswith("--predecessor-id=") for a in d1["args"]), d1["args"]
        ids = {d["id"] for d in decision["directives"]}
        assert "d6" in ids, ids

    def test_legacy_nonconforming_predecessor_id_omitted(self, tmp_path):
        """A realistic legacy `handoff_id` (pre-`hnd-<slug>-<6hex>` convention)
        drives the omission path: `--predecessor-id` is left off entirely
        while `--predecessor` still threads through, since the path edge and
        the id edge are independently gated."""
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
