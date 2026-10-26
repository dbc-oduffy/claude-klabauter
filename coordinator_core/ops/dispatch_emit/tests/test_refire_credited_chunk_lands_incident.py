"""Incident replay: a refire after ``workflow.revise credit_chunks`` lands the credited row's declared product.

A first fire refuses ``no-credited-chunk-landed`` with X1 stranded; the EM credits X1 and refires; the
refire's ``dispatch.land_run`` must put X1's declared file in its commit and name X1 in ``credited_chunks``.
"""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _handler as ask_gate
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.land_run_contract import REPLY_CREDITED_CHUNKS
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo as _base_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/2026-10-24-aa-credited-refire.yaml"
PLAN = "docs/plans/2026-10-24-aa-credited-refire.md"
DLV = "dlv-credited-aaaaaa"
RUN1 = "ask-20261024T100000-aaaaaa"
RUN2 = "ask-20261024T110000-bbbbbb"
X0_PATH, X1_PATH = "src/x0.py", "src/x1.py"
_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = reviewed()
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _plan_text() -> str:
    rows = "".join(
        f"- id: {rid}\n  title: row {rid}\n  change_kind: code-edit\n  surface: {path}\n  writes:\n    - {path}\n"
        "  disposition: open\n  body: |\n    b\n"
        for rid, path in (("X0", X0_PATH), ("X1", X1_PATH))
    )
    return (
        f"---\ntitle: credited refire\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\n---\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{rows}```\n"
    )


def _stage_run(root, run_id: str, chunk_paths: dict) -> str:
    """Write a run's commit-request marker and script for ``chunk_paths`` ({row id: path}); returns the script."""
    request = CommitRequest(
        chunks=tuple(ChunkCommit(id=cid, title=cid, paths=(p,)) for cid, p in chunk_paths.items()),
        deliverable_id=DLV, plan_path=PLAN,
    )
    marker = f"{RUN_DIR_ROOT}/{run_id}/commit-request.txt"
    _write(root, marker, render_marker(request) + "\n")
    manifest = f"{RUN_DIR_ROOT}/{run_id}/manifest.json"
    manifest_doc = json.loads((root / manifest).read_text(encoding="utf-8")) if (root / manifest).is_file() else {
        "run_dir": f"{RUN_DIR_ROOT}/{run_id}", "rows": [], "review_declared_paths": [], "marker_path": marker,
    }
    manifest_doc["marker_path"] = marker
    _write(root, manifest, json.dumps(manifest_doc))
    script = f"{run_id}.mjs"
    _write(root, script, f"// emitted\n{ASK_MANIFEST_MARKER}{manifest}\n")
    return script


def _land(root, script: str, run_id: str, incomplete: list) -> dict:
    return land_run._handler(
        {
            "script_path": script, "run_id": run_id, "incomplete_chunks": incomplete,
            "inline_review": dict(_REVIEWED), "session_id": _SID, "ledger_verdict": _MET,
        },
        repo_root=root / ".git",
    )


@pytest.fixture
def halted(_base_repo, monkeypatch):  # noqa: F811
    """A first fire (RUN1) that refused no-credited-chunk-landed with X0 and X1 stranded."""
    root = _base_repo
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    doc = yaml.safe_load((root / "state/sizings/20260101-a.yaml").read_text(encoding="utf-8"))
    doc.update(
        name="credited refire", plan=PLAN, deliverable_id=DLV, interaction_mode="pm", route="spec-dispatch",
        status="sized", exit_criterion={"statement": "done", "accepted": None},
    )
    _write(root, SIZING, yaml.safe_dump(doc, sort_keys=False))
    _write(root, PLAN, _plan_text())
    _write(root, X0_PATH, "x0 = 1\n")
    _write(root, X1_PATH, "x1 = 1\n")
    rl.start(root, RUN1, all_of_pm_ask=False)
    rl.bind(root, RUN1, SIZING)
    script = _stage_run(root, RUN1, {"X0": X0_PATH, "X1": X1_PATH})
    first = _land(root, script, RUN1, ["X0", "X1"])
    assert first.get("refused") == "no-credited-chunk-landed", first
    return root


def test_refire_after_credit_lands_the_credited_rows_declared_path(halted):
    root = halted
    revised = rl._handler(
        {"run_id": RUN1, "credit_chunks": [{"chunk": "X1", "evidence": "x1 complete; tests pass"}]}, repo_root=root
    )
    assert revised["ok"] is True, revised

    gate_reply = ask_gate({"sizing_path": SIZING, "run_id": RUN2, "session_id": _SID}, repo_root=root)
    assert gate_reply.get("halt") is None, gate_reply
    assert gate_reply.get("resume_rows") == ["X0"], gate_reply

    _write(root, X0_PATH, "x0 = 2\n")
    script = _stage_run(root, RUN2, {"X0": X0_PATH})
    rl.start(root, RUN2, all_of_pm_ask=False, resumed_from=RUN1)
    rl.bind(root, RUN2, SIZING)
    out = _land(root, script, RUN2, [])

    assert out["committed"] is True, out
    landed = set(_git(root, "show", "--name-only", "--format=", out["sha"]).split())
    assert X0_PATH in landed
    assert X1_PATH in landed, f"credited row X1's declared path missing from the landing: {sorted(landed)}"
    assert out[REPLY_CREDITED_CHUNKS] == ["X1"], out
    assert _git(root, "status", "--porcelain", "--", X1_PATH).strip() == ""


def test_credited_row_closed_on_the_spine_or_held_is_not_synthesised():
    request = CommitRequest(chunks=(ChunkCommit(id="X0", title="x0", paths=(X0_PATH,)),), deliverable_id=DLV)
    credits = {"X1": "done"}
    closed = _plan_text().replace("disposition: open\n  body: |\n    b\n```", "disposition: coded\n  body: |\n    b\n```")
    assert [c.id for c in land_run._credited_absent_chunks(_plan_text(), request, credits, set(), set())] == ["X1"]
    assert land_run._credited_absent_chunks(closed, request, credits, set(), set()) == []
    assert land_run._credited_absent_chunks(_plan_text(), request, credits, {"X1"}, set()) == []
    assert land_run._credited_absent_chunks(_plan_text(), request, {}, set(), set()) == []
