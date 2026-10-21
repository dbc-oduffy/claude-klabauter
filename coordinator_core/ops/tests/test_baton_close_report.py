"""End-to-end tests for the baton.close_report handler on a tmp_path fixture repo."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from coordinator_core.ops import baton_close_report as op
from coordinator_core.ops.baton_close_report_contract import OP_KEY, REPORT_KEYS


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _baton(status: str = "open", extra: str = "") -> str:
    return f"---\ntitle: t\nstatus: {status}\n{extra}---\n\nbody\n"


def _fixture(root: Path) -> None:
    (root / ".git").mkdir()
    _write(root, "docs/plans/done.md", "---\nstatus: implemented\n---\n")
    _write(root, "docs/plans/live.md", "---\nstatus: reviewed\n---\n")
    _write(root, "state/handoffs/closable.md", _baton(extra="governing_plan: docs/plans/done.md\n"))
    _write(root, "state/handoffs/claimed.md", _baton("claimed", "governing_plan: docs/plans/done.md\n"))
    _write(root, "state/handoffs/conflict.md",
           _baton(extra="governing_plan: docs/plans/done.md\nsizing_object: state/sizings/none.yaml\n"))
    _write(root, "state/handoffs/missing.md", _baton(extra="governing_plan: docs/plans/gone.md\n"))
    _write(root, "state/handoffs/notyet.md", _baton(extra="governing_plan: docs/plans/live.md\n"))
    _write(root, "state/handoffs/bare.md", _baton())
    _write(root, "state/handoffs/broken.md", "no frontmatter here\n")
    _write(root, "state/handoffs/closed.md", _baton("concluded", "governing_plan: docs/plans/done.md\n"))
    _write(root, "state/sizings/none.yaml", "status: draft\n")


def _run(root: Path, **params: str) -> dict:
    return asyncio.run(op._handler(params, root))


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def test_report_end_to_end(tmp_path: Path) -> None:
    _fixture(tmp_path)
    rep = _run(tmp_path)
    assert tuple(rep) == REPORT_KEYS
    assert rep["op"] == OP_KEY and rep["exit_code"] == 0 and rep["scan_incomplete"] is False
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/closable.md"]
    assert rep["closable"][0]["evidence"][0]["done"] is True
    reasons = {r["handoff_path"].rsplit("/", 1)[1]: r["reason"] for r in rep["residue"]}
    assert reasons == {
        "broken.md": "unreadable_frontmatter",
        "claimed.md": "claimed",
        "conflict.md": "evidence_conflict",
        "missing.md": "target_missing",
    }
    assert rep["no_evidence"] == 2  # bare.md and notyet.md; closed.md is not live
    assert rep["batons_scanned"] == 7


def test_handoff_param_narrows_to_one_baton(tmp_path: Path) -> None:
    _fixture(tmp_path)
    rep = _run(tmp_path, handoff="state/handoffs/claimed.md")
    assert rep["batons_scanned"] == 1
    assert rep["closable"] == [] and [r["reason"] for r in rep["residue"]] == ["claimed"]
    rep = _run(tmp_path, handoff="./state/handoffs/closable.md")
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/closable.md"]


def test_live_successor_is_residue(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "state/handoffs/next.md", _baton(extra="predecessor: state/handoffs/closable.md\n"))
    rep = _run(tmp_path)
    reasons = {r["handoff_path"].rsplit("/", 1)[1]: r["reason"] for r in rep["residue"]}
    assert reasons["closable.md"] == "live_successor"
    assert rep["closable"] == []


def test_missing_handoffs_dir_is_scan_incomplete(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    rep = _run(tmp_path)
    assert rep["scan_incomplete"] is True
    assert rep["batons_scanned"] == 0 and rep["closable"] == [] and rep["residue"] == []


def test_no_repo_root_is_an_error() -> None:
    assert asyncio.run(op._handler({}, None))["exit_code"] == 1


def _plan(status: str, extra: str = "") -> str:
    return f"---\nstatus: {status}\n{extra}---\n"


def _reverse_root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _by_name(items: list[dict]) -> dict[str, dict]:
    return {i["handoff_path"].rsplit("/", 1)[1]: i for i in items}


def test_reverse_implemented_plan_by_deliverable_id_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b.md", _baton(extra="deliverable_id: dlv-a\n"))
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/b.md"]
    ev = rep["closable"][0]["evidence"]
    assert [e["field"] for e in ev] == ["plan.deliverable_id"]
    assert rep["no_evidence"] == 0


def test_reverse_landed_plan_stays_no_evidence(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("landed", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b.md", _baton(extra="deliverable_id: dlv-a\n"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == []
    assert rep["no_evidence"] == 1


def test_reverse_predecessor_handoff_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "predecessor_handoff: state\\handoffs\\b.md\n"))
    _write(root, "state/handoffs/b.md", _baton())
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/b.md"]
    assert [e["field"] for e in rep["closable"][0]["evidence"]] == ["plan.predecessor_handoff"]


def test_reverse_spinoff_baton_not_closed_by_deliverable_join(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/s.md", _baton(extra="deliverable_id: dlv-a\nkind: spinoff\n"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == []
    assert rep["no_evidence"] == 1


def test_reverse_implemented_and_draft_plans_conflict(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p1.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "docs/plans/p2.md", _plan("draft", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b.md", _baton(extra="deliverable_id: dlv-a\n"))
    rep = _run(root)
    assert rep["closable"] == []
    assert [(r["handoff_path"], r["reason"]) for r in rep["residue"]] == [
        ("state/handoffs/b.md", "evidence_conflict")
    ]


def test_reverse_shared_deliverable_id_across_batons_stays_no_evidence(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b1.md", _baton(extra="deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b2.md", _baton(extra="deliverable_id: dlv-a\n"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == []
    assert rep["no_evidence"] == 2


def test_writes_nothing(tmp_path: Path) -> None:
    _fixture(tmp_path)
    before = _snapshot(tmp_path)
    _run(tmp_path)
    assert _snapshot(tmp_path) == before


def test_zero_subprocess_spawns(tmp_path: Path, monkeypatch) -> None:
    _fixture(tmp_path)
    calls: list[object] = []

    def _count(*a, **k):
        calls.append(a)
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _count)
    monkeypatch.setattr(os, "system", _count)
    _run(tmp_path)
    assert calls == []
