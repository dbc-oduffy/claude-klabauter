"""`plan-spine-check`'s micro-chunk, depends-on-plan-status, contributed-signal, research-layout and
absolute-path finding classes."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_extra", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_extra"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


def _row(rid, writes=("a.py",), **extra):
    row = {"id": rid, "agent_type": "coordinator:executor", "writes": list(writes)}
    row.update(extra)
    return row


# --- micro-chunk-siblings -------------------------------------------------------------------------


def test_two_thin_same_wave_siblings_flagged():
    found = psc.micro_chunk_findings([_row("C1"), _row("C2", ("b.py",))])
    assert [f["class"] for f in found] == ["micro-chunk-siblings"]
    assert "C1, C2" in found[0]["detail"]


def test_siblings_carrying_enough_work_pass():
    rows = [_row("C1", change_kind="code-edit"), _row("C2", ("b.py",), change_kind="code-edit")]
    assert psc.micro_chunk_findings(rows) == []


def test_disjoint_parallel_row_is_exempt():
    assert psc.micro_chunk_findings([_row("C1", disjoint_parallel=True), _row("C2", ("b.py",))]) == []


def test_siblings_in_different_waves_pass():
    rows = [_row("C1"), _row("C2", ("b.py",), depends_on=[{"chunk": "C1"}])]
    assert psc.micro_chunk_findings(rows) == []


def test_deferred_and_non_executor_rows_do_not_count():
    rows = [_row("C1"), _row("C2", ("b.py",), disposition="deferred"), _row("C3", ("c.py",), agent_type="x:y")]
    assert psc.micro_chunk_findings(rows) == []


def test_dependency_cycle_yields_no_finding():
    rows = [_row("C1", depends_on=[{"chunk": "C2"}]), _row("C2", ("b.py",), depends_on=[{"chunk": "C1"}])]
    assert psc.micro_chunk_findings(rows) == []


# --- depends-on-plan-status-mismatch --------------------------------------------------------------


class _FakeGate:
    @staticmethod
    def plan_frontmatter(text):
        return {"status": text.split("status:", 1)[1].split()[0]}


def _dep_plan(tmp_path, cited_status):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "dep-plan.md").write_text(f"---\nstatus: {cited_status}\n---\n", encoding="utf-8")


_ROW = [{"id": "C1", "depends_on_plan": [{"plan": "docs/dep-plan.md", "status": "landed"}]}]


def test_gate_prose_met_but_plan_behind_is_flagged(tmp_path):
    _dep_plan(tmp_path, "approved")
    found = psc.depends_on_plan_status_findings("dep-plan gate is met\n", _ROW, tmp_path, _FakeGate)
    assert [f["class"] for f in found] == ["depends-on-plan-status-mismatch"]


def test_plan_at_named_status_is_not_flagged(tmp_path):
    _dep_plan(tmp_path, "landed")
    assert psc.depends_on_plan_status_findings("dep-plan gate is met\n", _ROW, tmp_path, _FakeGate) == []


def test_prose_not_claiming_met_is_not_flagged(tmp_path):
    _dep_plan(tmp_path, "approved")
    assert psc.depends_on_plan_status_findings("dep-plan gate is not met\n", _ROW, tmp_path, _FakeGate) == []


def test_spine_block_prose_is_ignored(tmp_path):
    _dep_plan(tmp_path, "approved")
    text = "```yaml plan-tasks\ndep-plan met\n```\n"
    assert psc.depends_on_plan_status_findings(text, _ROW, tmp_path, _FakeGate) == []


# --- contributed-signal-missing -------------------------------------------------------------------

_SIG = {
    "id": "p-signal", "plugin": "p", "selects": "p:reviewer", "model": "opus",
    "prose_terms": [], "write_triggers": [{"kind": "path-suffix", "value": ".zig"}],
}


def test_fired_trigger_without_declared_signal_is_flagged(tmp_path):
    rows = [{"id": "C1", "writes": ["src/a.zig"]}]
    found = psc.contributed_signal_findings({}, rows, tmp_path, contributed=[_SIG])
    assert [f["class"] for f in found] == ["contributed-signal-missing"]


def test_declared_signal_passes(tmp_path):
    rows = [{"id": "C1", "writes": ["src/a.zig"]}]
    assert psc.contributed_signal_findings({"review_signals": ["p-signal"]}, rows, tmp_path, contributed=[_SIG]) == []


def test_unfired_trigger_passes(tmp_path):
    rows = [{"id": "C1", "writes": ["src/a.py"]}]
    assert psc.contributed_signal_findings({}, rows, tmp_path, contributed=[_SIG]) == []


def test_no_contributed_signals_never_fires(tmp_path):
    assert psc.contributed_signal_findings({}, [{"id": "C1", "writes": ["a.zig"]}], tmp_path, contributed=[]) == []


# --- research-layout ------------------------------------------------------------------------------


def test_flat_research_path_flagged(tmp_path):
    rows = [{"id": "C1", "writes": ["docs/research/2026-11-05-flat-note.md"]}]
    found = psc.research_layout_findings(rows, tmp_path, today="2026-11-05")
    assert [f["class"] for f in found] == ["research-layout"]


def test_conforming_research_path_passes(tmp_path):
    rows = [{"id": "C1", "writes": ["docs/research/topic/2026-11-05-run/synthesis.md"]}]
    assert psc.research_layout_findings(rows, tmp_path, today="2026-11-05") == []


def test_existing_legacy_entry_is_grandfathered(tmp_path):
    (tmp_path / "docs" / "research").mkdir(parents=True)
    (tmp_path / "docs" / "research" / "2026-11-05-flat-note.md").write_text("x", encoding="utf-8")
    rows = [{"id": "C1", "writes": ["docs/research/2026-11-05-flat-note.md"]}]
    assert psc.research_layout_findings(rows, tmp_path, today="2026-11-05") == []


def test_non_research_path_ignored(tmp_path):
    assert psc.research_layout_findings([{"id": "C1", "writes": ["docs/plans/x.md"]}], tmp_path) == []


# --- absolute-path-literal ------------------------------------------------------------------------


def test_absolute_path_literal_flagged_and_marker_honoured():
    literal = "C" + ":/Users/x/repo/a.py"  # assembled so this file carries no literal itself
    bad = psc.abs_path_findings(f"see {literal} here\n")
    assert [f["class"] for f in bad] == ["absolute-path-literal"]
    ok = psc.abs_path_findings(f"see {literal} here abs-path-ok: example\n")
    assert ok == []


def test_repo_relative_text_passes():
    assert psc.abs_path_findings("see coordinator/bin/file.py here\n") == []
