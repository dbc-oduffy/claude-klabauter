"""roadmap.blitz_stage read_staged: a truncated stage reply is recovered from the durable
approval.json and frozen gate report, through the op and the params the emitted ask script sends."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops import roadmap_blitz_stage as op
from coordinator_core.roadmap import blitz_stage as bs

CLUSTERS = """# Clusters — rm-test

> Stub slug prefix: `rmt`.

## C1 — Transport
**Rows:** A. **loe:** M
**blocked_by:** none.
"""
RECON = "| Cluster | Verdict |\n|---|---|\n| C1 | **KEEP** |\n"
APPROVED = {"verdict": "approved", "approver": "pm", "utterance": "ship it"}
TRAIL = "scratch/warp/run-1/roadmap-blitz"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    from coordinator_core.roadmap import gate_v2

    monkeypatch.setattr(bs, "_commit_paths", lambda root, paths, message: {"committed": True, "sha": "s1"})
    monkeypatch.setattr(
        gate_v2,
        "compute_gate_report",
        lambda root, **kw: {
            "batons": [{
                "id": "b1", "path": "state/handoffs/b1.md", "title": "t", "sized": True,
                "plan": {"path": "docs/plans/p.md"}, "execution_gate": {"open": True, "blocking": []},
                "sizing_objects": [],
            }],
            "waves": [["b1"]],
        },
    )
    monkeypatch.setattr(op, "main_worktree_root", lambda root: root)
    (tmp_path / ".git").mkdir()
    run = tmp_path / "state" / "roadmap" / "rm-test"
    run.mkdir(parents=True)
    (run / "clusters.md").write_text(CLUSTERS, encoding="utf-8")
    (run / "reconciliation.md").write_text(RECON, encoding="utf-8")
    (run / "OVERVIEW.md").write_text("---\nroadmap_id: rm-test\n---\n# o\n", encoding="utf-8")
    reply = op._handler(
        {"roadmap": "state/roadmap/rm-test", "approval": APPROVED, "trail_dir": TRAIL}, repo_root=tmp_path
    )
    return tmp_path, reply


def _read(root: Path, **read_staged):
    return op._handler({"read_staged": read_staged}, repo_root=root)


def test_truncated_reply_is_recovered_from_disk_with_both_facts(staged):
    root, reply = staged
    truncated = {k: v for k, v in reply.items() if k not in ("gate_report_path", "approval_record")}
    assert not truncated.get("gate_report_path") and not truncated.get("approval_record")
    disk = _read(root, roadmap_id="rm-test", trail_dir=TRAIL)
    assert disk["gate_report_path"] == reply["gate_report_path"]
    assert disk["approval_record"] == reply["approval_record"]
    assert [b["id"] for w in disk["waves"] for b in w["batons"]] == ["b1"]
    # the emitted script's recovery gate: proceed only on both facts
    assert disk["gate_report_path"] and disk["approval_record"]


def test_missing_approval_leaves_the_gate_to_halt(staged):
    root, reply = staged
    (root / TRAIL / "approval.json").unlink()
    disk = _read(root, roadmap_id="rm-test", trail_dir=TRAIL)
    assert disk["approval_record"] is None and disk["gate_report_path"] == reply["gate_report_path"]


def test_missing_gate_report_leaves_the_gate_to_halt(staged):
    root, _ = staged
    again = _read(root, roadmap_id="rm-other", trail_dir=TRAIL)
    assert again["gate_report_path"] is None and again["approval_record"] is not None


def test_read_staged_stages_nothing(staged, monkeypatch):
    root, _ = staged
    monkeypatch.setattr(op, "stage_roadmap", lambda *a, **k: pytest.fail("read_staged must not stage"))
    _read(root, roadmap_id="rm-test", trail_dir=TRAIL)


@pytest.mark.parametrize(
    "read_staged",
    [
        "x",
        {"trail_dir": TRAIL},
        {"roadmap_id": " ", "trail_dir": TRAIL},
        {"roadmap_id": "rm-test"},
        {"roadmap_id": "rm-test", "trail_dir": "state/x"},
        {"roadmap_id": "rm-test", "trail_dir": "scratch/../x"},
        {"roadmap_id": "../rm-test", "trail_dir": TRAIL},
        {"roadmap_id": "a/b", "trail_dir": TRAIL},
    ],
)
def test_malformed_read_staged_refused(staged, read_staged):
    root, _ = staged
    with pytest.raises(ValueError):
        op._handler({"read_staged": read_staged}, repo_root=root)


def test_params_the_emitted_ask_script_sends_are_accepted_by_the_op(staged):
    from coordinator_core.ops.dispatch_emit import ask_roadmap

    root, reply = staged
    lines = ask_roadmap._staged_from_disk_lines(root.as_posix(), "run-1", TRAIL)
    script = "\n".join(lines)
    assert ask_roadmap.READ_STAGED_PARAM in script
    assert "_disk?.gate_report_path && _disk?.approval_record" in script
    trail = re.search(r"trail_dir: '([^']+)'", script)
    assert trail and trail.group(1) == TRAIL
    disk = _read(root, roadmap_id="rm-test", trail_dir=trail.group(1))
    assert disk["gate_report_path"] == reply["gate_report_path"] and disk["approval_record"]
