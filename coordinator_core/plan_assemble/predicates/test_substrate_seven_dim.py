from __future__ import annotations

from pathlib import Path

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates import substrate_seven_dim as m


def _ctx(
    tmp_path: Path,
    *,
    plan_path=None,
    plan_frontmatter=None,
    plan_body=None,
    sizing_object_path=None,
    sizing_frontmatter=None,
    caller_flags=None,
) -> PredicateContext:
    return PredicateContext(
        repo_root=tmp_path,
        plan_path=plan_path,
        plan_frontmatter=plan_frontmatter,
        plan_body=plan_body,
        sizing_object_path=sizing_object_path,
        sizing_frontmatter=sizing_frontmatter,
        resolved_route="plan",
        caller_flags=caller_flags or {},
    )


def test_problem_set_present_inline(tmp_path):
    ctx = _ctx(tmp_path, plan_frontmatter={"problem_set": "inline"})
    result = m.problem_set(ctx)
    assert result == {"present": True, "path": None}


def test_problem_set_present_with_file(tmp_path):
    (tmp_path / "docs" / "problems").mkdir(parents=True)
    (tmp_path / "docs" / "problems" / "foo.md").write_text("x", encoding="utf-8")
    ctx = _ctx(tmp_path, plan_frontmatter={"problem_set": "foo.md"})
    result = m.problem_set(ctx)
    assert result["present"] is True
    assert result["path"] == "docs/problems/foo.md"


def test_problem_set_absent_key():
    ctx = _ctx(Path("/tmp"), plan_frontmatter={})
    result = m.problem_set(ctx)
    assert result == {"present": False, "path": None}


def test_problem_set_undetermined_no_plan(tmp_path):
    ctx = _ctx(tmp_path, plan_frontmatter=None)
    result = m.problem_set(ctx)
    assert result["present"]["undetermined"] is True
    assert result["path"] is None


def test_scope_mode_surfaces_frontmatter_value(tmp_path):
    ctx = _ctx(tmp_path, plan_frontmatter={"scope_mode": "spec-dispatch"})
    assert m.scope_mode(ctx) == {"value": "spec-dispatch"}


def test_scope_mode_undetermined_when_key_absent(tmp_path):
    ctx = _ctx(tmp_path, plan_frontmatter={})
    result = m.scope_mode(ctx)
    assert result["value"]["undetermined"] is True


def test_scope_mode_undetermined_no_plan(tmp_path):
    ctx = _ctx(tmp_path, plan_frontmatter=None)
    result = m.scope_mode(ctx)
    assert result["value"]["undetermined"] is True


def test_seven_dim_no_duplicate_true_when_sidecar_present(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    sidecar_dir = tmp_path / ".coordinator-local" / "plan-sidecars"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "my-plan.prior-art-check.md").write_text("x", encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path)
    assert m.seven_dim_no_duplicate(ctx) is True


def test_seven_dim_no_duplicate_undetermined_when_sidecar_absent(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path)
    result = m.seven_dim_no_duplicate(ctx)
    assert result["undetermined"] is True


def test_seven_dim_no_duplicate_undetermined_no_plan(tmp_path):
    ctx = _ctx(tmp_path, plan_path=None)
    result = m.seven_dim_no_duplicate(ctx)
    assert result["undetermined"] is True


