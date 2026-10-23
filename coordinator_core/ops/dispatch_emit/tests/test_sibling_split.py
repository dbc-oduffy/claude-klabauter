"""Sibling-workflow split: partitioning, firing manifest, digest merge, below-cap identity."""

from __future__ import annotations

import copy
import json

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import sibling_split as ss
from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import build_dag

from .conftest import REVIEW_KW


def _row(id_, writes=None, deps=()):
    return EmitterRow(
        id=id_,
        title=f"t-{id_}",
        surface="test",
        writes=writes if writes is not None else [f"{id_}.py"],
        reads=[],
        depends_on=[{"chunk": d, "gate_kind": "output-consumption-runtime"} for d in deps],
        appends=(),
    )


def _plan(rows) -> str:
    out = ""
    for row_id, deps in rows:
        edge = "".join(
            f"  depends_on:\n    - chunk: {d}\n      gate_kind: output-consumption-runtime\n" for d in deps[:1]
        )
        out += (
            f"- id: {row_id}\n  title: t{row_id}\n  change_kind: doc-edit\n"
            f"  surface: docs/x/{row_id}.md\n  writes:\n    - docs/x/{row_id}.md\n"
            f"  queue_scope: project\n  disposition: open\n{edge}  body: |\n    Do {row_id}.\n"
        )
    return (
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n"
    )


def _plan_file(tmp_path, rows):
    path = tmp_path / "p.md"
    path.write_text(_plan(rows), encoding="utf-8")
    return path


def test_runtime_cap_formula():
    assert [ss.runtime_cap(n) for n in (1, 2, 4, 8, 18, 64)] == [2, 2, 2, 6, 16, 16]


def test_independent_branches_form_one_stage_of_packed_siblings():
    dag = build_dag([_row(f"C{i}") for i in range(1, 7)])
    split = ss.plan_split(dag, 2)
    assert [s.rows for s in split.siblings] == [("C1", "C2"), ("C3", "C4"), ("C5", "C6")]
    assert {s.stage for s in split.siblings} == {1}
    assert split.achieved_width == 6 and split.plan_width == 6


def test_below_cap_dag_is_not_split():
    assert ss.plan_split(build_dag([_row("C1"), _row("C2")]), 2) is None


def test_cross_branch_dependency_is_level_segmented():
    rows = [_row("A"), _row("B"), _row("C"), _row("D", deps=("A", "B", "C"))]
    split = ss.plan_split(build_dag(rows), 2)
    assert [(s.stage, s.rows) for s in split.siblings] == [(1, ("A", "B")), (1, ("C",)), (2, ("D",))]
    assert [s.carrier for s in split.siblings] == [False, False, True]


def test_every_row_lands_in_exactly_one_sibling():
    rows = [_row(f"R{i}") for i in range(5)] + [_row("J", deps=("R0", "R1", "R2")), _row("K", deps=("J",))]
    split = ss.plan_split(build_dag(rows), 2)
    flat = [r for s in split.siblings for r in s.rows]
    assert sorted(flat) == sorted(r.id for r in rows)
    assert all(s.width <= 2 for s in split.siblings)


def test_manifest_states_firing_order_and_commit_carrier():
    rows = [_row("A"), _row("B"), _row("C"), _row("D", deps=("A", "B", "C"))]
    split = ss.plan_split(build_dag(rows), 2)
    m = ss.build_manifest(split, plan_path="p.md", script_paths={s.index: f"s{s.index}.mjs" for s in split.siblings})
    assert [(st["stage"], st["fire_together"]) for st in m["stages"]] == [(1, ["sib1", "sib2"]), (2, ["sib3"])]
    assert m["stages"][0]["fire_after"] == "emit time"
    assert "every stage 1 sibling" in m["stages"][1]["fire_after"]
    assert m["commit_carrier"] == "sib3"
    assert [s["commit_carrier"] for s in m["siblings"]] == [False, False, True]
    assert m["cap"] == 2 and m["plan_width"] == 3 and m["achieved_width"] == 3
    assert json.loads(json.dumps(m)) == m


