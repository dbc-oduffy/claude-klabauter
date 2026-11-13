"""Tests for coordinator_core.post_run.p5_audit."""

from __future__ import annotations

import json

from coordinator_core.ops import post_run_finish
from coordinator_core.post_run import p5_audit

RESULT = {
    "run_id": "r-1",
    "promoted_files": ["docs/wiki/a.md", {"target": "docs/wiki/b.md"}],
    "distillation_log_rows": [
        {"path": "tasks/x.md", "disposition": "DELETE", "fate": "promoted"},
        {"path": "tasks/y.md", "disposition": "KEEP", "fate": "kept", "run_id": "r-0"},
    ],
    "phase_3d_deletion_manifest": {"deletions": [
        {"artifact_path": "tasks/x.md", "disposition": "DELETE"},
        {"artifact_path": "tasks/z.json", "disposition": "DELETE"},
        {"artifact_path": "tasks/y.md", "disposition": "KEEP"},
    ]},
    "provenance_completeness": {"complete": 2, "files": 3, "missing_fields": ["f1"]},
    "dr_minting": {"minted": 2, "ids": ["DR-1"], "nested": {"a": 1}},
    "contradiction_detection": {"total_unresolvable_contradictions": 0},
    "opus_escalation": {"resolution": {"ok": True}},
    "coverage": {"covered": 5, "gaps": ["g1", "g2"]},
}
LOGS = ["phase-3d: ok", "unrelated noise", "BLOCKED nothing"]


def test_counts_and_log_lines(tmp_path):
    out = p5_audit.run({"result": RESULT, "logs": LOGS}, [], repo_root=tmp_path)
    rep = out["report"]
    assert rep["promoted_files"] == 2 and rep["distillation_log_rows"] == 2
    assert rep["deletion_dispositions"] == {"DELETE": 2, "KEEP": 1}
    assert rep["provenance"] == {"complete": 2, "files": 3, "missing_fields": 1}
    assert rep["dr_minting"] == {"minted": 2}
    assert rep["coverage"] == {"covered": 5, "gaps": 2}
    assert rep["total_unresolvable_contradictions"] == 0
    assert rep["opus_resolution"] == {"ok": True}
    assert rep["log_lines"] == ["phase-3d: ok", "BLOCKED nothing"]
    assert (rep["delete_md"], rep["delete_non_md"]) == (1, 1)


def test_unexplained_both_sides(tmp_path):
    porcelain = [
        " M docs/wiki/a.md",
        "?? docs/decisions/DR-1.md",
        " D tasks/x.md",
        "R  old.md -> docs/wiki/b.md",
        " M src/stray.py",
        '?? "odd/name.md"',
    ]
    out = p5_audit.run(RESULT, porcelain, repo_root=tmp_path)
    assert out["unexplained"] == ["odd/name.md", "src/stray.py"]
    assert out["report"]["unexplained"] == 2
    assert p5_audit.run(RESULT, porcelain[:4], repo_root=tmp_path)["unexplained"] == []


def test_evidence_files(tmp_path):
    out = p5_audit.run(RESULT, [], repo_root=tmp_path)
    assert out["evidence"] == [
        "state/post-run/r-1/p5-delete-md.txt",
        "state/post-run/r-1/p5-distillation-log-rows.jsonl",
    ]
    assert (tmp_path / out["evidence"][0]).read_text() == "tasks/x.md\n"
    rows = [json.loads(l) for l in (tmp_path / out["evidence"][1]).read_text().splitlines()]
    assert rows == [
        {"path": "tasks/x.md", "disposition": "DELETE", "fate": "promoted", "run_id": "r-1"},
        {"path": "tasks/y.md", "disposition": "KEEP", "fate": "kept", "run_id": "r-0"},
    ]


def test_empty_result_writes_nothing(tmp_path):
    out = p5_audit.run({}, [" M a.md"], repo_root=tmp_path)
    assert out["evidence"] == [] and out["unexplained"] == ["a.md"]
    assert not (tmp_path / "state").exists()


def test_run_id_cannot_escape_evidence_dir(tmp_path):
    out = p5_audit.run({"run_id": "../../x", "distillation_log_rows": [{"path": "p"}]}, [], repo_root=tmp_path)
    assert all(e.startswith("state/post-run/") and ".." not in e for e in out["evidence"])


def test_finish_audit_consumes_run_output(tmp_path):
    unexplained, evidence = post_run_finish._audit(
        "distill", {"result": RESULT, "porcelain": [" M src/stray.py"]}, tmp_path)
    assert unexplained == ["src/stray.py"] and len(evidence) == 2
    assert all((tmp_path / e).is_file() for e in evidence)
    assert post_run_finish._audit("lessons", {"result": RESULT}, tmp_path) == ([], [])
