"""run_findings.build_findings_record: ledger rows joined to the usage scan."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import run_findings

SHARE = ".coordinator-local/subagent-share/sess"


def _sidecar(root: Path, name: str, rows: list, agent_type: str = "coordinator:code-reviewer", crlf: bool = False) -> str:
    text = (
        f"---\nagent_type: {agent_type}\nstatus: complete\n---\n\n## Findings\n\nx\n\n"
        f"## Findings Ledger\n\n```json\n{json.dumps(rows, indent=2)}\n```\n\n## Exit interview\n"
    )
    if crlf:
        text = text.replace("\n", "\r\n")
    rel = f"{SHARE}/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return rel


def _scan(*pairs) -> dict:
    return {"agents": [{"agent_id": a, "label": lbl, "total_tokens": t} for a, lbl, t in pairs]}


def _build(root, rels, scan):
    return run_findings.build_findings_record(root, {"wave_sidecar_paths": rels}, scan)


def test_dispositions_and_role(tmp_path):
    rows = [
        {"id": "F1", "file": "a.py", "status": "applied", "verified": True},
        {"id": "F2", "file": "b.py", "status": "em-rejected", "reason": "wrong call"},
        {"id": "F3", "file": "c.py", "status": "em-rejected", "reason": "Not a defect: works as designed"},
        {"id": "F4", "file": "d.py", "status": "suspended"},
        {"id": "F5", "file": "e.py", "status": "proposed"},
    ]
    rel = _sidecar(tmp_path, "coordinator-code-reviewer.abc123", rows)
    rec = _build(tmp_path, [rel], _scan(("abc123", "review:coordinator:code-reviewer", 100)))
    by = {f["finding_id"]: f for f in rec["findings"]}
    assert {k: (v["disposition"], v["ledger_status"]) for k, v in by.items()} == {
        "F1": ("applied", "applied"),
        "F2": ("rejected", "em-rejected"),
        "F3": ("false-alarm", "em-rejected"),
        "F4": ("suspended", "suspended"),
        "F5": ("unresolved", "proposed"),
    }
    assert all(f["reviewer_role"] == "coordinator:code-reviewer" for f in rec["findings"])
    assert by["F1"]["applying_agent"] == "review:coordinator:code-reviewer"
    assert by["F2"]["applying_agent"] == "em"
    assert by["F4"]["applying_agent"] is None and by["F4"]["applying_agent_reason"]
    assert rec["skipped"] == []


def test_tokens_split_exactly_and_by_role(tmp_path):
    rows = [{"id": f"F{i}", "file": "a.py", "status": "applied", "verified": True} for i in range(3)]
    rel = _sidecar(tmp_path, "coordinator-code-reviewer.abc123", rows)
    rec = _build(tmp_path, [rel], _scan(("abc123", "r", 100)))
    assert [f["tokens"] for f in rec["findings"]] == [34, 33, 33]
    assert all(f["agent_tokens"] == 100 for f in rec["findings"])
    assert rec["by_role"] == {"coordinator:code-reviewer": {"findings": 3, "tokens": 100}}


def test_unmatched_agent_gives_null_tokens_with_reason(tmp_path):
    rel = _sidecar(tmp_path, "coordinator-code-reviewer.zzz", [{"id": "F1", "status": "applied"}])
    rec = _build(tmp_path, [rel], _scan(("other", "unrelated", 50)))
    f = rec["findings"][0]
    assert f["tokens"] is None and f["agent_tokens"] is None
    assert f["tokens_reason"] == "agent not matched"
    assert f["applying_agent"] is None


def test_no_scan_gives_null_tokens_with_reason(tmp_path):
    rel = _sidecar(tmp_path, "coordinator-code-reviewer.zzz", [{"id": "F1", "status": "applied"}])
    f = _build(tmp_path, [rel], None)["findings"][0]
    assert f["tokens"] is None and f["tokens_reason"] == "no usage scan"


def test_label_slug_segment_matches_when_id_absent(tmp_path):
    rel = f"{SHARE}/../x/review-C1/note.md".replace(f"{SHARE}/../x", ".coordinator-local/workflow-sidecars/ask/run")
    p = tmp_path / rel
    p.parent.mkdir(parents=True)
    p.write_text(
        "---\nagent_type: coordinator:staff-eng\n---\n\n## Findings Ledger\n\n```json\n"
        '[{"id": "F1", "status": "applied"}]\n```\n',
        encoding="utf-8",
    )
    rec = _build(tmp_path, [rel], _scan(("q", "review-C1", 7)))
    assert rec["findings"][0]["tokens"] == 7


def test_crlf_sidecar_parses(tmp_path):
    rel = _sidecar(tmp_path, "coordinator-code-reviewer.abc", [{"id": "F1", "status": "applied"}], crlf=True)
    rec = _build(tmp_path, [rel.replace("/", "\\")], _scan(("abc", "r", 4)))
    assert [f["finding_id"] for f in rec["findings"]] == ["F1"]
    assert rec["findings"][0]["reviewer_role"] == "coordinator:code-reviewer"


def test_missing_and_garbled_sidecars_are_skipped(tmp_path):
    garbled = f"{SHARE}/bad.md"
    (tmp_path / garbled).parent.mkdir(parents=True)
    (tmp_path / garbled).write_text("---\nagent_type: x\n---\n## Findings Ledger\n\n```json\n{nope\n```\n", encoding="utf-8")
    good = _sidecar(tmp_path, "coordinator-code-reviewer.abc", [{"id": "F1", "status": "applied"}])
    rec = _build(tmp_path, [f"{SHARE}/missing.md", garbled, good, 5], _scan(("abc", "r", 1)))
    assert len(rec["skipped"]) == 2
    assert [f["finding_id"] for f in rec["findings"]] == ["F1"]


def test_non_dict_inline_review_is_empty(tmp_path):
    assert run_findings.build_findings_record(tmp_path, None, None) == {"findings": [], "by_role": {}, "skipped": []}


def test_write_findings_record(tmp_path):
    body = {"findings": [], "by_role": {}, "skipped": []}
    target = run_findings.write_findings_record(tmp_path / "state" / "warp" / "r", body)
    assert target.name == "findings.json"
    assert json.loads(target.read_text(encoding="utf-8")) == body