@pytest.fixture
def cap2(monkeypatch):
    monkeypatch.setattr(ss, "runtime_cap", lambda cpus=None: 2)


ROWS = [("C1", ()), ("C2", ()), ("C3", ()), ("C4", ()), ("C5", ("C1",))]


def test_below_cap_emit_is_byte_identical_with_and_without_auto_split(tmp_path, monkeypatch):
    plan = _plan_file(tmp_path, ROWS)
    plain = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    monkeypatch.setattr(ss, "runtime_cap", lambda cpus=None: 16)
    assert emit_script(plan, repo_root=tmp_path, auto_split=True, **REVIEW_KW) == plain


def test_wide_plan_raises_with_split_only_under_auto_split(tmp_path, cap2):
    plan = _plan_file(tmp_path, ROWS)
    emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    with pytest.raises(ss.WidthOverCapError) as exc:
        emit_script(plan, repo_root=tmp_path, auto_split=True, **REVIEW_KW)
    assert exc.value.split.plan_width == 4


def _sibling_scripts(tmp_path, split):
    all_ids = [r for s in split.siblings for r in s.rows]
    out = {}
    for sib in split.siblings:
        note = ss.width_note(split, sib)
        out[sib.index] = emit_script(
            _plan_file(tmp_path, ROWS),
            repo_root=tmp_path,
            sibling={
                **note,
                "keep_rows": list(sib.rows),
                "terminal_commit": sib.carrier,
                "commit_scope": [r for r in all_ids if r not in sib.rows] if sib.carrier else [],
            },
            **REVIEW_KW,
        )
    return out


def test_only_the_carrier_commits_and_its_marker_covers_every_row(tmp_path, cap2):
    plan = _plan_file(tmp_path, ROWS)
    with pytest.raises(ss.WidthOverCapError) as exc:
        emit_script(plan, repo_root=tmp_path, auto_split=True, **REVIEW_KW)
    split = exc.value.split
    scripts = _sibling_scripts(tmp_path, split)
    markers = {i: parse_marker(s) for i, s in scripts.items()}
    carrier = next(s.index for s in split.siblings if s.carrier)
    assert [i for i, m in markers.items() if m is not None] == [carrier]
    assert sorted(c.id for c in markers[carrier].chunks) == ["C1", "C2", "C3", "C4", "C5"]
    assert all(c.paths for c in markers[carrier].chunks)
    for sib in split.siblings:
        ran = {r for r in ("C1", "C2", "C3", "C4", "C5") if f"_rows['{r}'] = _runRow(" in scripts[sib.index]}
        assert ran == set(sib.rows)
        assert "Sibling workflow" in scripts[sib.index]
        assert f"achieved width {split.achieved_width}" in scripts[sib.index]


def _digest(chunks, **over):
    d = {
        "schema": "wake-digest", "version": 1, "plan": {"path": "p.md", "deliverable_id": None},
        "outcome": "completed", "completed": True, "halted": None, "chunks": chunks,
        "criterion": {"status": "not_run", "observation": None, "sidecar": None},
        "tests": {
            "status": "pass", "run": 3, "failed": 0, "build_clean": True, "note": None, "sidecar": None,
            "per_row": {"verified": 1, "passed": 1, "failed": [], "unstructured": [], "skipped": []},
        },
        "review": {
            "status": "not_run", "slices": None, "fixes_applied": None, "em_may_think_differently": [],
            "unresolved": [], "overflow": 0, "brief_conformance": None, "rebuild_decision": None,
            "delivery": {"verdict": "not_run", "product_files": None, "claims_unbacked": None},
            "integration_sidecar": None,
        },
        "deviations": [], "run_base_sha": None,
        "width": {"rows": len(chunks), "max_concurrent_rows": 2, "critical_path_rows": 1,
                  "runtime_cap": "min(16, CPUs-2)", "runtime_cap_on_emitting_host": 2},
        "predispatch": None, "decision_required": None, "terminal_commit_cli": None,
        "next_action": {"kind": "none", "op": None, "params": None},
    }
    d.update(over)
    return d


