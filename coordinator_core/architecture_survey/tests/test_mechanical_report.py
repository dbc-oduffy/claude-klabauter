"""mechanical_report: the drift gate lists only pages with a dead path, file-index delta or matrix cell change."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.architecture_survey import mechanical_report as mr
from coordinator_core.architecture_survey import render
import pytest

REPO = Path(__file__).resolve().parents[3]
ATLAS = "docs/architecture"


def _need(payload, covered: bool = True, reason=None):
    return {"covered": covered, "door": None, "calls": 0, "reason": reason, "payload": payload}


def _structure(rows, edges, *, rows_covered: bool = True, edges_covered: bool = True):
    return {
        "index": {"commit": "unstated", "commits_behind": 0, "state": "behind", "dropped_paths": 0, "unindexed": []},
        "systems": [],
        "needs": {
            "file-index": _need([{"path": p, "system": s} for p, s in rows], rows_covered, None if rows_covered else "no"),
            "edges": _need({"edges": [{"from_system": a, "to_system": b, "weight": w} for a, b, w in edges]},
                           edges_covered, None if edges_covered else "no"),
            "system-map": _need({"systems": []}, False, "not fetched"),
        },
    }


def _atlas(tmp_path, rows, edges, pages=("alpha", "beta", "gamma")):
    """A repo whose atlas is exactly what render writes for (rows, edges), plus one page per system."""
    structure = _structure(rows, edges)
    atlas = tmp_path / ATLAS
    (atlas / "systems").mkdir(parents=True)
    for name in pages:
        (atlas / "systems" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    render.render_structure(tmp_path, {**structure, "systems": []}, today="2026-11-13")
    return structure


ROWS = [("a/x.py", "alpha"), ("b/y.py", "beta"), ("c/z.py", "gamma")]
EDGES = [("alpha", "beta", 2), ("beta", "gamma", 1)]


def test_unchanged_atlas_lists_no_pages(tmp_path: Path) -> None:
    structure = _atlas(tmp_path, ROWS, EDGES)
    report = mr.build_report(tmp_path, structure, [])
    assert report["pages_changed"] == []
    assert report["legs"]["file-index"]["status"] == "ran"
    assert report["legs"]["connectivity-matrix"]["status"] == "ran"
    assert report["schema"] == "atlas-mechanical-layer" and report["schema_version"] == 1


def test_file_added_flags_only_its_system_page(tmp_path: Path) -> None:
    _atlas(tmp_path, ROWS, EDGES)
    fresh = _structure(ROWS + [("b/new.py", "beta")], EDGES)
    report = mr.build_report(tmp_path, fresh, [])
    assert report["pages_changed"] == [f"{ATLAS}/systems/beta.md"]
    assert report["file_index"]["added"] == [["b/new.py", "beta"]]
    assert report["page_briefs"][f"{ATLAS}/systems/beta.md"]["brief"] == mr.BRIEF_CLAUSE


def test_file_removed_flags_its_system_page(tmp_path: Path) -> None:
    _atlas(tmp_path, ROWS, EDGES)
    report = mr.build_report(tmp_path, _structure(ROWS[:2], EDGES), [])
    assert report["pages_changed"] == [f"{ATLAS}/systems/gamma.md"]
    assert report["file_index"]["removed"] == [["c/z.py", "gamma"]]


def test_matrix_cell_change_flags_both_endpoint_pages_not_the_generated_matrix(tmp_path: Path) -> None:
    _atlas(tmp_path, ROWS, EDGES)
    report = mr.build_report(tmp_path, _structure(ROWS, [("alpha", "beta", 5), ("beta", "gamma", 1)]), [])
    assert report["pages_changed"] == [f"{ATLAS}/systems/alpha.md", f"{ATLAS}/systems/beta.md"]
    assert report["connectivity_matrix"]["changed_cells"] == [{"from": "alpha", "to": "beta", "before": 2, "after": 5}]


def test_dead_path_flags_its_page_but_historical_and_generated_pages_do_not(tmp_path: Path) -> None:
    structure = _atlas(tmp_path, ROWS, EDGES)
    (tmp_path / ATLAS / "audit-records").mkdir()
    (tmp_path / ATLAS / "audit-records" / "old.md").write_text("old\n", encoding="utf-8")
    rows = [
        {"cls": "unresolved-file", "page": f"{ATLAS}/systems/alpha.md", "line": 3, "cited": "gone.py", "target": None},
        {"cls": "unresolved-file", "page": f"{ATLAS}/audit-records/old.md", "line": 1, "cited": "gone.py", "target": None},
        {"cls": "unresolved-file", "page": f"{ATLAS}/connectivity-matrix.md", "line": 9, "cited": "gone.py", "target": None},
        {"cls": "unresolved-file", "page": f"{ATLAS}/file-index.md", "line": 9, "cited": "gone.py", "target": None},
    ]
    report = mr.build_report(tmp_path, structure, rows)
    assert report["pages_changed"] == [f"{ATLAS}/systems/alpha.md"]
    assert len(report["dead_paths"]) == 4


def test_no_baseline_means_no_delta_not_everything_added(tmp_path: Path) -> None:
    atlas = tmp_path / ATLAS
    (atlas / "systems").mkdir(parents=True)
    (atlas / "systems" / "alpha.md").write_text("# a\n", encoding="utf-8")
    (atlas / "file-index.md").write_text("hand-written, no generated block\n", encoding="utf-8")
    (atlas / "connectivity-matrix.md").write_text("| a | b |\n|---|---|\n| x | 1 |\n", encoding="utf-8")
    report = mr.build_report(tmp_path, _structure(ROWS, EDGES), [])
    assert report["pages_changed"] == []
    assert "no committed baseline" in report["legs"]["file-index"]["detail"]
    assert "no committed baseline" in report["legs"]["connectivity-matrix"]["detail"]


def test_uncovered_needs_are_named_not_ran_and_drift_without_a_page_is_reported(tmp_path: Path) -> None:
    _atlas(tmp_path, ROWS, EDGES)
    report = mr.build_report(tmp_path, _structure(ROWS, EDGES, rows_covered=False, edges_covered=False), [])
    assert report["legs"]["file-index"]["status"] == "uncovered"
    assert report["legs"]["connectivity-matrix"]["status"] == "uncovered"
    fresh = _structure(ROWS + [("q/n.py", "nopage")], EDGES)
    report = mr.build_report(tmp_path, fresh, [])
    assert report["pages_changed"] == [] and report["systems_without_page"] == [f"{ATLAS}/systems/nopage.md"]


def test_dead_path_rows_keep_only_dead_kinds_and_extract_the_cited_path() -> None:
    f = SimpleNamespace
    check = f(findings=[
        f(kind="unresolved-file", page="p.md", line=4, detail="cited path does not resolve: `x/y.py`"),
        f(kind="missing-file-index-row", page="file-index.md", line=None, detail="3 missing"),
    ])
    assert mr.dead_path_rows(check) == [
        {"cls": "unresolved-file", "page": "p.md", "line": 4, "cited": "x/y.py", "target": None}]


def test_rag_unavailable_fallback_names_the_real_reason_and_default_opts_edges_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_fetch(root, run_dir, pages, *, costs):
        calls.append(costs["needs"]["edges"].get("fetch"))
        if costs["needs"]["edges"].get("fetch") is True:
            raise mr.structure_mod.StructureUnavailable("rag answered for another project")
        s = _structure([], [], edges_covered=False)
        s["needs"]["edges"]["reason"] = "door-over-bar: stale"
        return s

    monkeypatch.setattr(mr.structure_mod, "fetch", fake_fetch)
    structure = mr._fetch_structure(tmp_path, tmp_path / "run")
    assert calls == [True, False]
    assert "rag answered for another project" in structure["needs"]["edges"]["reason"]
    assert "door-over-bar" not in structure["needs"]["edges"]["reason"]


def test_fetch_structure_keeps_covered_edges_when_rag_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _structure(ROWS, EDGES)
    monkeypatch.setattr(mr.structure_mod, "fetch", lambda *a, **k: good)
    assert mr._fetch_structure(tmp_path, tmp_path / "run") is good


def test_produce_writes_report_through_real_checker(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    report = mr.produce(REPO, out, structure=_structure([], [], rows_covered=True, edges_covered=True))
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(report))
    assert set(on_disk) >= {"legs", "dead_paths", "file_index", "connectivity_matrix", "pages_changed"}
    assert on_disk["legs"]["dead-paths"]["status"] == "ran"
    assert all(b["brief"] == mr.BRIEF_CLAUSE for b in on_disk["page_briefs"].values())


def _row(page, line, cited):
    return {"cls": "unresolved-file", "page": page, "line": line, "cited": cited, "target": None}


def test_drifted_page_brief_carries_its_own_deltas_and_a_capped_preview_and_tokens_are_measured(tmp_path: Path) -> None:
    _atlas(tmp_path, ROWS, EDGES)
    beta = tmp_path / ATLAS / "systems" / "beta.md"
    beta.write_text("# beta\n" + "x" * (mr.PREVIEW_CAP_BYTES * 3), encoding="utf-8")
    fresh = _structure(ROWS + [("b/new.py", "beta"), ("a/other.py", "alpha")], EDGES)
    rel = f"{ATLAS}/systems/beta.md"
    dead = _row(rel, 1, "gone.py")
    report = mr.build_report(tmp_path, fresh, [dead])
    brief = report["page_briefs"][rel]
    assert brief["dead_paths"] == [dead]
    assert brief["file_index"] == {"added": [["b/new.py", "beta"]], "removed": []}
    assert brief["preview"].startswith("# beta") and len(brief["preview"].encode()) == mr.PREVIEW_CAP_BYTES
    alpha = report["page_briefs"][f"{ATLAS}/systems/alpha.md"]
    assert alpha["file_index"]["added"] == [["a/other.py", "alpha"]] and alpha["dead_paths"] == []
    assert f"{ATLAS}/systems/gamma.md" not in report["page_briefs"]
    tokens = report["tokens"]
    assert tokens["pages"] == 2 and tokens["narrative_payload"] == mr.estimate_tokens(tokens["bytes"]) > 0
    assert tokens["bytes"] == len(json.dumps(report["page_briefs"], sort_keys=True, separators=(",", ":")).encode())


def test_unchanged_atlas_has_no_briefs_and_zero_tokens(tmp_path: Path) -> None:
    report = mr.build_report(tmp_path, _atlas(tmp_path, ROWS, EDGES), [])
    assert report["page_briefs"] == {}
    assert report["tokens"]["narrative_payload"] == mr.estimate_tokens(2)


def test_other_repo_cite_is_not_a_dead_path_but_a_local_dead_path_still_is(tmp_path: Path) -> None:
    structure = _atlas(tmp_path, ROWS, EDGES)
    page = tmp_path / ATLAS / "systems" / "alpha.md"
    page.write_text("# alpha\nthe DoE-side `setup/x.py` is not vendored\ntheir CLI `bin/gone.py` here\n", encoding="utf-8")
    rel = f"{ATLAS}/systems/alpha.md"
    foreign, local = _row(rel, 2, "setup/x.py"), _row(rel, 3, "bin/gone.py")
    report = mr.build_report(tmp_path, structure, [foreign, local])
    assert [r["cited"] for r in report["dead_paths"]] == ["bin/gone.py"]
    assert report["pages_changed"] == [rel]
