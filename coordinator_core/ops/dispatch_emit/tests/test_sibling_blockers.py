"""sibling_blockers: names the non-landed fan-out sibling an open spine row waits on."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.sibling_blockers import sibling_blockers
from coordinator_core.warp_fire import fire_command

PARENT = "ask-parent-20261021T194132-ddef39"
OWN = "ask-own"
SIB = "ask-sib"
LANDED = "ask-landed"
PLAN = "docs/plans/own.md"
SIB_PLAN = "docs/plans/sib.md"
HOOK = "pkg/__init__.py"


def _plan(root: Path, edge_plan: str | None, writes=(HOOK,)) -> None:
    edge = f"  depends_on_plan:\n    - plan: {edge_plan}\n      chunk: C2\n" if edge_plan else ""
    body = "".join(f"    - {w}\n" for w in writes)
    text = (
        "---\ntitle: t\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C4\n  title: t\n  disposition: open\n  writes:\n" + body + edge + "```\n"
    )
    path = root / PLAN
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(root: Path, sib_status: str = c.STATUS_RUNNING, manifest_writes=(), bound: bool = True) -> None:
    runs = (
        c.SiblingRun(OWN, "own", (), "state/sizings/own.yaml", c.STATUS_RUNNING),
        c.SiblingRun(SIB, "sib", (), "state/sizings/sib.yaml", c.STATUS_RUNNING),
        c.SiblingRun(LANDED, "landed", (), "state/sizings/landed.yaml", c.STATUS_LANDED),
    )
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, runs).to_json()), encoding="utf-8")
    statuses = {OWN: c.STATUS_RUNNING, SIB: sib_status, LANDED: c.STATUS_LANDED}
    for rid, st in statuses.items():
        if rid == OWN and not bound:
            continue
        f = root / c.status_rel(rid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"status": st, "parent_id": PARENT}), encoding="utf-8")
    sizing = root / "state/sizings/sib.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text(f"plan: {SIB_PLAN}\n", encoding="utf-8")
    mdir = root / "state/warp" / SIB
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C2", "writes": list(manifest_writes)}]}), encoding="utf-8"
    )


def test_depends_on_plan_names_running_sibling_and_refire(tmp_path):
    _plan(tmp_path, SIB_PLAN)
    _seed(tmp_path)
    out = sibling_blockers(tmp_path, OWN, PLAN)
    assert out == [
        {
            "row": "C4",
            "run_id": SIB,
            "label": "sib",
            "status": c.STATUS_RUNNING,
            "plan": SIB_PLAN,
            "via": "depends_on_plan",
            "refire": fire_command("state/sizings/own.yaml"),
        }
    ]


def test_writes_overlap_is_the_fallback(tmp_path):
    _plan(tmp_path, None)
    _seed(tmp_path, manifest_writes=[HOOK, "state/x.json"])
    out = sibling_blockers(tmp_path, OWN, PLAN)
    assert [(e["row"], e["run_id"], e["via"]) for e in out] == [("C4", SIB, "writes")]


def test_mixed_separators_still_match(tmp_path):
    _plan(tmp_path, "./docs/plans/sib.md", writes=["pkg\\__init__.py"])
    _seed(tmp_path, manifest_writes=["pkg\\__init__.py"])
    assert [e["via"] for e in sibling_blockers(tmp_path, OWN, PLAN)] == ["depends_on_plan"]
    _plan(tmp_path, None, writes=["pkg\\__init__.py"])
    assert [e["via"] for e in sibling_blockers(tmp_path, OWN, PLAN)] == ["writes"]


def test_landed_sibling_is_not_a_blocker(tmp_path):
    _plan(tmp_path, SIB_PLAN)
    _seed(tmp_path, sib_status=c.STATUS_LANDED)
    assert sibling_blockers(tmp_path, OWN, PLAN) == []


def test_missing_parent_record_or_status_file_returns_empty(tmp_path):
    _plan(tmp_path, SIB_PLAN)
    _seed(tmp_path)
    (tmp_path / c.parent_record_rel(PARENT)).unlink()
    assert sibling_blockers(tmp_path, OWN, PLAN) == []
    _seed(tmp_path)
    (tmp_path / c.status_rel(OWN)).unlink()
    assert sibling_blockers(tmp_path, OWN, PLAN) == []


def test_unrelated_open_row_returns_empty(tmp_path):
    _plan(tmp_path, None, writes=["elsewhere.py"])
    _seed(tmp_path, manifest_writes=[HOOK])
    assert sibling_blockers(tmp_path, OWN, PLAN) == []


def test_no_subprocess_is_spawned(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    _plan(tmp_path, SIB_PLAN)
    _seed(tmp_path)
    assert len(sibling_blockers(tmp_path, OWN, PLAN)) == 1


def test_sibling_blocked_is_an_admitted_close_input():
    assert "sibling_blocked" in run_close.CLOSE_INPUT_KEYS
