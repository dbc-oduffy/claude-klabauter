"""sizings / workflow_runs sections: emit only when the vendored pin carries the entity; project
the pinned fields; stay under the 500ms process-time bar on the live corpus."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.contract.cockpit_schema.entities.sizing_summary import SizingSummary
from coordinator_core.contract.cockpit_schema.entities.workflow_run import WorkflowRun
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import sizings, workflow_runs

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BAR_SECS = 0.5


def _ctx(root: Path) -> EmitContext:
    return EmitContext(
        repo_root=root,
        coordinator_root=root,
        central_state_root=root / "state",
        git_branch="main",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at="2026-10-24T12:00:00Z",
        hostname="test-host",
        repo_name="fixture-owner/fixture-repo",
    )


@pytest.fixture
def pinned(monkeypatch):
    """Pretend the vendored bundle already carries both entities."""
    for mod in (sizings, workflow_runs):
        monkeypatch.setattr(mod, "_entity_schema_path", lambda name: Path(__file__))


@pytest.fixture
def unpinned(monkeypatch):
    for mod in (sizings, workflow_runs):
        monkeypatch.setattr(mod, "_entity_schema_path", lambda name: Path("absent.schema.json"))


def _sizing(root: Path, name: str, body: str) -> None:
    d = root / "state" / "sizings"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


_BASE = """schema: sizing-object
intent: do a thing
estimate:
  tshirt: M
