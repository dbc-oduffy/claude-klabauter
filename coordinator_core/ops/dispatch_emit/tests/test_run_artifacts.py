"""run_artifacts: per-class candidate set read from a run's own record graph, zero git."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts as ra

RUN = "ask-20261016T120000-bbbbbb"
OLD = "ask-20261016T100000-aaaaaa"
SIZING = "state/sizings/2026-10-16-x.yaml"
PLAN = "docs/plans/2026-10-16-x.md"
SLICE = "state/sizings/2026-10-16-x-slice-2.yaml"


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    from coordinator_core.git import run as git_run

    def boom(*a, **k):
        raise AssertionError("run_artifacts must not spawn git")

    monkeypatch.setattr(git_run, "run_git", boom)


def _w(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def graph(tmp_path):
    root = tmp_path
    rec = f"state/warp/{RUN}"
    _w(root, f"{rec}/ask.md")
    _w(root, f"{rec}/run.json", json.dumps({"sizing_rel": SIZING.replace("/", "\\")}))
    _w(root, f"{rec}/parent.json", json.dumps({"runs": [{"run_id": "s2", "slice_sizing": SLICE}]}))
    _w(root, SIZING, f"baton: state/handoffs/h.md  # c\nplan: '{PLAN}'\n")
    _w(root, "state/handoffs/h.md")
    _w(root, PLAN)
    _w(root, "docs/plans/2026-10-16-x.evidence.yaml")
    _w(root, "docs/plans/2026-10-16-x.seam.yaml")
    _w(root, f"docs/delivery/{RUN}.md")
    _w(root, SLICE)
    _w(root, "docs/reference/r.md")
    return root


def test_every_class_found(graph):
    a = ra.candidate_paths(graph, RUN, doc_paths=["docs/reference/r.md"])
    expect = {
        "record-dir": f"state/warp/{RUN}/ask.md",
        "sizing": SIZING,
        "baton": "state/handoffs/h.md",
        "plan": PLAN,
        "evidence-sidecar": "docs/plans/2026-10-16-x.evidence.yaml",
        "seam-sidecar": "docs/plans/2026-10-16-x.seam.yaml",
        "delivery-note": f"docs/delivery/{RUN}.md",
        "slice-sizing": SLICE,
        "docs-stage": "docs/reference/r.md",
    }
    for cls, path in expect.items():
        assert path in a.by_class[cls], cls
        assert a.class_of(path) == cls
    assert a.paths() == tuple(sorted(set(a.paths())))
    assert a.record_dirs == (f"state/warp/{RUN}",)


def test_missing_run_json_yields_empty(tmp_path):
    a = ra.candidate_paths(tmp_path, "ask-20261016T120000-cccccc")
    assert a.paths() == ()


def test_explicit_args_win(graph):
    _w(graph, "state/sizings/other.yaml", "plan: docs/plans/z.md\n")
    _w(graph, "docs/plans/z.md")
    a = ra.candidate_paths(graph, RUN, sizing_rel="state/sizings/other.yaml", plan_rel="docs/plans/z.md")
    assert a.by_class["sizing"] == ("state/sizings/other.yaml",)
    assert a.by_class["plan"] == ("docs/plans/z.md",)


def test_doc_paths_filtering(graph):
    for rel in ("scratch/a.md", "state/warp/x/y.md"):
        _w(graph, rel)
    drive_abs = "C" + ":/abs.md"  # abs-path-ok: rejection fixture, built so no drive literal is in source
    a = ra.candidate_paths(
        graph, RUN, doc_paths=["scratch/a.md", "../out.md", "state/warp/x/y.md", drive_abs, "/abs.md", "docs/reference/r.md"]
    )
    assert a.by_class["docs-stage"] == ("docs/reference/r.md",)


def test_superseded_detection(graph):
    for name, sizing in ((OLD, SIZING), ("ask-20261016T110000-dddddd", "state/sizings/else.yaml"),
                         ("ask-20261016T130000-eeeeee", SIZING), ("ask-20261016T090000-ffffff", SIZING)):
        _w(graph, f"state/warp/{name}/run.json", json.dumps({"sizing_rel": sizing}))
    changed = [f"state/warp/{n}/run.json" for n in (OLD, "ask-20261016T110000-dddddd",
               "ask-20261016T130000-eeeeee", "ask-20261016T090000-ffffff")] + [f"state/warp/{RUN}/ask.md"]
    got = ra.superseded_runs(graph, RUN, SIZING, changed, live_run_ids=["ask-20261016T090000-ffffff"])
    assert got == (OLD,)
    assert ra.superseded_runs(graph, RUN, None, changed) == ()
    a = ra.candidate_paths(graph, RUN).with_superseded(got, "state/warp")
    assert f"state/warp/{OLD}/run.json" in a.by_class["superseded-record"]
    assert f"state/warp/{OLD}" in a.record_dirs


def test_landable_withholds_peer_held_and_unclaimed(graph, monkeypatch):
    from coordinator_core.session import scope

    a = ra.candidate_paths(graph, RUN, doc_paths=["docs/reference/r.md"])
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {PLAN: ["peer"]})
    monkeypatch.setattr(scope, "own_path_claims_in_process", lambda sid, cwd=None: [])
    changed = list(a.paths())
    out = ra.landable(graph, a, changed, ["state/handoffs/h.md"], session_id="s1")
    assert out.withheld[PLAN] == "peer-held"
    assert out.withheld["docs/reference/r.md"] == "not-claimed"
    assert "state/handoffs/h.md" not in out.present
    assert SIZING in out.present

    monkeypatch.setattr(scope, "own_path_claims_in_process", lambda sid, cwd=None: ["docs/reference/r.md"])
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: None)
    out = ra.landable(graph, a, changed, [], session_id="s1")
    assert out.contest_unknown and not out.withheld


def test_landable_splits_deletions(graph, monkeypatch):
    from coordinator_core.git import commit

    a = ra.candidate_paths(graph, RUN)
    (graph / SLICE).unlink()
    monkeypatch.setattr(commit, "partition_declared_deletions", lambda root, gone: (list(gone), []))
    out = ra.landable(graph, a, a.paths(), [], session_id=None)
    assert out.deleted == (SLICE,) and SLICE not in out.present


@pytest.fixture
def mapped(graph):
    _w(graph, SIZING, f"baton: state/handoffs/h.md\nplan: '{PLAN}'\n")
    _w(graph, PLAN, f"---\nsizing_object: {SIZING}\n---\n")
    _w(graph, "state/handoffs/2026-10-16-x.md", f"sizing_object: {SIZING}\n")
    return graph


def test_runs_of_artifacts_maps_each_class(mapped):
    paths = [PLAN, "docs/plans/2026-10-16-x.evidence.yaml", "docs/plans/2026-10-16-x.seam.yaml",
             "state/handoffs/2026-10-16-x.md", f"docs/delivery/{RUN}.md", SLICE, SIZING]
    got = ra.runs_of_artifacts(mapped, [p.replace("/", "\\") for p in paths])
    assert got == {p: RUN for p in paths}


def test_runs_of_artifacts_handoff_falls_back_to_plan(mapped):
    _w(mapped, "state/handoffs/2026-10-16-x.md", "no frontmatter\n")
    assert ra.runs_of_artifacts(mapped, ["state/handoffs/2026-10-16-x.md"]) == {
        "state/handoffs/2026-10-16-x.md": RUN
    }


def test_runs_of_artifacts_latest_stamp_wins(mapped):
    newer = "ask-20261016T130000-eeeeee"
    _w(mapped, f"state/warp/{newer}/run.json", json.dumps({"sizing_rel": SIZING}))
    _w(mapped, "state/warp/ask-20261016T090000-ffffff/run.json", json.dumps({"sizing_rel": SIZING}))
    assert ra.runs_of_artifacts(mapped, [PLAN]) == {PLAN: newer}


def test_runs_of_artifacts_unmapped_paths_absent(mapped):
    _w(mapped, "docs/plans/nosizing.md", "no key\n")
    _w(mapped, "docs/delivery/ask-20261016T150000-999999.md")
    _w(mapped, "state/sizings/unbound.yaml")
    _w(mapped, "docs/plans/orphan.md", "sizing_object: state/sizings/unbound.yaml\n")
    got = ra.runs_of_artifacts(
        mapped,
        ["docs/plans/nosizing.md", "docs/delivery/ask-20261016T150000-999999.md", "state/sizings/unbound.yaml",
         "docs/plans/orphan.md", f"state/warp/{RUN}/run.json", "src/a.py"],
    )
    assert got == {}


def test_runs_of_artifacts_no_reads_for_unrelated(mapped, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("read")

    monkeypatch.setattr(ra, "_read_text", boom)
    monkeypatch.setattr(ra, "_read_json", boom)
    assert ra.runs_of_artifacts(mapped, ["src/a.py", f"state/warp/{RUN}/ask.md"]) == {}


def test_runs_of_artifacts_round_trip_filter(mapped, monkeypatch):
    monkeypatch.setattr(
        ra, "candidate_paths",
        lambda root, rid, **k: ra.RunArtifacts(rid, {"plan": (PLAN,)}, (), root),
    )
    assert ra.runs_of_artifacts(mapped, [PLAN, SIZING]) == {PLAN: RUN}


def test_residue_empty_when_all_landed(graph):
    a = ra.candidate_paths(graph, RUN)
    assert ra.residue(a, a.paths(), a.paths(), {}) == {}
    left = ra.residue(a, [SIZING, "unrelated.py"], [], {})
    assert left == {SIZING: "sizing"}
    assert ra.residue(a, [SIZING], [], {SIZING: "peer-held"}) == {}