def _manifest():
    rows = [_row("A"), _row("B"), _row("C"), _row("D", deps=("A", "B", "C"))]
    split = ss.plan_split(build_dag(rows), 2)
    return ss.build_manifest(split, plan_path="p.md", script_paths={})


def test_merge_digests_is_schema_valid_and_aggregates():
    m = _manifest()
    carrier_params = {"incomplete_chunks": ["D"], "inline_review": None, "held": []}
    digests = {
        "sib1": _digest(["A", "B"], deviations=[{"chunk": "B", "kind": "partial", "anchor": "x"}]),
        "sib2": _digest(
            ["C"], outcome="incomplete", completed=False,
            tests={**_digest([])["tests"], "status": "fail", "failed": 1, "run": 2, "build_clean": False},
            next_action={"kind": "terminal_commit", "op": "dispatch.terminal_commit",
                         "params": {"incomplete_chunks": ["C"], "inline_review": None}},
        ),
        "sib3": _digest(
            ["D"], run_base_sha="a" * 40,
            next_action={"kind": "terminal_commit", "op": "dispatch.terminal_commit", "params": carrier_params},
        ),
    }
    before = copy.deepcopy(digests)
    merged = ss.merge_digests(digests, m)
    assert wd.validate_digest(merged) == []
    assert digests == before
    assert merged["chunks"] == ["A", "B", "C", "D"]
    assert merged["outcome"] == "incomplete" and merged["completed"] is False
    assert merged["tests"]["status"] == "fail"
    assert merged["tests"]["run"] == 8 and merged["tests"]["failed"] == 1
    assert merged["tests"]["build_clean"] is False
    assert merged["tests"]["per_row"]["verified"] == 3
    assert merged["deviations"][0]["chunk"] == "B"
    assert merged["run_base_sha"] == "a" * 40
    assert merged["next_action"]["params"]["incomplete_chunks"] == ["C", "D"]
    assert merged["next_action"]["params"]["siblings_merged"] == [s["id"] for s in m["siblings"]]
    assert "siblings_merged" not in carrier_params
    assert merged["terminal_commit_cli"].startswith("coordinator-invoke dispatch.terminal_commit --from-task-output ")
    assert merged["width"]["max_concurrent_rows"] == m["achieved_width"]
    assert merged["width"]["rows"] == 4


def test_merge_requires_every_sibling_digest():
    with pytest.raises(ValueError, match="sib3"):
        ss.merge_digests({"sib1": _digest(["A"]), "sib2": _digest(["B"])}, _manifest())


def test_cli_emit_siblings_passes_scope_and_prints_manifest(tmp_path, monkeypatch, capsys):
    rows = [_row("A"), _row("B"), _row("C"), _row("D", deps=("A", "B", "C"))]
    split = ss.plan_split(build_dag(rows), 2)
    seen = []

    def fake_emit(params, repo_root=None):
        seen.append(params)
        return {"path": params["output_path"], "ok": True, "sha256": "x"}

    admissions = []
    from coordinator_core.ops.dispatch_emit import admission

    monkeypatch.setattr(
        admission, "await_admission", lambda *a, **k: admissions.append(1) or {"verdict": "admitted"}
    )
    monkeypatch.setattr(cli_module, "_dispatch_emit", fake_emit)
    rc = cli_module._emit_sibling_workflows(
        {"plan_path": "p.md", "output_path": str(tmp_path / "p.workflow.mjs")},
        None, ss.WidthOverCapError(split), None, False, {"verdict": "admitted"},
    )
    assert rc == cli_module.EXIT_OK
    assert [p["sibling"]["terminal_commit"] for p in seen] == [False, False, True]
    assert seen[2]["sibling"]["commit_scope"] == ["A", "B", "C"]
    assert seen[0]["output_path"].endswith("p.sib1.workflow.mjs")
    assert len(admissions) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["manifest"]["commit_carrier"] == "sib3"
    assert set(out["admission"]) == {"sib1", "sib2", "sib3"}
