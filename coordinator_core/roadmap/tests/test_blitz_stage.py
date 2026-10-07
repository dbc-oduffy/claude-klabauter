"""roadmap.blitz_stage: stubs scaffolded and numbered from the roadmap's own
cluster edges, size reported against the M/L band, audit surfaced, gate report
frozen in plan-blitz's input shape, and a rerun that duplicates nothing."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.roadmap import blitz_stage as bs

CLUSTERS = """# Clusters — rm-test

> Stub slug prefix: `rmt`.

## C1 — Transport
**Rows:** A. **loe:** M
**blocked_by:** none inside the roadmap. **blocks:** C2.

## C2 — Outlets
**Rows:** B. **loe:** L
**blocked_by:** C1. **blocks:** C3.

## C3 — The ban
**Rows:** C. **loe:** M
**blocked_by:** C2.

## C4 — Side
**Rows:** D. **loe:** M
**blocked_by:** none.
"""

CLUSTERS_WITH_XS = CLUSTERS.replace("D. **loe:** M", "D. **loe:** XS")

RECON = """| Cluster | Verdict |
|---|---|
| C1 | **KEEP** |
| C2 | **KEEP** |
| C3 | **KEEP** |
| C4 | **KEEP** |
"""


@pytest.fixture(autouse=True)
def commit_calls(monkeypatch):
    calls = []

    def fake(repo_root, paths, message):
        calls.append({"paths": list(paths), "message": message})
        return {"committed": True, "sha": f"sha{len(calls)}"}

    monkeypatch.setattr(bs, "_commit_paths", fake)
    return calls


def _make_roadmap(tmp_path: Path, roadmap_id: str, clusters: str) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    run = tmp_path / "state" / "roadmap" / roadmap_id
    run.mkdir(parents=True)
    (run / "clusters.md").write_text(clusters, encoding="utf-8")
    (run / "reconciliation.md").write_text(RECON, encoding="utf-8")
    (run / "OVERVIEW.md").write_text(f"---\nroadmap_id: {roadmap_id}\n---\n# o\n", encoding="utf-8")
    return run


@pytest.fixture
def roadmap(tmp_path: Path) -> Path:
    return _make_roadmap(tmp_path, "rm-test", CLUSTERS)


def _fm(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---")[1]


def test_stubs_scaffolded_and_numbered_from_cluster_edges(tmp_path, roadmap):
    reply = bs.stage_roadmap(tmp_path, str(roadmap / "OVERVIEW.md"))
    assert reply["edge_source"] == "clusters.md"
    by_cluster = {s["cluster"]: s for s in reply["stubs"]}
    assert [by_cluster[c]["stub_id"] for c in ("C1", "C2", "C3")] == ["rmt-01", "rmt-02", "rmt-03"]
    assert by_cluster["C2"]["blocked_by"] == [by_cluster["C1"]["stub_id"]]
    fm = _fm(tmp_path / by_cluster["C2"]["path"])
    assert "kind: roadmap-baton" in fm and 'roadmap_id: "rm-test"' in fm
    assert f"  - \"{by_cluster['C1']['stub_id']}\"" in fm or f"  - {by_cluster['C1']['stub_id']}" in fm
    assert "covers:\n  - \"C2\"" in fm
    assert "loe: L" in fm
    assert 'deliverable_id: "dlv-' + by_cluster["C2"]["stub_id"] + '-' in fm
    assert reply["numbering"]["C3"]["wave"] > reply["numbering"]["C2"]["wave"] > reply["numbering"]["C1"]["wave"]


def test_date_led_roadmap_id_without_prefix_line_is_refused(tmp_path):
    run = _make_roadmap(tmp_path, "2026-10-06-alpha", CLUSTERS.replace("> Stub slug prefix: `rmt`.\n", ""))
    with pytest.raises(bs.BlitzStageRefused, match="date-led"):
        bs.stage_roadmap(tmp_path, str(run / "OVERVIEW.md"))
    assert not (tmp_path / "state" / "handoffs").exists()


def test_date_led_roadmap_id_with_prefix_line_stages(tmp_path):
    run = _make_roadmap(tmp_path, "2026-10-06-alpha", CLUSTERS)
    reply = bs.stage_roadmap(tmp_path, str(run / "OVERVIEW.md"))
    assert {s["stub_id"] for s in reply["stubs"]} >= {"rmt-01"}


def test_edges_path_overrides_cluster_edges(tmp_path, roadmap):
    (roadmap / "clusters.md").write_text(CLUSTERS_WITH_XS, encoding="utf-8")
    edges = tmp_path / "edges.txt"
    edges.write_text("C1\nC2\nC3\nC4\nC1 <- C4\n", encoding="utf-8")
    reply = bs.stage_roadmap(tmp_path, str(roadmap), edges_path=str(edges))
    assert reply["edge_source"] == "edges_path"
    by_cluster = {s["cluster"]: s for s in reply["stubs"]}
    assert by_cluster["C1"]["covers"] == ["C4", "C1"]
    assert by_cluster["C1"]["blocked_by"] == []
    assert by_cluster["C2"]["blocked_by"] == []


def test_edge_free_small_cluster_is_flagged_unfoldable(tmp_path, roadmap):
    (roadmap / "clusters.md").write_text(CLUSTERS_WITH_XS, encoding="utf-8")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    ids = {s["cluster"]: s["stub_id"] for s in reply["stubs"]}
    folds = reply["folds"]
    assert set(folds["in_band"]) == {ids["C1"], ids["C2"], ids["C3"]}
    assert folds["merged"] == [] and folds["split"] == []
    assert folds["flagged"] == [
        {"stub_id": ids["C4"], "cluster": "C4", "reason": "unfoldable-small: no edge"}
    ]
    assert folds["routes"][ids["C1"]] == "plan"
    assert "ambiguities" not in folds and "unsized" not in folds


def test_every_cluster_missing_loe_is_named_in_one_refusal(tmp_path, roadmap):
    (roadmap / "clusters.md").write_text(
        "## C1 — a\n**loe:** M\n\n## C2 — b\n\n## C3 — c\n**blocked_by:** C1.\n", encoding="utf-8"
    )
    with pytest.raises(bs.BlitzStageRefused) as exc:
        bs.stage_roadmap(tmp_path, str(roadmap))
    assert str(exc.value).endswith("C2, C3")
    assert not (tmp_path / "state" / "handoffs").exists()


def _write_clusters(roadmap, body):
    (roadmap / "clusters.md").write_text(body, encoding="utf-8")
    (roadmap / "reconciliation.md").unlink()


def test_small_folds_into_its_sole_dependent_until_it_reaches_m(tmp_path, roadmap):
    _write_clusters(roadmap, """## C1 — a
