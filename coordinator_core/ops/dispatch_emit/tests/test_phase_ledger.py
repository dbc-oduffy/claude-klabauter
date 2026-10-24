"""Contract tests for phase_ledger."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import phase_ledger as pl


def test_review_blocked_invalidates_review_and_dependents_only():
    out = pl.invalidated("review_blocked", revised=())
    assert out == {"review", "test", "criterion", "commit"}
    assert not out & {"gate", "size", "plan", "plan_review", "plan_gate_rulings"}


def test_ruling_halt_maps_to_plan_gate_rulings():
    out = pl.invalidated("plan_gate")
    assert "plan_gate_rulings" in out and "plan_review" not in out and "commit" in out


def test_revise_plan_input_invalidates_plan_review_downstream():
    out = pl.invalidated(None, ["plan"])
    assert out == set(pl.PHASES) - {"gate", "size", "plan"}


def test_revise_requirement_note_invalidates_only_criterion_commit():
    assert pl.invalidated(None, ["requirement"]) == {"criterion", "commit"}


def test_unknown_halt_kind_or_input_is_full_pass():
    assert pl.invalidated("mystery") == frozenset(pl.PHASES)
    assert pl.invalidated(None, ["mystery"]) == frozenset(pl.PHASES)


def test_dag_is_closed_over_phases():
    assert set(pl.DEPENDS) == set(pl.PHASES)
    assert all(d in pl.PHASES for deps in pl.DEPENDS.values() for d in deps)
    assert set(pl.HALT_PHASE.values()) <= set(pl.PHASES)


def test_unsafe_run_id_reads_empty(tmp_path):
    assert pl.derive(tmp_path, "../x") == {}
    assert pl.derive(tmp_path, "") == {}
    assert pl.derive(tmp_path, "absent-run") == {}


def test_derive_reads_kept_json(tmp_path):
    d = tmp_path / "scratch" / "warp" / "run-1"
    d.mkdir(parents=True)
    kept = {"chunks": {"C1": {"paths": {}}}, "plan_gate": {"plan": "p.md", "plan_sha256": "ab", "reviews": [1]}}
    (d / "kept.json").write_text(json.dumps(kept), encoding="utf-8")
    got = pl.derive(tmp_path, "run-1")
    assert set(got) == {"plan", "plan_review", "execute"}
    assert got["execute"] == {"chunks": ["C1"]}


def test_derive_malformed_is_empty(tmp_path):
    d = tmp_path / "scratch" / "warp" / "run-1"
    d.mkdir(parents=True)
    (d / "kept.json").write_text("[1", encoding="utf-8")
    assert pl.derive(tmp_path, "run-1") == {}


def test_resume_phases_shape_reuses_valid_and_reruns_invalid():
    landed = {"plan": {"a": 1}, "plan_review": {"b": 2}, "execute": {"c": 3}}
    out = pl.resume_phases(landed, "review_blocked")
    assert out["reuse"] == ["plan", "plan_review", "execute"]
    assert out["rerun"] == ["gate", "size", "plan_gate_rulings", "review", "test", "criterion", "commit"]
    assert out["verdicts"] == landed
    revised = pl.resume_phases(landed, None, ["plan"])
    assert revised["reuse"] == ["plan"] and "execute" in revised["rerun"]
    assert pl.RESUME_PHASES_FIELD == "resume_phases"
