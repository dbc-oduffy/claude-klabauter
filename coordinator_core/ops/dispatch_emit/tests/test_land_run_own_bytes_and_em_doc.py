"""The measured-rev drift checks attribute before refusing: a path whose bytes are the run's own last
write, or an edit outside the stage manifest's declared writes, is not drift; a foreign rewrite of a
declared path still refuses (land_run's ledger gate, the terminal-commit ledger refusal, and the
landing-tree mismatch)."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, measured_rev, requirements_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.session import touch_record

RID = "run-c2"
SID = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb6"
VERDICT = {"items": [{"id": "R1", "status": "met"}]}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _setup(tmp_path, declared=("a.py",)):
    """a.py is a declared product path, b.md a recorded doc outside the declared writes."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    (tmp_path / "b.md").write_text("doc\n", encoding="utf-8", newline="")
    record = measured_rev_record_path(tmp_path, RID)
    record.parent.mkdir(parents=True)
    blobs = {p: commit.checkin_blob_sha(tmp_path, p) for p in ("a.py", "b.md")}
    record.write_text(json.dumps({"blobs": blobs, "removed": []}), encoding="utf-8")
    run_dir = record.parent
    if declared is not None:
        (run_dir / "manifest.json").write_text(
            json.dumps({"rows": [{"id": "C1", "writes": list(declared)}]}), encoding="utf-8"
        )
    (run_dir / "run-stage.json").write_text(json.dumps({"session_id": SID}), encoding="utf-8")
    return run_dir


def _own_write(root, rel, text, at=1000.0):
    touch_record.append_event(
        touch_record.sink_path(root / ".git" / "coordinator-sessions" / SID),
        session_id=SID, agent_id=None, verb="T", path=rel, content_hash=_sha(text), kind="w", timestamp=at,
    )


def _gate(root, monkeypatch, run_dir):
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: SimpleNamespace(items=[1], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []})
    return land_run._ledger_gate(root, run_dir, VERDICT, None, [], {}, None, {}, None, None)


def test_an_em_edit_outside_the_declared_writes_is_not_drift(tmp_path, monkeypatch):
    run_dir = _setup(tmp_path)
    (tmp_path / "b.md").write_text("em edit\n", encoding="utf-8", newline="")
    assert measured_rev.measured_rev_drift(run_dir, tmp_path) == ["b.md"]
    assert measured_rev.foreign_drift(run_dir, tmp_path) == []
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert refusal is None
    assert terminal_commit_refusals._measured_rev_drift_refusal(
        run_dir, tmp_path, VERDICT, SimpleNamespace(items=[1], all_of_pm_ask=False)
    ) is None


def test_a_foreign_rewrite_of_a_declared_path_still_refuses(tmp_path, monkeypatch):
    run_dir = _setup(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8", newline="")
    (tmp_path / "b.md").write_text("em edit\n", encoding="utf-8", newline="")
    assert measured_rev.foreign_drift(run_dir, tmp_path) == ["a.py"]
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert any(f["reason"] == "measured-rev-drift: a.py" for f in refusal["ledger_failures"])
    out = terminal_commit_refusals._measured_rev_drift_refusal(
        run_dir, tmp_path, VERDICT, SimpleNamespace(items=[1], all_of_pm_ask=False)
    )
    assert out["refused"] == "ledger-unmet"
    assert "measured-rev-drift: a.py" in out["error"]


def test_the_runs_own_fix_after_the_record_is_not_drift(tmp_path, monkeypatch):
    run_dir = _setup(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8", newline="")
    _own_write(tmp_path, "a.py", "x = 2\n")
    assert measured_rev.foreign_drift(run_dir, tmp_path) == []
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert refusal is None


def test_a_foreign_rewrite_after_the_runs_last_own_write_still_refuses(tmp_path):
    run_dir = _setup(tmp_path)
    (tmp_path / "a.py").write_text("x = 3\n", encoding="utf-8", newline="")
    _own_write(tmp_path, "a.py", "x = 2\n")
    assert measured_rev.foreign_drift(run_dir, tmp_path) == ["a.py"]


def test_with_no_declared_writes_on_record_nothing_is_outside_them(tmp_path):
    run_dir = _setup(tmp_path, declared=None)
    (tmp_path / "b.md").write_text("em edit\n", encoding="utf-8", newline="")
    assert measured_rev.foreign_drift(run_dir, tmp_path) == ["b.md"]


def _record_for(tmp_path, text):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "p.py").write_text(text, encoding="utf-8", newline="")
    return {"blobs": {"p.py": commit.checkin_blob_sha(tmp_path, "p.py")}, "removed": []}


def test_a_landing_path_the_run_rewrote_after_the_record_is_not_a_mismatch(tmp_path):
    """Case (c): the fix leg edited p.py after the measured record."""
    record = _record_for(tmp_path, "x = 1\n")
    (tmp_path / "p.py").write_text("x = 2\n", encoding="utf-8", newline="")
    assert land_run._measured_mismatch(tmp_path, record, ["p.py"], [], {}) == ["p.py"]
    seen: list = []

    def owned(paths):
        seen.append(list(paths))
        return ["p.py"]

    assert land_run._measured_mismatch(tmp_path, record, ["p.py"], [], {}, owned_now=owned) == []
    assert seen == [["p.py"]]


def test_a_landing_path_the_run_did_not_write_is_still_a_mismatch(tmp_path):
    record = _record_for(tmp_path, "x = 1\n")
    (tmp_path / "p.py").write_text("x = 2\n", encoding="utf-8", newline="")
    assert land_run._measured_mismatch(tmp_path, record, ["p.py"], [], {}, owned_now=lambda paths: []) == ["p.py"]


def test_own_bytes_now_attributes_through_the_session_ledger(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "p.py").write_text("x = 2\n", encoding="utf-8", newline="")
    (tmp_path / "q.py").write_text("y = 9\n", encoding="utf-8", newline="")
    _own_write(tmp_path, "p.py", "x = 2\n")
    _own_write(tmp_path, "q.py", "y = 1\n")
    owned = land_run._own_bytes_now(tmp_path, None, SID, None, {})
    assert owned(["p.py", "q.py"]) == ["p.py"]