**loe:** XS
**blocks:** C2.

## C2 — b
**loe:** S
**blocked_by:** C1. **blocks:** C3.

## C3 — c
**loe:** M
**blocked_by:** C2.
""")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert len(reply["stubs"]) == 1
    (stub,) = reply["stubs"]
    assert stub["covers"] == ["C1", "C2", "C3"] and stub["loe"] == "M"
    assert reply["folds"]["merged"] == [{"stub_id": stub["stub_id"], "sources": ["C1", "C2", "C3"], "loe": "M"}]
    assert "covers:\n  - \"C1\"\n  - \"C2\"\n  - \"C3\"" in _fm(tmp_path / stub["path"])
    assert reply["folds"]["flagged"] == []


def test_small_stops_folding_once_weight_reaches_m(tmp_path, roadmap):
    _write_clusters(roadmap, """## C1 — a
**loe:** S
**blocks:** C2.

## C2 — b
**loe:** S
**blocked_by:** C1. **blocks:** C3.

## C3 — c
**loe:** L
**blocked_by:** C2.
""")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert [s["covers"] for s in reply["stubs"]] == [["C1", "C2"], ["C3"]]
    assert [s["loe"] for s in reply["stubs"]] == ["M", "L"]


SEVERAL_DEPENDENTS = """## C1 — a
**loe:** XS
**blocks:** C2, C3.

## C2 — b
**loe:** M
**blocked_by:** C1.