route: plan
xl_exit: null
status: {status}
{extra}"""


def _run(root: Path, run_id: str, stage: dict | None, sizing_rel: str | None = "state/sizings/x.yaml") -> None:
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": sizing_rel}), encoding="utf-8")
    if stage is not None:
        (d / "run-stage.json").write_text(json.dumps(stage), encoding="utf-8")


def _stage(stage: str, last: str = "2026-10-24T11:00:00Z", **kw) -> dict:
    return {
        "session_id": "s1",
        "started_at": "2026-10-24T10:00:00Z",
        "stage": stage,
        "last_activity_at": last,
        "steps_planned": None,
        **kw,
    }


def test_sizings_unpinned_emits_nothing(tmp_path, unpinned):
    _sizing(tmp_path, "2026-10-01-a.yaml", _BASE.format(status="routed", extra=""))
    assert sizings.collect(_ctx(tmp_path)) == ([], [])


def test_workflow_runs_unpinned_emits_nothing(tmp_path, unpinned):
    _run(tmp_path, "ask-1", _stage("staged"))
    assert workflow_runs.collect(_ctx(tmp_path)) == ([], [])


def test_sizings_pinned_projects_and_resolves(tmp_path, pinned):
    _sizing(tmp_path, "2026-10-01-planned.yaml", _BASE.format(status="routed", extra="plan: docs/plans/p.md\nexit_criterion:\n  accepted:\n    rule: r\n"))
    _sizing(tmp_path, "2026-10-02-closed.yaml", _BASE.format(status="closed", extra=""))
    _sizing(tmp_path, "2026-10-03-open.yaml", _BASE.format(status="routed", extra=""))
    _sizing(tmp_path, "2026-10-04-bad.yaml", "schema: sizing-object\nstatus: routed\n")
    records, malformed = sizings.collect(_ctx(tmp_path))
    by = {r["path"].rsplit("/", 1)[-1]: r for r in records}
    assert by["2026-10-01-planned.yaml"]["resolution"] == "planned"
    assert by["2026-10-01-planned.yaml"]["linked_plan"] == "docs/plans/p.md"
    assert by["2026-10-01-planned.yaml"]["exit_criterion_accepted"] is True
    assert by["2026-10-01-planned.yaml"]["created"] == "2026-10-01"
    assert by["2026-10-01-planned.yaml"]["tshirt"] == "M"
    assert by["2026-10-02-closed.yaml"]["resolution"] == "closed"
    assert by["2026-10-02-closed.yaml"]["exit_criterion_accepted"] is False
    assert by["2026-10-03-open.yaml"]["resolution"] == "unresolved"
    assert by["2026-10-03-open.yaml"]["linked_plan"] is None
    for r in records:
        SizingSummary.model_validate(r)
    assert [m["path"].rsplit("/", 1)[-1] for m in malformed] == ["2026-10-04-bad.yaml"]


def test_workflow_runs_pinned_selects_window_and_status(tmp_path, pinned):
    _run(tmp_path, "r-live", _stage("staged", last="2026-01-01T00:00:00Z", steps_planned=4))
    _run(tmp_path, "r-halted-recent", _stage("halted", last="2026-10-20T00:00:00Z"))
    _run(tmp_path, "r-landed-old", _stage("landed", last="2026-10-01T00:00:00Z"))
    _run(tmp_path, "r-landed-recent", _stage("landed", last="2026-10-23T00:00:00Z"), sizing_rel=None)
    _run(tmp_path, "r-legacy", None)
    _run(tmp_path, "r-bad", _stage("bogus"))
    records, malformed = workflow_runs.collect(_ctx(tmp_path))
    by = {r["run_id"]: r for r in records}
    assert sorted(by) == ["r-halted-recent", "r-landed-recent", "r-live"]
    assert by["r-live"]["status"] == "live" and by["r-live"]["steps_planned"] == 4
    assert by["r-halted-recent"]["status"] == "halted"
    assert by["r-landed-recent"]["status"] == "closed"
    assert by["r-landed-recent"]["sizing_rel"] is None
    assert by["r-live"]["sizing_rel"] == "state/sizings/x.yaml"
    for r in records:
        WorkflowRun.model_validate(r)
    assert [m["path"] for m in malformed] == ["state/warp/r-bad/run-stage.json"]


def test_workflow_runs_includes_in_flight_run_recorded_only_under_scratch(tmp_path, pinned):
    d = tmp_path / "scratch" / "warp" / "r-scratch"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": None}), encoding="utf-8")
    (d / "run-stage.json").write_text(json.dumps(_stage("minted")), encoding="utf-8")
    records, _ = workflow_runs.collect(_ctx(tmp_path))
    assert [r["run_id"] for r in records] == ["r-scratch"]
    assert records[0]["status"] == "live"


def test_workflow_runs_memo_holds_a_bounded_number_of_roots(tmp_path, pinned):
    workflow_runs._MEMO.clear()
    for i in range(workflow_runs._MEMO_MAX_ROOTS + 4):
        root = tmp_path / f"root{i}"
        root.mkdir()
        workflow_runs.collect(_ctx(root))
    assert len(workflow_runs._MEMO) == workflow_runs._MEMO_MAX_ROOTS
    assert str(tmp_path / f"root{workflow_runs._MEMO_MAX_ROOTS + 3}") in workflow_runs._MEMO
    assert str(tmp_path / "root0") not in workflow_runs._MEMO


def test_workflow_runs_rereads_only_runs_whose_files_moved(tmp_path, pinned, monkeypatch):
    for i in range(30):
        _run(tmp_path, f"r-old-{i:02d}", _stage("landed", last="2026-01-01T00:00:00Z"))
    _run(tmp_path, "r-live", _stage("staged", steps_planned=2))
    ctx = _ctx(tmp_path)
    reads: list[str] = []
    real = workflow_runs._read_json
    monkeypatch.setattr(workflow_runs, "_read_json", lambda p: (reads.append(p), real(p))[1])

    first, _ = workflow_runs.collect(ctx)
    assert [r["run_id"] for r in first] == ["r-live"]
    assert len([p for p in reads if p.endswith("run-stage.json")]) == 31

    reads.clear()
    again, _ = workflow_runs.collect(ctx)
    assert again == first
    assert reads == []  # unchanged corpus: no JSON read, old terminal runs included

    stage_file = tmp_path / "state" / "warp" / "r-live" / "run-stage.json"
    stage_file.write_text(json.dumps(_stage("landed", last="2026-10-24T11:30:00Z", steps_planned=22)), encoding="utf-8")
    reads.clear()
    moved, _ = workflow_runs.collect(ctx)
    assert [r["steps_planned"] for r in moved] == [22] and moved[0]["status"] == "closed"
    assert [Path(p).parent.name for p in reads] == ["r-live", "r-live"]  # its stage and run.json, nothing else

    (stage_file.parent / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/y.yaml"}), encoding="utf-8")
    refreshed, _ = workflow_runs.collect(ctx)
    assert refreshed[0]["sizing_rel"] == "state/sizings/y.yaml"


def test_workflow_runs_cold_engine_adopts_the_snapshot_without_reading_run_files(tmp_path, pinned, monkeypatch):
    (tmp_path / ".coordinator-local").mkdir()
    for i in range(20):
        _run(tmp_path, f"r-old-{i:02d}", _stage("landed", last="2026-01-01T00:00:00Z"))
    (tmp_path / "state" / "warp" / "r-empty").mkdir()
    _run(tmp_path, "r-live", _stage("staged", steps_planned=2))
    ctx = _ctx(tmp_path)
    first, _ = workflow_runs.collect(ctx)
    assert (tmp_path / ".coordinator-local" / "workflow-runs-index.json").is_file()

    workflow_runs._MEMO.clear()  # engine restart
    reads: list[str] = []
    real = workflow_runs._read_json
    monkeypatch.setattr(workflow_runs, "_read_json", lambda p: (reads.append(p), real(p))[1])
    cold, _ = workflow_runs.collect(ctx)
    assert cold == first == [r for r in first if r["run_id"] == "r-live"]
    assert [p for p in reads if "warp" in p] == []  # no run file read; only the snapshot itself

    # A run dir whose mtime moved is re-read; the rest stay snapshot-served.
    workflow_runs._MEMO.clear()
    reads.clear()
    atomic_write_bytes(
        tmp_path / "state" / "warp" / "r-live" / "run-stage.json",
        json.dumps(_stage("landed", last="2026-10-24T11:30:00Z", steps_planned=9)).encode("utf-8"),
    )
    moved, _ =workflow_runs.collect(ctx)
    assert [(r["steps_planned"], r["status"]) for r in moved] == [(9, "closed")]
    assert {Path(p).parent.name for p in reads if "warp" in p} == {"r-live"}


def test_workflow_runs_ignores_a_snapshot_from_another_schema(tmp_path, pinned):
    (tmp_path / ".coordinator-local").mkdir()
    _run(tmp_path, "r-live", _stage("staged", steps_planned=2))
    ctx = _ctx(tmp_path)
    workflow_runs.collect(ctx)
    snap = tmp_path / ".coordinator-local" / "workflow-runs-index.json"
    data = json.loads(snap.read_text(encoding="utf-8"))
    data["f"] = [-1, 0]
    data["e"] = {k: [v[0], 1, "staged", v[3], {"run_id": "stale"}, None] for k, v in data["e"].items()}
    snap.write_text(json.dumps(data), encoding="utf-8")
    workflow_runs._MEMO.clear()
    again, _ = workflow_runs.collect(ctx)
    assert [r["run_id"] for r in again] == ["r-live"]


def test_live_corpus_sections_stay_under_the_500ms_bar(pinned):
    if len(list((_REPO_ROOT / "state" / "sizings").glob("*.yaml"))) < 100:
        pytest.skip("live sizing corpus absent")
    ctx = _ctx(_REPO_ROOT)
    sizings.collect(ctx)  # populate the parse memo and per-worktree record cache
    workflow_runs.collect(ctx)  # populate the per-run memo the warm engine carries
    t = time.process_time()
    records, _ = sizings.collect(ctx)
    sizings_secs = time.process_time() - t
    t = time.process_time()
    workflow_runs.collect(ctx)
    runs_secs = time.process_time() - t
    print(f"sizings: {len(records)} rows {sizings_secs * 1000:.0f}ms process; workflow_runs {runs_secs * 1000:.0f}ms")
    assert records
    assert sizings_secs < _BAR_SECS
    assert runs_secs < _BAR_SECS
