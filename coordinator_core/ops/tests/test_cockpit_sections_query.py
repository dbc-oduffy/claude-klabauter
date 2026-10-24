"""sizings.summary / workflow_runs.list: registered, COMPUTE_ONLY, reached through IPC dispatch,
rows validate against the contract entities, and the live corpus stays under the 500ms bar."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 -- populates _REGISTRY
from coordinator_core.authz.classification import OpClass, OP_CLASSIFICATION
from coordinator_core.contract.cockpit_schema.entities.sizing_summary import SizingSummary
from coordinator_core.contract.cockpit_schema.entities.workflow_run import WorkflowRun
from coordinator_core.ipc import _REGISTRY, _dispatch_message_impl
from coordinator_core.ops import cockpit_sections_query
from coordinator_core.ops.emit.sections import sizings, workflow_runs
from coordinator_core.win_portability import no_console_creationflags

# IPC routing resolves the git common dir, so the dispatch tests run a real `git init`.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPS = ("sizings.summary", "workflow_runs.list")
_BAR_SECS = 0.5

_SIZING = """schema: sizing-object
intent: do a thing
estimate:
  tshirt: M
route: plan
xl_exit: null
status: {status}
"""


@pytest.fixture
def pinned(monkeypatch):
    for mod in (sizings, workflow_runs):
        monkeypatch.setattr(mod, "_entity_schema_path", lambda name: Path(__file__))


def _dispatch(op: str, root: Path) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": op, "params": {}, "_origin_worktree": str(root)}
    return asyncio.run(_dispatch_message_impl(msg))


def _seed(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    sd = root / "state" / "sizings"
    sd.mkdir(parents=True)
    (sd / "2026-10-01-a.yaml").write_text(_SIZING.format(status="routed"), encoding="utf-8")
    (sd / "2026-10-02-b.yaml").write_text(_SIZING.format(status="closed"), encoding="utf-8")
    (sd / "2026-10-03-bad.yaml").write_text("schema: sizing-object\nstatus: routed\n", encoding="utf-8")
    for run_id, stage in (("r-live", "staged"), ("r-bad", "bogus")):
        d = root / "state" / "warp" / run_id
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/x.yaml"}), encoding="utf-8")
        (d / "run-stage.json").write_text(
            json.dumps(
                {
                    "session_id": "s1",
                    "started_at": "2026-10-24T10:00:00Z",
                    "stage": stage,
                    "last_activity_at": "2026-10-24T11:00:00Z",
                    "steps_planned": 3,
                }
            ),
            encoding="utf-8",
        )


def test_ops_registered_and_compute_only():
    for op in _OPS:
        assert op in _REGISTRY
        assert OP_CLASSIFICATION[op] is OpClass.COMPUTE_ONLY


def test_sizings_summary_through_ipc_dispatch(tmp_path, pinned):
    _seed(tmp_path)
    result = _dispatch("sizings.summary", tmp_path)["result"]
    by = {r["path"].rsplit("/", 1)[-1]: r for r in result["records"]}
    assert sorted(by) == ["2026-10-01-a.yaml", "2026-10-02-b.yaml"]
    assert by["2026-10-02-b.yaml"]["resolution"] == "closed"
    for r in result["records"]:
        SizingSummary.model_validate(r)
    assert [m["path"].rsplit("/", 1)[-1] for m in result["malformed"]] == ["2026-10-03-bad.yaml"]
    assert set(result["malformed"][0]) == {"path", "reason"}


def test_workflow_runs_list_through_ipc_dispatch(tmp_path, pinned):
    _seed(tmp_path)
    result = _dispatch("workflow_runs.list", tmp_path)["result"]
    assert [r["run_id"] for r in result["records"]] == ["r-live"]
    WorkflowRun.model_validate(result["records"][0])
    assert [m["path"] for m in result["malformed"]] == ["state/warp/r-bad/run-stage.json"]


def test_unpinned_schema_yields_empty_rows(tmp_path, monkeypatch):
    _seed(tmp_path)
    for mod in (sizings, workflow_runs):
        monkeypatch.setattr(mod, "_entity_schema_path", lambda name: Path("absent.schema.json"))
    for op in _OPS:
        assert _dispatch(op, tmp_path)["result"] == {"records": [], "malformed": []}


def test_absent_repo_root_returns_empty_payload():
    for handler in (cockpit_sections_query._sizings_summary, cockpit_sections_query._workflow_runs_list):
        assert handler({}, repo_root=None) == {"records": [], "malformed": []}


def test_live_corpus_handlers_stay_under_the_500ms_bar(pinned):
    if len(list((_REPO_ROOT / "state" / "sizings").glob("*.yaml"))) < 100:
        pytest.skip("live sizing corpus absent")
    cockpit_sections_query._sizings_summary({}, repo_root=_REPO_ROOT)  # warm the record cache
    t = time.process_time()
    sizing_out = cockpit_sections_query._sizings_summary({}, repo_root=_REPO_ROOT)
    sizings_secs = time.process_time() - t
    t = time.process_time()
    cockpit_sections_query._workflow_runs_list({}, repo_root=_REPO_ROOT)
    runs_secs = time.process_time() - t
    print(f"sizings.summary {sizings_secs * 1000:.0f}ms process; workflow_runs.list {runs_secs * 1000:.0f}ms")
    assert sizing_out["records"]
    assert sizings_secs < _BAR_SECS
    assert runs_secs < _BAR_SECS