## C3 — c
**loe:** M
**blocked_by:** C1.
"""


def test_tie_break_folds_into_earliest_candidate_and_records_the_key(tmp_path, roadmap):
    _write_clusters(roadmap, SEVERAL_DEPENDENTS)
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert len(reply["stubs"]) == 2 and reply["folds"]["flagged"] == []
    (tie,) = reply["folds"]["tie_breaks"]
    assert (tie["absorbed"], tie["into"], tie["candidates"]) == ("C1", "C2", ["C2", "C3"])
    assert tie["key"] == bs.TIE_BREAK_KEY
    assert {s["cluster"]: s["covers"] for s in reply["stubs"]}["C2"] == ["C1", "C2"]


def test_tie_break_is_deterministic(tmp_path):
    order = ["C1", "C2", "C3"]
    edges = [{"from": "C2", "to": "C1"}, {"from": "C3", "to": "C1"}]
    runs = [
        bs.fold_units(order, {"C1": "XS", "C2": "M", "C3": "M"}, edges, tie_break=True)
        for _ in range(3)
    ]
    assert all(r["tie_breaks"] == runs[0]["tie_breaks"] for r in runs)
    assert runs[0]["tie_breaks"][0]["into"] == "C2"


def test_without_tie_break_several_candidates_stay_flagged():
    edges = [{"from": "C2", "to": "C1"}, {"from": "C3", "to": "C1"}]
    fold = bs.fold_units(["C1", "C2", "C3"], {"C1": "XS", "C2": "M", "C3": "M"}, edges)
    assert fold["flagged"] == [{"cluster": "C1", "reason": "unfoldable-small: several candidates"}]
    assert fold["tie_breaks"] == []


def test_resume_does_not_refold_with_the_tie_break(tmp_path, roadmap):
    _write_clusters(roadmap, SEVERAL_DEPENDENTS)
    handoffs = tmp_path / "state" / "handoffs"
    bs_fold = bs.fold_units
    try:
        bs.fold_units = lambda *a, **k: bs_fold(*a, **{**k, "tie_break": False})
        legacy = bs.stage_roadmap(tmp_path, str(roadmap))
    finally:
        bs.fold_units = bs_fold
    assert len(legacy["stubs"]) == 3
    before = sorted((p.name, p.read_bytes()) for p in handoffs.glob("*.md"))
    again = bs.stage_roadmap(tmp_path, str(roadmap))
    assert len(again["stubs"]) == 3 and all(s["created"] is False for s in again["stubs"])
    assert again["folds"]["tie_breaks"] == []
    assert sorted((p.name, p.read_bytes()) for p in handoffs.glob("*.md")) == before


def test_merge_that_would_reach_xl_is_refused(tmp_path, roadmap):
    _write_clusters(roadmap, """## C1 — a
**loe:** S
**blocks:** C2.

## C2 — b
**loe:** XL
**blocked_by:** C1.
""")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    reasons = {f["cluster"]: f["reason"] for f in reply["folds"]["flagged"]}
    assert reasons["C1"] == "unfoldable-small: merge would reach XL" and reasons["C2"] == "xl-review"
    assert len(reply["stubs"]) == 2


def test_xxl_is_flagged_mis_made_and_never_split(tmp_path, roadmap):
    _write_clusters(roadmap, "## C1 — a\n**loe:** XXL\n")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert reply["folds"]["flagged"][0]["reason"] == "mis-made: XXL"
    assert reply["folds"]["split"] == [] and len(reply["stubs"]) == 1


def test_small_with_no_dependent_folds_into_its_sole_dependency(tmp_path, roadmap):
    _write_clusters(roadmap, """## C1 — a
**loe:** L
**blocks:** C2.

## C2 — b
**loe:** XS
**blocked_by:** C1.
""")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    (stub,) = reply["stubs"]
    assert stub["covers"] == ["C1", "C2"] and stub["loe"] == "L"
    assert reply["folds"]["merged"][0]["sources"] == ["C1", "C2"]


def test_a_dependent_is_preferred_over_a_dependency(tmp_path, roadmap):
    _write_clusters(roadmap, """## C1 — a
**loe:** M
**blocks:** C2.

## C2 — b
**loe:** XS
**blocked_by:** C1. **blocks:** C3.

## C3 — c
**loe:** M
**blocked_by:** C2.
""")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert [s["covers"] for s in reply["stubs"]] == [["C1"], ["C2", "C3"]]


def test_edge_free_stubs_share_a_wave_and_the_dependent_is_later(tmp_path, roadmap):
    (roadmap / "reconciliation.md").write_text(
        "| Cluster | Verdict |\n|---|---|\n| C1 | **KEEP** |\n| C2 | **KEEP** |\n| C3 | **KEEP** |\n",
        encoding="utf-8",
    )
    (roadmap / "clusters.md").write_text("""## C1 — a
**loe:** M

## C2 — b
**loe:** M

