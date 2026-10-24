"""Interfaces for per-row drop reasons: snapshot reasons, drop note lines, StageManifest.dropped_rows."""

import json

from coordinator_core.ops.dispatch_emit import resume_snapshot, run_notes
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ROWS_DROPPED_WITHOUT_REASON,
    ResumePlan,
    StageManifest,
)


def test_reasons_round_trip(tmp_path):
    resume_snapshot.write(tmp_path, "r1", "p.md", ["B", "A"], ["C"], reasons={"B": "landed", "A": "closed"})
    snap = resume_snapshot.read(tmp_path, "r1", "p.md")
    assert dict(snap.reasons) == {"A": "closed", "B": "landed"}
    raw = json.loads((tmp_path / "scratch/warp/r1/resume-snapshot.json").read_text(encoding="utf-8"))
    assert list(raw["reasons"]) == ["A", "B"]


def test_absent_reasons_read_empty(tmp_path):
    resume_snapshot.write(tmp_path, "r1", "p.md", ["A"], [])
    raw = json.loads((tmp_path / "scratch/warp/r1/resume-snapshot.json").read_text(encoding="utf-8"))
    assert "reasons" not in raw
    assert dict(resume_snapshot.read(tmp_path, "r1", "p.md").reasons) == {}


def test_non_str_entries_dropped_and_malformed_reads_empty(tmp_path):
    resume_snapshot.write(tmp_path, "r1", "p.md", ["A"], [])
    path = tmp_path / "scratch/warp/r1/resume-snapshot.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["reasons"] = {"A": "ok", "B": 3}
    path.write_text(json.dumps(body), encoding="utf-8")
    assert dict(resume_snapshot.read(tmp_path, "r1", "p.md").reasons) == {"A": "ok"}
    body["reasons"] = ["x"]
    path.write_text(json.dumps(body), encoding="utf-8")
    assert dict(resume_snapshot.read(tmp_path, "r1", "p.md").reasons) == {}


def test_drop_lines_are_not_notes(tmp_path):
    p = tmp_path / "notes.jsonl"
    n1 = (json.dumps({"note": "one", "at": "t"}) + "\n").encode()
    n2 = (json.dumps({"note": "two", "at": "t"}) + "\n").encode()
    p.write_bytes(n1 + run_notes.drop_line("FA", "x" * 900, "t", "r1") + n2)
    assert [e["id"] for e in run_notes.entries(p, "r1")] == ["N1", "N2"]
    drops = run_notes.drop_entries(p)
    assert [d["id"] for d in drops] == ["FA"]
    assert len(drops[0]["reason"]) == run_notes.REASON_CAP
    assert run_notes.drop_entries(tmp_path / "missing") == []


def test_manifest_dropped_rows_round_trip():
    base = dict(run_dir="d", rows=(), review_declared_paths=(), marker_path="m")
    assert "dropped_rows" not in StageManifest(**base).to_json()
    m = StageManifest(**base, dropped_rows=({"id": "FA", "reason": "r"},))
    assert StageManifest.from_json(m.to_json()).dropped_rows == ({"id": "FA", "reason": "r"},)


def test_names_and_defaults():
    assert ROWS_DROPPED_WITHOUT_REASON == "ROWS-DROPPED-WITHOUT-REASON"
    assert ResumePlan("p", ("A",)).not_run_reasons == ()