def test_seven_dim_no_fabrication_true_when_all_citations_resolve(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    (tmp_path / "real" / "file.py").parent.mkdir(parents=True)
    (tmp_path / "real" / "file.py").write_text("x", encoding="utf-8")
    body = "See `real/file.py` for details.\n"
    plan_path.write_text("---\ntitle: x\n---\n" + body, encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path, plan_body=body)
    result = m.seven_dim_no_fabrication(ctx)
    assert result["no_fabrication"] is True
    assert result["absent_citations"] == []


def test_seven_dim_no_fabrication_false_on_absent_citation(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    body = "See `nonexistent/file.py` for details.\n"
    plan_path.write_text("---\ntitle: x\n---\n" + body, encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path, plan_body=body)
    result = m.seven_dim_no_fabrication(ctx)
    assert result["no_fabrication"] is False
    assert result["absent_citations"] == [{"line": 1, "token": "nonexistent/file.py"}]


def test_seven_dim_no_fabrication_true_for_doc_relative_citation(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    (plan_path.parent / "sibling.py").write_text("x", encoding="utf-8")
    body = "See `sibling.py` for details.\n"
    plan_path.write_text("---\ntitle: x\n---\n" + body, encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path, plan_body=body)
    result = m.seven_dim_no_fabrication(ctx)
    assert result["no_fabrication"] is True
    assert result["absent_citations"] == []


def test_seven_dim_no_fabrication_undetermined_no_plan(tmp_path):
    ctx = _ctx(tmp_path, plan_path=None, plan_body=None)
    result = m.seven_dim_no_fabrication(ctx)
    assert result["undetermined"] is True


def test_seven_dim_official_docs_read_true_when_sidecar_present(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    sidecar_dir = tmp_path / ".coordinator-local" / "plan-sidecars"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "my-plan.docs-check.md").write_text("x", encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path)
    assert m.seven_dim_official_docs_read(ctx) is True


def test_seven_dim_official_docs_read_undetermined_when_sidecar_absent(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    ctx = _ctx(tmp_path, plan_path=plan_path)
    result = m.seven_dim_official_docs_read(ctx)
    assert result["undetermined"] is True


def test_seven_dim_reference_impl_seen_true_on_file_line_citation(tmp_path):
    body = "See coordinator_core/foo.py:42 for the reference implementation.\n"
    ctx = _ctx(tmp_path, plan_body=body)
    assert m.seven_dim_reference_impl_seen(ctx) is True


def test_seven_dim_reference_impl_seen_false_with_no_citation(tmp_path):
    ctx = _ctx(tmp_path, plan_body="No citations here at all.\n")
    assert m.seven_dim_reference_impl_seen(ctx) is False


def test_seven_dim_reference_impl_seen_undetermined_no_plan_body(tmp_path):
    result = m.seven_dim_reference_impl_seen(_ctx(tmp_path, plan_body=None))
    assert result["undetermined"] is True


# --- :94 premise_gate.m_band_uncovered (reads _PREMISE_DETENT_TSHIRTS, never widens) --


def test_premise_gate_m_band_uncovered_false_for_m(tmp_path):
    # M is IN _PREMISE_DETENT_TSHIRTS — the premise_unproven detent DOES
    ctx = _ctx(tmp_path, sizing_frontmatter={"estimate": {"tshirt": "M"}})
    result = m.premise_gate(ctx)
    assert result == {"m_band_uncovered": False, "tshirt": "M"}


def test_premise_gate_m_band_uncovered_false_for_large_tshirts(tmp_path):
    from coordinator_core.sizing_assemble import _PREMISE_DETENT_TSHIRTS

    for tshirt in _PREMISE_DETENT_TSHIRTS:
        ctx = _ctx(tmp_path, sizing_frontmatter={"estimate": {"tshirt": tshirt}})
        result = m.premise_gate(ctx)
        assert result["m_band_uncovered"] is False, tshirt


def test_premise_gate_distinguishes_premise_detent_from_large_tshirts(tmp_path):
    # Pin the two constants apart: _LARGE_TSHIRTS excludes "M",
    # _PREMISE_DETENT_TSHIRTS includes it. If a future edit merges them
    # (or this row starts reading _LARGE_TSHIRTS again), this must fail.
    from coordinator_core.sizing_assemble import _LARGE_TSHIRTS, _PREMISE_DETENT_TSHIRTS

    assert "M" not in _LARGE_TSHIRTS
    assert "M" in _PREMISE_DETENT_TSHIRTS
    assert _LARGE_TSHIRTS != _PREMISE_DETENT_TSHIRTS

    ctx = _ctx(tmp_path, sizing_frontmatter={"estimate": {"tshirt": "M"}})
    result = m.premise_gate(ctx)
    assert result["m_band_uncovered"] is False


def test_premise_gate_m_band_uncovered_false_for_small(tmp_path):
    ctx = _ctx(tmp_path, sizing_frontmatter={"estimate": {"tshirt": "S"}})
    result = m.premise_gate(ctx)
    assert result["m_band_uncovered"] is False
    assert result["tshirt"] == "S"


def test_premise_gate_undetermined_no_sizing_object(tmp_path):
    ctx = _ctx(tmp_path, sizing_frontmatter=None)
    result = m.premise_gate(ctx)
    assert result["m_band_uncovered"]["undetermined"] is True
    assert result["tshirt"] is None


def test_premise_gate_undetermined_no_estimate_key(tmp_path):
    ctx = _ctx(tmp_path, sizing_frontmatter={"intent": "x"})
    result = m.premise_gate(ctx)
    assert result["m_band_uncovered"]["undetermined"] is True


def test_premise_gate_reads_premise_detent_tshirts_not_hardcoded():
    # Bug-for-bug guard: the row must observe `_PREMISE_DETENT_TSHIRTS`
    import coordinator_core.sizing_assemble as sizing_assemble

    original = sizing_assemble._PREMISE_DETENT_TSHIRTS
    try:
        sizing_assemble._PREMISE_DETENT_TSHIRTS = ("L", "XL", "XXL")
        ctx = _ctx(Path("/tmp"), sizing_frontmatter={"estimate": {"tshirt": "M"}})
        result = m.premise_gate(ctx)
        assert result["m_band_uncovered"] is True
    finally:
        sizing_assemble._PREMISE_DETENT_TSHIRTS = original


def test_trampoline_verdict_cited_true_when_record_present(tmp_path):
    verdict_dir = tmp_path / "docs" / "research" / "spike-verdicts"
    verdict_dir.mkdir(parents=True)
    record = verdict_dir / "2026-08-13-fixture.md"
    record.write_text("---\nverdict: viable\n---\nbody\n", encoding="utf-8")
    ctx = _ctx(
        tmp_path,
        sizing_frontmatter={
            "premise": {"spike_verdict": "docs/research/spike-verdicts/2026-08-13-fixture.md"}
        },
    )
    result = m.trampoline_verdict(ctx)
    assert result == {
        "verdict_cited": True,
        "verdict_path": "docs/research/spike-verdicts/2026-08-13-fixture.md",
        "verdict": "viable",
    }


def test_trampoline_verdict_cited_false_when_no_spike_verdict_key(tmp_path):
    ctx = _ctx(tmp_path, sizing_frontmatter={"premise": {"provenance": "read"}})
    result = m.trampoline_verdict(ctx)
    assert result == {"verdict_cited": False, "verdict_path": None, "verdict": None}


def test_trampoline_verdict_cited_false_when_record_missing(tmp_path):
    ctx = _ctx(
        tmp_path,
        sizing_frontmatter={"premise": {"spike_verdict": "docs/research/spike-verdicts/gone.md"}},
    )
    result = m.trampoline_verdict(ctx)
    assert result["verdict_cited"] is False
    assert result["verdict_path"] == "docs/research/spike-verdicts/gone.md"
    assert result["verdict"] is None


def test_trampoline_verdict_undetermined_no_sizing_object(tmp_path):
    ctx = _ctx(tmp_path, sizing_frontmatter=None)
    result = m.trampoline_verdict(ctx)
    assert result["verdict_cited"]["undetermined"] is True


def test_trampoline_dec4_signal_true(tmp_path):
    ctx = _ctx(tmp_path, caller_flags={"trampoline": True})
    assert m.trampoline_dec4_signal(ctx) is True


def test_trampoline_dec4_signal_false(tmp_path):
    ctx = _ctx(tmp_path, caller_flags={"trampoline": False})
    assert m.trampoline_dec4_signal(ctx) is False


def test_trampoline_dec4_signal_undetermined_when_flag_absent(tmp_path):
    ctx = _ctx(tmp_path, caller_flags={})
    result = m.trampoline_dec4_signal(ctx)
    assert result["undetermined"] is True


def test_compute_assembles_all_rows(tmp_path):
    plan_path = tmp_path / "docs" / "plans" / "my-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")
    ctx = _ctx(
        tmp_path,
        plan_path=plan_path,
        plan_frontmatter={"problem_set": "inline", "scope_mode": "spec-dispatch"},
        plan_body="body\n",
        sizing_frontmatter={"estimate": {"tshirt": "M"}},
    )
    result = m.compute(ctx)
    assert set(result.keys()) == {
        "problem_set",
        "scope_mode",
        "seven_dim",
        "premise_gate",
        "trampoline",
    }
    assert set(result["seven_dim"].keys()) == {
        "no_duplicate",
        "no_fabrication",
        "official_docs_read",
        "reference_impl_seen",
    }
    assert result["premise_gate"]["m_band_uncovered"] is False