## C3 — c
**loe:** L
**blocked_by:** C1.
""", encoding="utf-8")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    wave = {s["cluster"]: s["wave"] for s in reply["stubs"]}
    assert wave["C1"] == wave["C2"] == 1 and wave["C3"] == 2
    report = json.loads((tmp_path / reply["gate_report_path"]).read_text(encoding="utf-8"))
    assert all(isinstance(w, list) and all(isinstance(i, str) for i in w) for w in report["waves"])
    assert sum(len(w) for w in report["waves"]) == 3
    assert len(reply["stubs"]) == 3


def test_unknown_size_is_refused(tmp_path, roadmap):
    from coordinator_core.roadmap.blitz_stage import fold_units

    with pytest.raises(bs.BlitzStageRefused, match="expected one of"):
        fold_units(["C1"], {"C1": "HUGE"}, [])


def test_audit_result_is_surfaced(tmp_path, roadmap):
    (roadmap / "clusters.md").write_text(CLUSTERS_WITH_XS, encoding="utf-8")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    audit = reply["audit"]
    assert set(audit) == {"exit_code", "passed", "stdout", "stderr"}
    assert any("Stub-coverage: all 4 KEEP cluster(s) named exactly once" in line for line in audit["stdout"])
    assert audit["passed"] is True and audit["stderr"] == []


def test_unfoldable_small_stub_is_exempt_from_audit7_and_named(tmp_path, roadmap):
    (roadmap / "clusters.md").write_text(CLUSTERS_WITH_XS, encoding="utf-8")
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    c4 = {s["cluster"]: s["stub_id"] for s in reply["stubs"]}["C4"]
    assert reply["unfoldable_small"] == [c4]
    assert any("Audit 7" in line and "unfoldable_small" in line and c4 in line for line in reply["audit"]["stdout"])
    assert reply["gate_report_state"] == "frozen-now"
    report = json.loads((tmp_path / reply["gate_report_path"]).read_text(encoding="utf-8"))
    assert report["unfoldable_small"] == [c4]
    (baton,) = (tmp_path / "state" / "handoffs").glob(f"*roadmap-{c4}.md")
    assert "fold_flag: unfoldable-small" in baton.read_text(encoding="utf-8")


def test_unflagged_small_stub_still_fails_audit7(tmp_path, roadmap):
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert reply["audit"]["passed"] is True
    victim = next(s for s in reply["stubs"] if s["cluster"] == "C3")
    path = tmp_path / victim["path"]
    path.write_text(path.read_text(encoding="utf-8").replace("loe: M", "loe: S"), encoding="utf-8")
    (tmp_path / reply["gate_report_path"]).chmod(0o644)
    (tmp_path / reply["gate_report_path"]).unlink()
    again = bs.stage_roadmap(tmp_path, str(roadmap))
    assert again["audit"]["passed"] is False
    assert any("Audit 7" in line and victim["stub_id"] in line for line in again["audit"]["stderr"])
    assert again["gate_report_state"] == "deferred-audit-failed"
    assert again["gate_report_path"] is None


def test_gate_report_frozen_in_plan_gate_shape(tmp_path, roadmap):
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert reply["gate_report_state"] == "frozen-now"
    path = tmp_path / reply["gate_report_path"]
    assert reply["gate_report_path"] == "state/plan-blitz/rm-test/wave-1.gate-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("batons", "waves", "cycles", "unresolved_blockers", "untracked", "counts"):
        assert key in payload
    assert "result" not in payload
    assert {b["stub_id"] for b in payload["batons"]} >= {s["stub_id"] for s in reply["stubs"]}
    assert not os.access(path, os.W_OK)


def test_rerun_duplicates_no_stub_and_keeps_the_frozen_report(tmp_path, roadmap):
    first = bs.stage_roadmap(tmp_path, str(roadmap))
    report = tmp_path / first["gate_report_path"]
    before = report.read_bytes()
    files_before = sorted(p.name for p in (tmp_path / "state" / "handoffs").iterdir())
    second = bs.stage_roadmap(tmp_path, str(roadmap))
    assert sorted(p.name for p in (tmp_path / "state" / "handoffs").iterdir()) == files_before
    assert all(s["created"] is False for s in second["stubs"])
    assert [s["stub_id"] for s in second["stubs"]] == [s["stub_id"] for s in first["stubs"]]
    assert second["gate_report_state"] == "already-frozen"
    assert report.read_bytes() == before


def test_untracked_stubs_defer_the_gate_report(tmp_path, roadmap, monkeypatch):
    from coordinator_core.roadmap import plan_gate

    monkeypatch.setattr(plan_gate, "_tracked_paths", lambda root: (frozenset(), None))
    reply = bs.stage_roadmap(tmp_path, str(roadmap), commit=False)
    assert reply["gate_report_path"] is None
    assert reply["gate_report_state"] == "deferred-untracked"
    assert len(reply["untracked_stubs"]) == len(reply["stubs"])
    assert not (tmp_path / "state" / "plan-blitz").exists()


def test_cycle_is_refused(tmp_path, roadmap):
    (roadmap / "clusters.md").write_text(
        "## C1 — a\n**loe:** M\n**blocked_by:** C2.\n\n## C2 — b\n**loe:** M\n**blocked_by:** C1.\n", encoding="utf-8"
    )
    with pytest.raises(bs.BlitzStageRefused, match="cycle"):
        bs.stage_roadmap(tmp_path, str(roadmap))


def test_op_wraps_the_library(tmp_path, roadmap):
    from coordinator_core.ops.roadmap_blitz_stage import _handler

    reply = _handler({"roadmap": str(roadmap)}, repo_root=tmp_path / ".git")
    assert len(reply["stubs"]) == 4
    with pytest.raises(ValueError, match="roadmap is required"):
        _handler({}, repo_root=tmp_path / ".git")


def test_frozen_report_is_byte_identical_to_the_bare_plan_gate_op(tmp_path, roadmap):
    from coordinator_core.invoke.__main__ import _dispatch_argv

    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    frozen = (tmp_path / reply["gate_report_path"]).read_text(encoding="utf-8")
    out, _, code = _dispatch_argv(
        ["roadmap.plan_gate", json.dumps({"roadmap_id": "rm-test"}), "--bare", "--repo", str(tmp_path)],
        str(tmp_path),
        allow_warm=False,
    )
    assert code == 0 and frozen == out
    waves = json.loads(frozen)["waves"]
    assert waves and all(isinstance(w, list) and all(isinstance(i, str) for i in w) for w in waves)


def test_recycle_check_reads_the_frozen_waves(tmp_path, roadmap):
    import importlib.util

    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    spec = importlib.util.spec_from_file_location(
        "recycle_check_for_blitz_stage", Path(bs.__file__).resolve().parents[2] / "coordinator" / "bin" / "recycle-check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ids, _ = mod._wave_ids(tmp_path / reply["gate_report_path"], 0)
    assert len(ids) == 2


def test_stubs_then_report_commit_with_explicit_paths(tmp_path, roadmap, commit_calls):
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert len(commit_calls) == 2
    stub_and_sizing = [p for s in reply["stubs"] for p in (s["path"], s["sizing_object"]) if p]
    assert commit_calls[0]["paths"] == stub_and_sizing
    assert commit_calls[1]["paths"] == [reply["gate_report_path"]]
    assert [c["sha"] for c in reply["commits"]] == ["sha1", "sha2"]
    assert reply["commit_error"] is None


def test_no_commit_leaves_everything_uncommitted(tmp_path, roadmap, commit_calls):
    reply = bs.stage_roadmap(tmp_path, str(roadmap), commit=False)
    assert commit_calls == [] and reply["commits"] == []


def test_rerun_commits_nothing(tmp_path, roadmap, commit_calls):
    bs.stage_roadmap(tmp_path, str(roadmap))
    commit_calls.clear()
    bs.stage_roadmap(tmp_path, str(roadmap))
    assert commit_calls == []


def test_a_failed_commit_is_reported_and_defers_the_report(tmp_path, roadmap, monkeypatch):
    from coordinator_core.roadmap import plan_gate

    monkeypatch.setattr(bs, "_commit_paths", lambda *a: {"committed": False, "error": "peer hold"})
    monkeypatch.setattr(plan_gate, "_tracked_paths", lambda root: (frozenset(), None))
    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    assert reply["commit_error"] == "peer hold"
    assert reply["gate_report_path"] is None


def test_each_sized_baton_gets_its_own_linked_sizing_object(tmp_path, roadmap):
    import yaml

    reply = bs.stage_roadmap(tmp_path, str(roadmap))
    sized = [s for s in reply["stubs"] if s["loe"]]
    paths = [s["sizing_object"] for s in sized]
    assert len(sized) >= 2 and len(set(paths)) == len(paths)
    for stub in sized:
        record = yaml.safe_load((tmp_path / stub["sizing_object"]).read_text(encoding="utf-8"))
        assert record["estimate"]["tshirt"] == stub["loe"]
        assert record["route"] == reply["folds"]["routes"][stub["stub_id"]]
        assert record["status"] == "routed"
        assert record["premise"]["provenance"] == "read"
        assert "clusters.md § " + ", ".join(stub["covers"]) in record["premise"]["evidence"]
        assert record["deliverable_id"] == stub["deliverable_id"]
        assert f'sizing_object: "{stub["sizing_object"]}"' in _fm(tmp_path / stub["path"])
    assert len({yaml.safe_load((tmp_path / p).read_text(encoding="utf-8"))["deliverable_id"] for p in paths}) == len(paths)
